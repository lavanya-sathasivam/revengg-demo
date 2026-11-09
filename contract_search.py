"""Query understanding and hybrid contract retrieval (Phase 4).

The retriever combines three signals over the *authorized, current* clause set:

1. semantic cosine similarity from persisted clause embeddings;
2. BM25 keyword scoring with exact-phrase, clause-number and legal-term
   boosts;
3. a deterministic reranker that uses intent/type fit, heading overlap and
   legal-concept coverage.

Every returned row is a real clause row.  Scores are the computed component
scores and the rank before/after reranking; none are invented or displayed as
probabilities.  Scope is always a set of contract ids supplied by the caller
after the application's firm authorization check.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import db
import embeddings
import retrieval_index

_CLAUSE_REF_RE = re.compile(r"\b(?:clause|section)\s+(\d+(?:\.\d+)*)", re.I)
_BARE_CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s*$")
_QUOTED_RE = re.compile(r'"([^"]{2,200})"|“([^”]{2,200})”')

_GENERIC_QUERY_TERMS = frozenset(
    {
        "contract", "agreement", "document", "clause", "section", "provide",
        "state", "states", "stated", "say", "says", "require", "requires",
        "required", "term", "terms", "provision", "provisions", "party",
        "parties", "please", "tell", "explain", "summarize", "summary",
    }
)

# Query-side legal expansions.  They improve recall for natural-language
# questions without changing the stored clause text or the evidence quotes.
INTENT_EXPANSIONS: Dict[str, str] = {
    "payment": "payment period invoice amount due payable charges settlement",
    "late_payment": "late payment overdue amount late charge interest default",
    "interest": "interest rate per annum accrues late payment",
    "penalty": "late charge late fee penalty liquidated damages",
    "termination": "terminate termination notice convenience default non-payment",
    "default": "default breach failure to pay cure period termination",
    "cure": "cure period remedy failure to pay notice default",
    "notice": "notice written notice deemed served despatch address",
    "service_level": "service level availability uptime performance standard",
    "service_credit": "service credit availability below charges credit",
    "liability": "liability limitation cap aggregate indirect consequential loss",
    "indemnity": "indemnity indemnify hold harmless losses",
    "confidentiality": "confidential information non-disclosure proprietary information",
    "force_majeure": "force majeure beyond reasonable control act of god",
    "governing_law": "governing law jurisdiction laws of india",
    "arbitration": "arbitration arbitrator seat dispute resolution tribunal",
    "dispute": "dispute disputed invoice good faith negotiation withhold payment",
    "assignment": "assignment assign transfer prior written consent successor",
    "amendment": "amendment written signed authorised representative variation",
    "term": "initial term renewal commence effective date duration",
    "tax": "taxes gst vat exclusive of applicable taxes invoice",
    "warranty": "warranty representation warrant",
    "increase": "increase charges percentage prior written notice",
    "scope": "scope services obligations duties performance",
    "definition": "definition means in respect of defined term",
    "exclusivity": "exclusivity exclusive non-compete non-solicit",
    "first_refusal": "right of first refusal right of first offer",
    "data_protection": "data protection personal data privacy gdpr",
    "insurance": "insurance policy coverage public liability professional indemnity",
}

# Which clause types a question intent is likely to be answered by.  This is a
# reranking signal only; it never excludes a clause from retrieval.
INTENT_TYPES: Dict[str, frozenset] = {
    "payment": frozenset({"payment", "fee", "interest", "definition", "obligations"}),
    "late_payment": frozenset({"fee", "interest", "payment", "default", "cure"}),
    "interest": frozenset({"interest", "fee", "payment"}),
    "penalty": frozenset({"fee", "interest", "limitation"}),
    "termination": frozenset({"termination", "default", "notice", "term"}),
    "default": frozenset({"default", "cure", "termination", "payment"}),
    "cure": frozenset({"cure", "default", "notice"}),
    "notice": frozenset({"notice", "termination", "amendment"}),
    "service_level": frozenset({"sla", "fee", "obligations"}),
    "service_credit": frozenset({"sla", "fee", "payment"}),
    "liability": frozenset({"limitation", "liability", "indemnity"}),
    "indemnity": frozenset({"indemnity", "limitation", "liability"}),
    "confidentiality": frozenset({"confidentiality", "indemnity"}),
    "force_majeure": frozenset({"force_majeure", "termination"}),
    "governing_law": frozenset({"governing_law", "dispute"}),
    "arbitration": frozenset({"dispute", "governing_law"}),
    "dispute": frozenset({"dispute", "payment", "governing_law"}),
    "assignment": frozenset({"other", "obligations"}),
    "amendment": frozenset({"other", "obligations", "notice"}),
    "term": frozenset({"term", "termination"}),
    "tax": frozenset({"other", "payment"}),
    "warranty": frozenset({"warranty", "indemnity", "obligations"}),
    "increase": frozenset({"payment", "fee", "notice"}),
    "scope": frozenset({"obligations", "sla", "other"}),
    "definition": frozenset({"definition"}),
    "exclusivity": frozenset({"other", "obligations"}),
    "first_refusal": frozenset({"other", "obligations"}),
    "data_protection": frozenset({"confidentiality", "other"}),
    "insurance": frozenset({"indemnity", "other"}),
}

# Intent concepts that are useful for reranking but too generic to require as
# proof that the question was answered.
NON_REQUIRED_CONCEPTS = frozenset(
    {"time_period", "scope", "definition", "force", "penalty"}
)

DEFAULT_TOP_K = 8
MAX_TOP_K = 20
MIN_EVIDENCE_SCORE = 0.18


def _clause_refs(question: str) -> List[str]:
    refs = {match.group(1) for match in _CLAUSE_REF_RE.finditer(question or "")}
    bare = _BARE_CLAUSE_RE.match(question or "")
    if bare:
        refs.add(bare.group(1))
    return sorted(refs, key=_section_sort_key)


def _section_sort_key(section: str):
    try:
        return tuple(int(part) for part in str(section).split("."))
    except (TypeError, ValueError):
        return (10 ** 6, str(section))


def _quoted_phrases(question: str) -> List[str]:
    phrases = []
    for match in _QUOTED_RE.finditer(question or ""):
        value = match.group(1) or match.group(2) or ""
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            phrases.append(value)
    return phrases


def _concept_scores(question: str) -> List[Tuple[str, float]]:
    normalised = " " + re.sub(r"\s+", " ", (question or "").lower()) + " "
    token_set = set(embeddings.tokenize(question or ""))
    scores: Dict[str, float] = {}
    for concept, entry in embeddings.CONCEPTS.items():
        score = 0.0
        for word in entry.get("words", ()):
            if embeddings._stem(word) in token_set or word in token_set:
                score += 1.0
        for phrase in entry.get("phrases", ()):
            phrase_norm = re.sub(r"\s+", " ", phrase.lower()).strip('"')
            if phrase_norm and phrase_norm in normalised:
                score += 2.0
        if score:
            scores[concept] = score
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def analyze_query(question: str, requested_contract_ids: Optional[Sequence[int]] = None) -> dict:
    """Extract scope, intent, legal terms and retrieval features."""

    normalised = re.sub(r"\s+", " ", (question or "").strip())
    tokens = embeddings.tokenize(normalised)
    meaningful = [token for token in tokens if token not in _GENERIC_QUERY_TERMS]
    concepts = _concept_scores(normalised)
    concept_names = [name for name, _score in concepts]
    primary = [
        name for name in concept_names if name not in NON_REQUIRED_CONCEPTS
    ][:3]
    # Intent drives phrase expansion and clause-type fit only.  It may be a
    # generic concept (for example "scope"), which is why it is selected before
    # the sufficiency-only NON_REQUIRED_CONCEPTS filter.
    intent = concept_names[0] if concept_names else "general"

    refs = _clause_refs(normalised)
    quotes = _quoted_phrases(normalised)
    comparison = bool(
        requested_contract_ids and len(set(requested_contract_ids)) > 1
    ) or bool(re.search(r"\b(compare|comparison|differ|difference|versus|vs\.?)\b", normalised, re.I))

    weighted_terms: Dict[str, float] = {}
    for token in meaningful:
        weighted_terms[token] = max(weighted_terms.get(token, 0.0), 1.0)
    expansions = []
    expansion_concepts = []
    for concept in [intent] + primary:
        if concept != "general" and concept not in expansion_concepts:
            expansion_concepts.append(concept)
    for concept in expansion_concepts:
        expansion = INTENT_EXPANSIONS.get(concept)
        if expansion:
            expansions.append(expansion)
            for token in embeddings.tokenize(expansion):
                weighted_terms[token] = max(weighted_terms.get(token, 0.0), 0.4)
    for ref in refs:
        weighted_terms[ref] = max(weighted_terms.get(ref, 0.0), 1.2)

    expanded_query = " ".join(
        part for part in [normalised, " ".join(expansions), " ".join(refs)] if part
    )
    return {
        "question": normalised,
        "normalised_question": normalised.lower(),
        "tokens": tokens,
        "keywords": meaningful,
        "weighted_terms": weighted_terms,
        "expanded_query": expanded_query,
        "clause_refs": refs,
        "quoted_phrases": quotes,
        "concepts": concept_names,
        "primary_concepts": primary,
        "intent": intent,
        "comparison": comparison,
        "scope": (
            "collection"
            if len(set(requested_contract_ids or [])) > 1
            else "contract"
        ),
    }


def _document_tokens(clause: dict) -> List[str]:
    metadata = clause.get("metadata") or {}
    heading = str(metadata.get("heading") or "")
    clause_type = str(metadata.get("type") or "")
    section = str(clause.get("section_number") or "")
    text = str(clause.get("clause_text") or "")
    tokens = []
    # Structural identity is repeated so a clause-number or heading query can
    # outrank a body mention.
    tokens.extend(embeddings.tokenize("clause %s" % section) * 3)
    tokens.extend(embeddings.tokenize(heading) * 2)
    tokens.extend(embeddings.tokenize(clause_type))
    tokens.extend(embeddings.tokenize(text))
    return tokens


def _bm25_scores(
    documents: Sequence[dict],
    weighted_terms: Dict[str, float],
) -> Dict[str, float]:
    if not documents or not weighted_terms:
        return {key: 0.0 for key in (doc["key"] for doc in documents)}

    tokenised = []
    df: Counter = Counter()
    for document in documents:
        tokens = _document_tokens(document)
        tokenised.append((document["key"], tokens))
        for token in set(tokens):
            df[token] += 1

    total = len(documents)
    avg_length = sum(len(tokens) for _key, tokens in tokenised) / float(total or 1)
    k1 = 1.5
    b = 0.75
    scores: Dict[str, float] = {}
    for key, tokens in tokenised:
        counts = Counter(tokens)
        length = len(tokens) or 1
        score = 0.0
        for term, query_weight in weighted_terms.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            idf = math.log(
                1.0 + (total - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5)
            )
            denominator = frequency + k1 * (
                1.0 - b + b * length / (avg_length or 1.0)
            )
            score += query_weight * idf * (
                frequency * (k1 + 1.0) / (denominator or 1.0)
            )
        scores[key] = score
    return scores


def _bigrams(tokens: Sequence[str]) -> List[Tuple[str, str]]:
    return list(zip(tokens, tokens[1:]))


def _phrase_hits(document: dict, analysis: dict) -> int:
    text = str(document.get("normalised_text") or "").lower()
    hits = 0
    for phrase in analysis.get("quoted_phrases", []):
        phrase_norm = re.sub(r"\s+", " ", phrase.lower()).strip()
        if phrase_norm and phrase_norm in text:
            hits += 2
    query_tokens = [token for token in analysis.get("keywords", []) if len(token) > 2]
    doc_tokens = set(embeddings.tokenize(text))
    for left, right in _bigrams(query_tokens):
        if left in doc_tokens and right in doc_tokens:
            # A loose bigram match is weaker than an exact quoted phrase.
            hits += 1
    return hits


def _heading_overlap(document: dict, keywords: Sequence[str]) -> float:
    heading = str((document.get("metadata") or {}).get("heading") or "")
    heading_tokens = set(embeddings.tokenize(heading))
    if not heading_tokens or not keywords:
        return 0.0
    return len(heading_tokens.intersection(keywords)) / float(len(heading_tokens))


def _source_url(contract_id: int, section: str) -> str:
    from urllib.parse import quote

    return "/explain?contract=%s&clause=%s" % (
        quote(str(contract_id)),
        quote(str(section)),
    )


def _hierarchy_path(row: dict, by_section: Dict[str, dict]) -> List[dict]:
    path = []
    seen = set()
    current = row
    while current is not None and current.get("section_number") not in seen:
        section = str(current.get("section_number") or "")
        seen.add(section)
        metadata = current.get("metadata") or {}
        path.append(
            {
                "section_number": section,
                "heading": metadata.get("heading") or "",
                "clause_id": current.get("clause_id"),
            }
        )
        parent_section = metadata.get("parent_section")
        if not parent_section:
            parent_id = current.get("parent_id")
            if parent_id is None:
                break
            parent = next(
                (
                    candidate
                    for candidate in by_section.values()
                    if candidate.get("clause_id") == parent_id
                ),
                None,
            )
            current = parent
        else:
            current = by_section.get(str(parent_section))
    path.reverse()
    return path


def _load_scope(contract_ids: Sequence[int]) -> Tuple[List[dict], List[dict], dict]:
    """Load live clauses and embeddings for the requested contracts."""

    documents: List[dict] = []
    contracts: List[dict] = []
    storage_summary = {
        "mode": None,
        "memory_vectors": 0,
        "contracts": {},
    }
    for contract_id in contract_ids:
        contract = db.get_contract(contract_id)
        if contract is None:
            continue
        contracts.append(
            {
                "contract_id": contract_id,
                "filename": contract.get("filename"),
                "version_id": contract.get("current_version_id"),
                "processing_status": contract.get("processing_status"),
                "provenance_status": contract.get("provenance_status"),
            }
        )
        version_id = contract.get("current_version_id")
        clauses = db.list_clauses(contract_id)
        if version_id is not None:
            clauses = [
                clause
                for clause in clauses
                if clause.get("version_id") == version_id
            ]
        by_section = {
            str(clause.get("section_number") or ""): clause for clause in clauses
        }
        vectors, storage = retrieval_index.load_current_embeddings(
            contract_id, clauses
        )
        storage_summary["contracts"][contract_id] = storage
        storage_summary["memory_vectors"] += int(
            storage.get("embedded_in_memory") or 0
        )
        mode = storage.get("storage")
        if storage_summary["mode"] is None:
            storage_summary["mode"] = mode
        elif storage_summary["mode"] != mode:
            storage_summary["mode"] = "mixed"

        for clause in clauses:
            metadata = clause.get("metadata") or {}
            section = str(clause.get("section_number") or "")
            documents.append(
                {
                    "key": "%s:%s" % (contract_id, section),
                    "contract_id": contract_id,
                    "filename": contract.get("filename"),
                    "version_id": clause.get("version_id"),
                    "clause_id": clause.get("clause_id"),
                    "section_number": section,
                    "text": clause.get("clause_text") or "",
                    "normalised_text": clause.get("normalized_text")
                    or (clause.get("clause_text") or ""),
                    "metadata": metadata,
                    "heading": metadata.get("heading") or "",
                    "type": metadata.get("type") or "other",
                    "level": metadata.get("level", 1),
                    "page_number": clause.get("page_number")
                    if clause.get("page_number") is not None
                    else metadata.get("page_number"),
                    "char_start": clause.get("char_start")
                    if clause.get("char_start") is not None
                    else metadata.get("char_start"),
                    "char_end": clause.get("char_end")
                    if clause.get("char_end") is not None
                    else metadata.get("char_end"),
                    "parent_id": clause.get("parent_id"),
                    "parent_section": metadata.get("parent_section"),
                    "clause_sha256": clause.get("clause_sha256"),
                    "vector": vectors.get(section, []),
                    "hierarchy": _hierarchy_path(clause, by_section),
                }
            )
    return documents, contracts, storage_summary


def search(
    question: str,
    contract_ids: Sequence[int],
    *,
    top_k: int = DEFAULT_TOP_K,
    model_key: Optional[str] = None,
) -> dict:
    """Run hybrid retrieval over an explicit, already-authorized scope."""

    contract_ids = [int(value) for value in contract_ids]
    top_k = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
    analysis = analyze_query(question, contract_ids)
    documents, contracts, storage = _load_scope(contract_ids)
    if not documents:
        return {
            "question": question,
            "analysis": analysis,
            "contracts": contracts,
            "results": [],
            "count": 0,
            "candidate_count": 0,
            "sufficient": False,
            "insufficient_reason": "no_clauses_in_scope",
            "index": storage,
            "model": embeddings.model_metadata(model_key),
        }

    query_vector = embeddings.embed_text(
        analysis["expanded_query"], model_key=model_key
    )
    semantic_raw: Dict[str, float] = {}
    for document in documents:
        semantic_raw[document["key"]] = (
            embeddings.cosine_similarity(query_vector, document["vector"])
            if document["vector"]
            else 0.0
        )
    keyword_raw = _bm25_scores(documents, analysis["weighted_terms"])

    max_semantic = max([0.0] + list(semantic_raw.values()))
    max_keyword = max([0.0] + list(keyword_raw.values()))
    by_key = {document["key"]: document for document in documents}
    primary = set(analysis["primary_concepts"])
    refs = set(analysis["clause_refs"])
    intent_types = INTENT_TYPES.get(analysis["intent"], frozenset())

    candidates = []
    for document in documents:
        key = document["key"]
        semantic_score = max(0.0, semantic_raw.get(key, 0.0))
        keyword_score = max(0.0, keyword_raw.get(key, 0.0))
        semantic_norm = semantic_score / max_semantic if max_semantic > 0 else 0.0
        keyword_norm = keyword_score / max_keyword if max_keyword > 0 else 0.0
        fusion = 0.55 * semantic_norm + 0.45 * keyword_norm

        concept_text = "%s %s %s" % (
            document["heading"], document["text"], document["type"]
        )
        doc_concepts = set(embeddings.concepts_in_text(concept_text))
        concept_coverage = (
            len(primary.intersection(doc_concepts)) / float(len(primary))
            if primary
            else 0.0
        )
        document_tokens = set(embeddings.tokenize(concept_text))
        keyword_hits = sum(
            1 for token in set(analysis["keywords"]) if token in document_tokens
        )
        phrase_hits = _phrase_hits(document, analysis)
        heading_overlap = _heading_overlap(document, analysis["keywords"])
        clause_ref_hit = document["section_number"] in refs
        type_fit = 1.0 if document["type"] in intent_types else 0.0

        bonus = (
            0.12 * concept_coverage
            + 0.09 * heading_overlap
            + 0.06 * type_fit
            + min(0.12, 0.025 * phrase_hits)
        )
        if clause_ref_hit:
            bonus += 0.85
        rerank_score = fusion + bonus

        candidates.append(
            {
                "key": key,
                "document": document,
                "semantic_score": semantic_score,
                "semantic_normalised": semantic_norm,
                "keyword_score": keyword_score,
                "keyword_normalised": keyword_norm,
                "fusion_score": fusion,
                "concept_coverage": concept_coverage,
                "concept_matches": sorted(primary.intersection(doc_concepts)),
                "keyword_hits": keyword_hits,
                "heading_overlap": heading_overlap,
                "type_fit": type_fit,
                "phrase_hits": phrase_hits,
                "clause_ref_hit": clause_ref_hit,
                "bonus": bonus,
                "rerank_score": rerank_score,
            }
        )

    # Rerank a generous candidate window, not only the first top_k.  This keeps
    # an exact clause-number hit that BM25 ranked low from being discarded.
    candidates.sort(key=lambda item: item["rerank_score"], reverse=True)
    window = candidates[: max(top_k * 5, 40)]
    for rank, candidate in enumerate(window, start=1):
        candidate["rank_before_rerank"] = rank
    window.sort(
        key=lambda item: (
            item["rerank_score"],
            item["semantic_score"],
            item["keyword_score"],
        ),
        reverse=True,
    )

    results = []
    for final_rank, candidate in enumerate(window[:top_k], start=1):
        document = candidate["document"]
        results.append(
            {
                "rank": final_rank,
                "rank_before_rerank": candidate["rank_before_rerank"],
                "contract_id": document["contract_id"],
                "filename": document["filename"],
                "version_id": document["version_id"],
                "clause_id": document["section_number"],
                "section_number": document["section_number"],
                "clause_db_id": document["clause_id"],
                "heading": document["heading"],
                "type": document["type"],
                "level": document["level"],
                "page_number": document["page_number"],
                "char_start": document["char_start"],
                "char_end": document["char_end"],
                "parent_section": document["parent_section"],
                "hierarchy": document["hierarchy"],
                "clause_sha256": document["clause_sha256"],
                "text": document["text"],
                "source_url": _source_url(
                    document["contract_id"], document["section_number"]
                ),
                "scores": {
                    "semantic": round(candidate["semantic_score"], 6),
                    "semantic_normalised": round(
                        candidate["semantic_normalised"], 6
                    ),
                    "keyword": round(candidate["keyword_score"], 6),
                    "keyword_normalised": round(
                        candidate["keyword_normalised"], 6
                    ),
                    "fusion": round(candidate["fusion_score"], 6),
                    "bonus": round(candidate["bonus"], 6),
                    "rerank": round(candidate["rerank_score"], 6),
                },
                "match": {
                    "concepts": candidate["concept_matches"],
                    "concept_coverage": round(candidate["concept_coverage"], 4),
                    "type_fit": bool(candidate["type_fit"]),
                    "phrase_hits": candidate["phrase_hits"],
                    "keyword_hits": candidate["keyword_hits"],
                    "clause_reference": candidate["clause_ref_hit"],
                },
            }
        )

    top = results[0] if results else None
    sufficient = bool(results) and top["scores"]["rerank"] >= MIN_EVIDENCE_SCORE
    insufficient_reason = None
    if not results:
        sufficient = False
        insufficient_reason = "no_results"
    elif not sufficient:
        insufficient_reason = "below_relevance_threshold"
    elif primary:
        # The best-ranked clause itself must support the asked-about legal
        # concept.  A lower-ranked clause that happens to contain the word is
        # not enough: negative questions ("is there an exclusivity clause?")
        # otherwise latch onto incidental wording such as "exclusive of
        # taxes".  An explicit clause number is itself direct evidence.
        exact_ref = bool(top["match"]["clause_reference"])
        if not set(top["match"]["concepts"]).intersection(primary) and not exact_ref:
            sufficient = False
            insufficient_reason = "question_concept_not_supported"
    else:
        # With no recognised legal concept, require a real lexical match in
        # the top clause (or an explicit clause reference).  Semantic rank
        # alone would make any vaguely worded question look answerable.
        exact_ref = bool(top["match"]["clause_reference"])
        if not top["match"].get("keyword_hits") and not exact_ref:
            sufficient = False
            insufficient_reason = "no_lexical_evidence"

    return {
        "question": question,
        "analysis": analysis,
        "contracts": contracts,
        "results": results,
        "count": len(results),
        "candidate_count": len(candidates),
        "sufficient": sufficient,
        "insufficient_reason": insufficient_reason,
        "index": storage,
        "model": embeddings.model_metadata(model_key),
        "thresholds": {
            "minimum_rerank_score": MIN_EVIDENCE_SCORE,
        },
    }
