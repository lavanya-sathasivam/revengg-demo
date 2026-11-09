"""Deterministic sentence embeddings for contract clauses.

Phase 4 needs an embedding model that works offline, is reproducible, and does
not add a heavyweight runtime dependency to an SQLite-only demo.  This module
therefore ships a local *legal-concept hashing* sentence encoder:

* text is tokenised, lightly stemmed and stripped of English stopwords;
* words and bi-grams are feature-hashed into a fixed 384-dimensional space;
* a curated legal concept lexicon adds synonym-level features (for example
  "overdue", "arrears" and "late payment" all contribute to ``late_payment``);
* vectors are L2-normalised, so cosine similarity is a dot product.

This is a sentence embedding model, not a neural transformer.  It is
deliberately deterministic and dependency-free, and the model interface is
pluggable: a deployment may register a Sentence-Transformers model through
``REVENGG_EMBEDDING_MODEL`` when that optional package is installed.  The
default model is what the tests and the evaluation in this repository measure.

Model/version metadata is returned with every vector and persisted in SQLite by
``retrieval_index.py``.  A vector is never described as coming from a model it
was not produced by.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


MODEL_KEY = "legal-concept-hash-v1"
MODEL_PROVIDER = "local-deterministic"
MODEL_NAME = "legal-concept-hash"
MODEL_VERSION = "1.0.0"
MODEL_DIMENSION = 384
DISTANCE_METRIC = "cosine"

# Keep this in the model configuration hash: changing the lexicon changes the
# meaning of a stored vector and therefore requires a new model version.
LEXICON_VERSION = "legal-concepts-2026-11"

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_CLAUSE_NUMBER_RE = re.compile(r"\b(?:clause|section)\s+(\d+(?:\.\d+)*)", re.I)
_BARE_CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s*$")

# Words that carry no retrieval signal.  "shall" is included because it occurs
# in almost every operative clause and would dominate similarity.
STOPWORDS = frozenset(
    {
        "a", "about", "above", "after", "again", "against", "all", "am",
        "an", "and", "any", "are", "as", "at", "be", "because", "been",
        "before", "being", "below", "between", "both", "but", "by", "can",
        "could", "did", "do", "does", "doing", "down", "during", "each",
        "few", "for", "from", "further", "had", "has", "have", "having",
        "he", "her", "here", "hers", "him", "his", "how", "i", "if", "in",
        "into", "is", "it", "its", "itself", "just", "me", "more", "most",
        "my", "no", "nor", "not", "now", "of", "off", "on", "once", "only",
        "or", "other", "our", "ours", "out", "over", "own", "same", "she",
        "should", "so", "some", "such", "than", "that", "the", "their",
        "theirs", "them", "then", "there", "these", "they", "this", "those",
        "through", "to", "too", "under", "until", "up", "very", "was", "we",
        "were", "what", "when", "where", "which", "while", "who", "whom",
        "why", "will", "with", "would", "you", "your", "yours", "shall",
        "may", "must", "hereby", "thereof", "therein", "herein", "pursuant",
    }
)

# Legal concepts.  ``words`` are matched after stemming; ``phrases`` are
# matched against the normalised text so a multi-word concept is not lost when
# its component words are ordinary.
CONCEPTS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "payment": {
        "words": (
            "pay", "payment", "invoice", "charge", "amount", "remit",
            "payable", "billing", "bill", "settle", "settlement", "due",
        ),
        "phrases": (
            "amount due", "amount stated", "payment period", "payment due date",
            "sum payable", "invoice date", "electronic transfer",
        ),
    },
    "time_period": {
        "words": (
            "day", "month", "year", "period", "deadline", "date", "within",
            "after", "before", "prior", "duration", "calendar", "time",
        ),
        "phrases": (
            "within days", "after the date", "prior written notice",
            "business day", "calendar month", "initial term", "successive periods",
        ),
    },
    "late_payment": {
        "words": ("late", "overdue", "arrear", "delay", "delayed", "default"),
        "phrases": (
            "late payment", "late charge", "late fee", "overdue amount",
            "fails to pay", "failure to pay", "paid late", "past due",
        ),
    },
    "interest": {
        "words": ("interest", "accrue", "accrual", "annual", "annum", "rate"),
        "phrases": (
            "interest at the rate", "per annum", "interest shall accrue",
            "rate of interest",
        ),
    },
    "penalty": {
        "words": ("penalty", "penalties", "liquidated", "damages", "forfeit"),
        "phrases": (
            "late charge", "late fee", "liquidated damages", "not a penalty",
            "service credit",
        ),
    },
    "termination": {
        "words": (
            "terminate", "termination", "cancel", "cancellation", "expiry",
            "expiration", "end", "survive", "survival",
        ),
        "phrases": (
            "terminate this agreement", "right to terminate",
            "termination for convenience", "termination for non-payment",
            "survive termination", "with immediate effect",
        ),
    },
    "default": {
        "words": ("default", "breach", "insolvency", "wound", "nonpayment"),
        "phrases": (
            "event of default", "in default", "failure to pay",
            "act of insolvency", "non-payment", "remains in default",
        ),
    },
    "cure": {
        "words": ("cure", "cured", "remedy", "remedied", "rectify", "grace"),
        "phrases": (
            "cure period", "cure that failure", "cure the failure",
            "grace period", "opportunity to cure", "failure is cured",
        ),
    },
    "notice": {
        "words": (
            "notice", "notify", "notification", "communication", "serve",
            "served", "despatch", "dispatch", "deliver", "delivery",
        ),
        "phrases": (
            "written notice", "notices", "deemed served", "notice in writing",
            "prior written notice", "address of the receiving party",
        ),
    },
    "service_level": {
        # "service" alone is a scope word, not a service-level commitment;
        # requiring the phrase keeps "what services are provided?" from being
        # answered with a service-level clause.
        "words": (
            "availability", "uptime", "sla", "level", "performance",
            "standard", "percent", "percentage",
        ),
        "phrases": (
            "service level", "service levels", "monthly availability",
            "achieve the service levels", "reasonable skill and care",
        ),
    },
    "service_credit": {
        "words": ("credit", "credits", "rebate", "compensation", "remedy"),
        "phrases": (
            "service credit", "service credits", "credit against the charges",
            "availability below",
        ),
    },
    "liability": {
        "words": (
            "liability", "liable", "cap", "capped", "consequential", "indirect",
            "loss", "losses", "aggregate", "exclude", "exclusion",
        ),
        "phrases": (
            "limitation of liability", "aggregate liability",
            "in no event", "indirect or consequential loss",
            "shall not be liable", "liability shall not exceed",
        ),
    },
    "indemnity": {
        "words": ("indemnify", "indemnity", "harmless", "defend", "indemnified"),
        "phrases": (
            "hold harmless", "indemnify the customer", "indemnify the supplier",
            "against all losses",
        ),
    },
    "confidentiality": {
        "words": (
            "confidential", "confidentiality", "proprietary", "secret",
            "disclose", "disclosure", "nondisclosure",
        ),
        "phrases": (
            "confidential information", "non-disclosure", "keep confidential",
            "shall not disclose",
        ),
    },
    "force_majeure": {
        "words": ("majeure", "unforeseeable", "mitigate", "endeavour", "god"),
        "phrases": (
            "force majeure", "beyond its reasonable control",
            "beyond reasonable control", "act of god",
        ),
    },
    "governing_law": {
        "words": ("govern", "governed", "governing", "law", "laws", "jurisdiction"),
        "phrases": (
            "governing law", "laws of india", "governed by the laws",
            "shall be governed",
        ),
    },
    "arbitration": {
        "words": (
            "arbitration", "arbitrator", "arbitral", "tribunal", "conciliation",
            "seat", "seated",
        ),
        "phrases": (
            "arbitration and conciliation act", "sole arbitrator",
            "referred to arbitration", "seat of arbitration", "dispute resolution",
        ),
    },
    "dispute": {
        "words": ("dispute", "disputed", "disagreement", "contest", "withhold", "bona"),
        "phrases": (
            "good faith negotiation", "genuinely disputed", "disputed invoice",
            "grounds of dispute", "withhold payment",
        ),
    },
    "assignment": {
        "words": ("assign", "assignment", "transfer", "novation", "successor"),
        "phrases": (
            "assign or transfer", "prior written consent", "merger or reorganisation",
            "successor in connection",
        ),
    },
    "amendment": {
        "words": ("amend", "amendment", "variation", "modify", "modification", "change"),
        "phrases": (
            "no amendment", "in writing and signed", "authorised representative",
            "change control", "agreement by email",
        ),
    },
    "term": {
        "words": (
            "term", "renew", "renewal", "commence", "effective", "expire",
            "expiration", "duration", "initial",
        ),
        "phrases": (
            "initial term", "renews automatically", "successive periods",
            "term of this agreement", "effective date",
        ),
    },
    "tax": {
        # The tax-exclusive wording is captured by the phrases below rather
        # than by the ambiguous word "exclusive".
        "words": ("tax", "taxes", "gst", "vat", "withholding"),
        "phrases": (
            "exclusive of applicable taxes", "exclusive of taxes", "tax amount",
            "added to the relevant invoice",
        ),
    },
    "warranty": {
        "words": ("warrant", "warranty", "warranties", "representation", "represent"),
        "phrases": ("warranties and representations", "warrants that"),
    },
    "increase": {
        "words": ("increase", "escalate", "uplift", "raise", "raise", "adjustment"),
        "phrases": (
            "increase in charges", "increase the charges", "shall not exceed",
            "per annum",
        ),
    },
    "scope": {
        "words": (
            "scope", "service", "services", "obligation", "obligations", "duty",
            "duties", "perform", "performance", "provide", "responsibility",
            "responsibilities", "deliver",
        ),
        "phrases": (
            "scope of services", "shall provide the services",
            "shall perform the services",
        ),
    },
    "definition": {
        "words": ("mean", "means", "meaning", "define", "definition", "defined", "interpretation"),
        "phrases": ('"means"', "shall mean", "means, in respect of", "definition"),
    },
    "exclusivity": {
        # "exclusive" alone is deliberately absent: "exclusive of taxes" is a
        # tax phrase, not an exclusivity obligation.  The phrases below are
        # specific enough to distinguish the two.
        "words": ("exclusivity", "compete", "competition", "solicit"),
        "phrases": (
            "exclusivity", "non-compete", "non-solicit", "right of first refusal",
            "most favoured customer",
        ),
    },
    "first_refusal": {
        "words": ("refusal", "rofr", "preemption", "preemptive"),
        "phrases": ("right of first refusal", "right of first offer", "first refusal"),
    },
    "data_protection": {
        "words": ("privacy", "gdpr", "personal", "data", "protection", "processor"),
        "phrases": (
            "data protection", "personal data", "data privacy", "gdpr",
            "information security",
        ),
    },
    "insurance": {
        "words": ("insurance", "insure", "insured", "policy", "coverage", "cover"),
        "phrases": (
            "insurance policy", "maintain insurance", "public liability insurance",
            "professional indemnity insurance",
        ),
    },
    "force": {
        "words": ("force", "enforce", "enforceable", "binding"),
        "phrases": ("force majeure", "force and effect"),
    },
}

# Short aliases used by intent detection and query understanding.
INTENT_CONCEPTS = frozenset(
    {
        "payment", "late_payment", "interest", "penalty", "termination",
        "default", "cure", "notice", "service_level", "service_credit",
        "liability", "indemnity", "confidentiality", "force_majeure",
        "governing_law", "arbitration", "dispute", "assignment", "amendment",
        "term", "tax", "warranty", "increase", "scope", "definition",
        "exclusivity", "first_refusal", "data_protection", "insurance",
    }
)

_WORD_TO_CONCEPT: Dict[str, str] = {}
for _concept, _entry in CONCEPTS.items():
    for _word in _entry.get("words", ()):
        _WORD_TO_CONCEPT.setdefault(_word, _concept)

# A second lookup for common inflections that the light stemmer does not
# collapse (e.g. "charges" -> "charge" already works; "liabilities" -> "liability").
_INFLECTION_FIXES = {
    "charges": "charge",
    "payments": "payment",
    "invoices": "invoice",
    "interest": "interest",
    "penalties": "penalty",
    "services": "service",
    "losses": "loss",
    "liabilities": "liability",
    "obligations": "obligation",
    "duties": "duty",
    "parties": "party",
    "days": "day",
    "months": "month",
    "years": "year",
    "notices": "notice",
    "credits": "credit",
    "taxes": "tax",
    "amendments": "amendment",
    "assignments": "assignment",
    "terminations": "termination",
    # Verb forms whose final "e" would otherwise be lost by the light stemmer
    # ("provided" -> "provid"), preventing them from matching the lexicon.
    "provided": "provide",
    "provides": "provide",
    "providing": "provide",
    "performed": "perform",
    "performs": "perform",
    "performing": "perform",
    "delivered": "deliver",
    "delivers": "deliver",
    "delivering": "deliver",
    "terminated": "terminate",
    "terminates": "terminate",
    "terminating": "terminate",
    "assigned": "assign",
    "assigns": "assign",
    "assigning": "assign",
    "amended": "amend",
    "amends": "amend",
    "amending": "amend",
    "disclosed": "disclose",
    "discloses": "disclose",
    "disclosing": "disclose",
}


def _normalise_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def _stem(token: str) -> str:
    """A deliberately small, deterministic stemmer.

    It only handles forms that recur in contract language and never changes a
    token that already matches a vocabulary word.  Aggressive stemming would
    make "assignment" and "assign" collapse (desirable) but could also make
    unrelated legal terms collide (not desirable).
    """

    # Strip a possessive ending only.  ``str.strip("'s")`` would also remove
    # a leading "s", turning "services" into "ervice" and corrupting both the
    # hashed vector and BM25 terms.
    token = token.strip()
    if token.endswith("'s"):
        token = token[:-2]
    token = token.strip("'")
    if not token:
        return ""
    token = _INFLECTION_FIXES.get(token, token)
    if token in _WORD_TO_CONCEPT or token in STOPWORDS:
        return token
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("ing"):
        base = token[:-3]
        if len(base) > 2 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es") and not token.endswith("ses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> List[str]:
    """Return meaningful stemmed tokens, preserving clause-like numbers."""

    normalised = _normalise_text(text)
    tokens: List[str] = []
    for match in _TOKEN_RE.finditer(normalised):
        token = match.group(0)
        if token in STOPWORDS:
            continue
        stem = _stem(token)
        if stem and stem not in STOPWORDS:
            tokens.append(stem)
    return tokens


def concepts_in_text(text: str) -> List[str]:
    """Return the legal concepts detected in *text*, in lexicon order."""

    normalised = " " + _normalise_text(text) + " "
    token_set = set(tokenize(text))
    found: List[str] = []
    for concept, entry in CONCEPTS.items():
        hit = False
        for word in entry.get("words", ()):
            if _stem(word) in token_set or word in token_set:
                hit = True
                break
        if not hit:
            for phrase in entry.get("phrases", ()):
                phrase_norm = _normalise_text(phrase).strip('"')
                if phrase_norm and phrase_norm in normalised:
                    hit = True
                    break
        if hit:
            found.append(concept)
    return found


def _feature_indexes(feature: str) -> Tuple[int, int, float]:
    digest = hashlib.blake2b(
        ("%s|%s" % (MODEL_KEY, feature)).encode("utf-8"), digest_size=8
    ).digest()
    value = int.from_bytes(digest, "little")
    first = value % MODEL_DIMENSION
    second = (value >> 32) % MODEL_DIMENSION
    # Signed hashing keeps collisions from systematically inflating similarity.
    sign = 1.0 if (value & 1) else -1.0
    return first, second, sign


def _add_feature(vector: List[float], feature: str, weight: float) -> None:
    if weight == 0:
        return
    first, second, sign = _feature_indexes(feature)
    vector[first] += sign * weight
    # A second, weaker projection reduces the variance of a single collision.
    vector[second] += sign * weight * 0.5


def _embed_hashed(text: str) -> List[float]:
    vector = [0.0] * MODEL_DIMENSION
    tokens = tokenize(text)
    if not tokens:
        return vector

    counts: Dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1

    for token, count in counts.items():
        tf = 1.0 + math.log(count)
        _add_feature(vector, "w:" + token, tf)
        concept = _WORD_TO_CONCEPT.get(token)
        if concept:
            _add_feature(vector, "c:" + concept, 1.6 * tf)
        # Character trigrams give a small amount of robustness to morphological
        # variants that the stemmer did not collapse.
        if len(token) >= 5:
            for index in range(len(token) - 2):
                _add_feature(vector, "g:" + token[index:index + 3], 0.12)

    # Bi-grams retain phrase-like distinctions ("late charge" vs "late").
    for left, right in zip(tokens, tokens[1:]):
        _add_feature(vector, "b:%s_%s" % (left, right), 0.55)

    normalised = " " + _normalise_text(text) + " "
    for concept, entry in CONCEPTS.items():
        for phrase in entry.get("phrases", ()):
            phrase_norm = _normalise_text(phrase).strip('"')
            if phrase_norm and phrase_norm in normalised:
                _add_feature(vector, "p:" + concept + ":" + phrase_norm, 1.35)
                _add_feature(vector, "c:" + concept, 1.0)

    # Clause numbers are exact, high-value features.  A query "clause 7.2"
    # should find clause 7.2 even if no other word matches.
    for match in _CLAUSE_NUMBER_RE.finditer(text or ""):
        _add_feature(vector, "n:" + match.group(1), 3.0)
    bare = _BARE_CLAUSE_RE.match(text or "")
    if bare:
        _add_feature(vector, "n:" + bare.group(1), 3.0)
    for token in tokens:
        if re.fullmatch(r"\d+(?:\.\d+)+", token):
            _add_feature(vector, "n:" + token, 2.0)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _load_sentence_transformer(model_name: str):
    """Optional neural backend.

    It is never imported unless explicitly configured, so the default install
    stays dependency-free.  Returning None means the caller must fail closed
    rather than silently substituting a different model.
    """

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None
    return SentenceTransformer(model_name)


def configured_model_key() -> str:
    """Return the selected model key (environment override supported)."""

    return os.environ.get("REVENGG_EMBEDDING_MODEL", MODEL_KEY).strip() or MODEL_KEY


def model_metadata(model_key: Optional[str] = None) -> dict:
    """Describe a model without pretending an unavailable backend exists."""

    selected = (model_key or configured_model_key()).strip()
    if selected == MODEL_KEY:
        return {
            "model_key": MODEL_KEY,
            "provider": MODEL_PROVIDER,
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "dimension": MODEL_DIMENSION,
            "distance_metric": DISTANCE_METRIC,
            "lexicon_version": LEXICON_VERSION,
            "neural": False,
            "description": (
                "Offline legal-concept hashing sentence encoder. Combines "
                "stemmed word/bi-gram features with a curated legal synonym "
                "lexicon; L2-normalised for cosine similarity."
            ),
        }
    if selected.startswith("sentence-transformers:"):
        name = selected.split(":", 1)[1].strip()
        loaded = _load_sentence_transformer(name)
        if loaded is None:
            raise ValueError(
                "Embedding model %r is configured but sentence-transformers is "
                "not installed." % selected
            )
        dimension = int(loaded.get_sentence_embedding_dimension() or 0)
        return {
            "model_key": selected,
            "provider": "sentence-transformers",
            "model_name": name,
            "model_version": getattr(loaded, "max_seq_length", None) or "unknown",
            "dimension": dimension,
            "distance_metric": DISTANCE_METRIC,
            "lexicon_version": None,
            "neural": True,
            "description": "Optional Sentence-Transformers embedding backend.",
        }
    raise ValueError("Unknown embedding model %r." % selected)


def _normalise_vector(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if norm == 0:
        return [float(value) for value in vector]
    return [float(value) / norm for value in vector]


def embed_texts(
    texts: Sequence[str],
    model_key: Optional[str] = None,
) -> List[List[float]]:
    """Embed a batch of texts with the selected model.

    The return value has one vector per input text, in the same order.  An
    empty input returns an empty list (not a fabricated vector).
    """

    selected = (model_key or configured_model_key()).strip()
    if selected == MODEL_KEY:
        return [_embed_hashed(text or "") for text in texts]
    if selected.startswith("sentence-transformers:"):
        name = selected.split(":", 1)[1].strip()
        model = _load_sentence_transformer(name)
        if model is None:
            raise ValueError(
                "Embedding model %r is configured but sentence-transformers is "
                "not installed." % selected
            )
        raw = model.encode(list(texts), normalize_embeddings=True)
        return [_normalise_vector(row) for row in raw]
    raise ValueError("Unknown embedding model %r." % selected)


def embed_text(text: str, model_key: Optional[str] = None) -> List[float]:
    vectors = embed_texts([text or ""], model_key=model_key)
    return vectors[0] if vectors else []


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity for already-normalised or arbitrary vectors."""

    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    dot = sum(float(left[index]) * float(right[index]) for index in range(length))
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left[:length]))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right[:length]))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def build_clause_embedding_text(
    *,
    section_number: str,
    heading: str = "",
    clause_type: str = "",
    text: str = "",
) -> str:
    """Canonical text representation for one clause.

    Section number, heading and type are explicit so the vector captures the
    clause's structural identity as well as its body.  The result is stored in
    the index input hash, which is how an unchanged clause is recognised.
    """

    parts = ["Clause %s" % (section_number or "?")]
    if heading:
        parts.append(str(heading))
    if clause_type:
        parts.append("Type: %s" % clause_type)
    if text:
        parts.append(str(text))
    return " | ".join(parts)


def iter_concepts() -> Iterable[str]:
    return CONCEPTS.keys()
