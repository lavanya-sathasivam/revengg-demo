"""Fine-grained clause analysis: hierarchy, classification, numbers, references.

Owner: Person B (branch b-graph).

RELATIONSHIP TO extract.py (Person A's module)
    extract.py answers: "where does the text of each TOP-LEVEL section end?"
    It is PDF-aware and fails safe to [].

    This module answers: "what are the numbered sub-clauses inside each
    section, what type is each one, what numbers does it state, and which
    other clauses does it refer to?"

    split_clauses() BUILDS ON extract.split_clauses() - it never re-implements
    PDF handling or top-level section detection. If A improves extract.py,
    this module gets that improvement for free.

WHY SUB-CLAUSE LEVEL MATTERS
    "7.2 Payment Period" and "7" are different legal objects. A change to 7.2
    (30 days -> 15 days) is not a change to section 7. The impact analysis
    needs the fine-grained view, so that is what we store.
"""

import re
from decimal import Decimal, InvalidOperation

import extract

# Recorded on every stored contract version so a later change to the splitter
# can be distinguished from a change to the source document.
SPLITTER_VERSION = "phase5.1"

# ---------------------------------------------------------------------------
# vocabularies (frozen - do not rename without telling A and C)
# ---------------------------------------------------------------------------
CLAUSE_TYPES = [
    "definition", "payment", "fee", "interest", "default", "cure", "termination",
    "notice", "sla", "confidentiality", "indemnity", "limitation", "force_majeure",
    "governing_law", "dispute", "warranty", "term", "other",
]

# TYPE PRIORITY - the FIRST type whose keyword appears wins.
# Ordered most-specific first. 'payment' and 'notice' sit near the end
# because words like "payment" and "notice" appear all over a contract.
TYPE_PRIORITY = [
    "fee", "interest", "default", "cure", "termination", "sla", "limitation",
    "indemnity", "force_majeure", "governing_law", "dispute", "warranty",
    "confidentiality", "definition", "term", "payment", "notice", "other",
]

TYPE_KEYWORDS = {
    "fee": ["late charge", "late fee", "additional charge", "service credit",
            "liquidated damages", "penalty"],
    "interest": ["interest at", "per annum", "interest shall accrue", "accrue interest",
                 "interest"],
    "default": ["in default", "event of default", "shall be in default", "default"],
    "cure": ["cure period", "cure that failure", "cure the failure", "to cure"],
    # NOTE: a bare 'termination' is NOT a keyword. Clause 17.4 says 'shall
    # survive termination' - it mentions termination but grants no
    # termination right. A termination clause has to actually terminate.
    "termination": ["terminate this agreement", "right to terminate",
                    "termination of this agreement", "termination for",
                    "termination upon"],
    "sla": ["service level", "availability", "uptime", "service credit"],
    "limitation": ["limitation of liability", "in no event", "aggregate liability"],
    "indemnity": ["indemnity", "indemnify", "hold harmless"],
    "force_majeure": ["force majeure", "beyond its reasonable control"],
    "governing_law": ["governed by the laws", "governing law", "laws of india"],
    "dispute": ["arbitration", "arbitral", "dispute resolution", "tribunal"],
    "warranty": ["warrant", "representation"],
    "confidentiality": ["confidential", "non-disclosure", "proprietary information"],
    # NOTE: 'interpretation' is not a keyword. 'definition' means 'defines a
    # term'. Clause 2.1 ('Interpretation of Headings') defines no term.
    "definition": ["shall mean", "means", "definition"],
    "term": ["initial term", "renewal term", "term of this agreement", "renews automatically"],
    "payment": ["payment period", "payment due date", "payable", "invoice", "charge"],
    "notice": ["notices", "deemed served", "notice shall be", "written notice"],
}

# HEADING-ONLY cues.
#
# A heading NAMES the subject of a clause; the body merely mentions things.
# So 'payment' is a safe cue for a heading ('7. PAYMENT') but not for body
# text, where the word 'payment' appears in half the contract.
# These are checked ONLY against the heading, in the same priority order.
HEADING_CUES = {
    "payment": ["payment"],
    "fee": ["late payment"],
    "term": ["term"],
    "termination": ["termination"],
}

# A heading that names a subject the frozen 18-type vocabulary has no type
# for. A tax clause is 'other' - it is not a payment clause just because it
# mentions an Invoice.
TAX_HEADING_RE = re.compile(r"^(tax|taxes|gst|vat)$", re.IGNORECASE)

# A schedule is a document that happens to contain obligations, not an
# operative clause of the agreement.
SCHEDULE_ONLY_RE = re.compile(r"^SCHEDULE\b", re.IGNORECASE)

# '7.2 Payment Period. The Customer shall pay...'  (a SUB-clause: needs a dot)
SUB_CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\.?\s+(\S.*)$")
# 'SCHEDULE A - SERVICES, CHARGES AND ADDRESSES'
SCHEDULE_RE = re.compile(r"^\s*(SCHEDULE\s+[A-Z])\b\s*[-–]?\s*(.*)$")
# 'Clause 7.2'  /  'Clauses 7.2 and 10.1'
REF_SINGLE_RE = re.compile(r"\b[Cc]lause\s+(\d+(?:\.\d+)*)")
REF_PLURAL_RE = re.compile(r"\b[Cc]lauses\s+(\d+(?:\.\d+)*)\s*(?:,|and|&)\s*(\d+(?:\.\d+)*)")
# '30 days', '30 (thirty) days', '12 (twelve) months', '1.5%', '99%'
# No trailing \b after the unit, otherwise '99%' never matches (a '%' is not a
# word character, so there is no word boundary after it).
NUM_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:\([^)]*\)\s*)?(days?|months?|years?|%|per\s*cent)",
    re.IGNORECASE,
)
# 'within 30 (thirty) days' - the operative deadline, not any number at all
WITHIN_RE = re.compile(
    r"within\s+(\d+)\s*(?:\([^)]*\)\s*)?(day|days|month|months|year|years)\b",
    re.IGNORECASE,
)
# 'within 30 Business Days' and 'within 10 working days' use a different
# calendar convention. The engine performs no business-day conversion, so a
# period expressed this way is reported as ambiguous rather than guessed.
WITHIN_BUSINESS_RE = re.compile(
    r"within\s+(\d+)\s*(?:\([^)]*\)\s*)?(?:business|working)\s+days?\b",
    re.IGNORECASE,
)
# 'within thirty days' - an unambiguous number word. Only simple English
# numbers are supported; an unknown word fails closed rather than guessing.
WITHIN_WORD_RE = re.compile(
    r"within\s+([A-Za-z]+(?:[\s-]+[A-Za-z]+)?)\s+"
    r"(day|days|month|months|year|years)\b",
    re.IGNORECASE,
)
_WORD_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}

# Units that a duration parser understands without conversion. Money and
# percentages are deliberately absent: they are values, not durations.
DURATION_UNITS = frozenset({"days", "months", "years"})


# ---------------------------------------------------------------------------
# headings
# ---------------------------------------------------------------------------
def _clean_heading(text):
    """'Payment Period. The Customer shall pay' -> 'Payment Period'."""
    text = text.strip()
    m = re.match(r'^("([^"]+)")', text)
    if m:
        return m.group(1)
    m = re.match(r"^([A-Z][A-Za-z /&'\-]{2,70}?)\.(?:\s|$)", text)
    if m:
        return m.group(1).strip()
    return text[:70].strip()


def sec_sort_key(sec_no):
    """Sort '2.10' after '2.9', not after '2.1'."""
    try:
        return tuple(int(p) for p in str(sec_no).split("."))
    except ValueError:
        return (10 ** 6,)


# ---------------------------------------------------------------------------
# hierarchy splitting
# ---------------------------------------------------------------------------
def _split_hierarchy(section_text):
    """Split one top-level section into its numbered sub-clauses.

    Returns a list of {'section_number', 'heading', 'text'}.
    """
    blocks = []
    current = None

    for raw in section_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if current is not None:
                current["text"] += "\n"
            continue

        sm = SCHEDULE_RE.match(line)
        if sm:
            if current is not None:
                blocks.append(current)
            current = {"section_number": sm.group(1).upper(),
                       "heading": sm.group(2).strip()[:70],
                       "text": line + "\n"}
            continue

        m = SUB_CLAUSE_RE.match(line)
        if m and len(line) <= 400:
            if current is not None:
                blocks.append(current)
            body = m.group(2).strip()
            current = {"section_number": m.group(1).rstrip("."),
                       "heading": _clean_heading(body),
                       "text": body + "\n"}
        else:
            if current is None:
                current = {"section_number": "", "heading": "", "text": ""}
            current["text"] += line + "\n"

    if current is not None:
        blocks.append(current)

    for b in blocks:
        b["text"] = b["text"].strip()
    return blocks


def split_clauses(text, preserve_text=False):
    """Fine-grained clause list, ready for db.save_clauses().

    Returns a list of:
        {"section_number": str, "clause_text": str, "metadata": {...}}

    Top-level section detection is delegated to extract.split_clauses()
    (Person A's tested PDF-aware splitter). We then subdivide each section
    into its numbered sub-clauses.

    With preserve_text=True, each row keeps its original source slice,
    including clause numbering, and text before the first nested clause stays
    with its parent section. This mode is used by the PDF upload path.

    Returns [] if the top-level structure cannot be detected - we never
    invent structure.
    """
    sections = extract.split_clauses(text)
    if not sections:
        return []

    out = []
    for section in sections:
        sec_no = section["section_number"]
        body = section["clause_text"]
        first_line = body.splitlines()[0] if body.splitlines() else ""

        # In the demo ingestion mode, the section anchor is heading-only and
        # its sub-clauses carry the operative text. Upload mode retains the
        # parent prefix as its own source slice so no unnumbered text is lost.
        anchor_heading = _clean_heading(
            re.sub(r"^\s*\d+(\.\d+)*\.?\s*", "", first_line).strip()
        ) or first_line.strip()[:70]
        if preserve_text:
            lines = body.splitlines(keepends=True)
            starts = []
            offset = 0
            for line in lines:
                starts.append(offset)
                offset += len(line)
            child_starts = []
            for line_index, line in enumerate(lines):
                if len(line.rstrip("\r\n").rstrip()) > 400:
                    continue
                match = SUB_CLAUSE_RE.match(line.rstrip("\r\n"))
                if match:
                    child_starts.append((line_index, match))
            parent_end = starts[child_starts[0][0]] if child_starts else len(body)
            parent_text = body[:parent_end]
        else:
            child_starts = []
            parent_text = first_line.strip()
        out.append({
            "section_number": sec_no,
            "clause_text": parent_text,
            "metadata": {"heading": anchor_heading, "level": 0,
                         "source": "section"},
        })

        if preserve_text:
            for position, (line_index, match) in enumerate(child_starts):
                start = starts[line_index]
                end = (starts[child_starts[position + 1][0]]
                       if position + 1 < len(child_starts) else len(body))
                clause_number = match.group(1).rstrip(".")
                clause_body = match.group(2).strip()
                out.append({
                    "section_number": clause_number,
                    "clause_text": body[start:end],
                    "metadata": {"heading": _clean_heading(clause_body),
                                 "level": 1, "source": "sub_clause"},
                })
        else:
            for sub in _split_hierarchy(body):
                if not sub["section_number"]:
                    continue
                out.append({
                    "section_number": sub["section_number"],
                    "clause_text": sub["text"],
                    "metadata": {"heading": sub["heading"], "level": 1,
                                 "source": "sub_clause"},
                })

    # de-duplicate section numbers, keeping the first occurrence
    seen = set()
    unique = []
    for item in out:
        key = item["section_number"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# keyword matching
# ---------------------------------------------------------------------------
# Keywords are matched with WORD BOUNDARIES and a light suffix tolerance.
#
# Why: without boundaries, 'term' also matches 'termination', and 'charge'
# also matches 'charged'. That makes it impossible to add a precise cue like
# the bare word 'payment' to the heading rules - it would fire on every
# clause that merely mentions payment.
def _suffix_forms(word):
    """'charge' -> charge, charges, charging, charged. 'penalty' -> penalty,
    penalties, ..."""
    forms = {word}
    if word.endswith("y") and len(word) > 1:
        forms.add(word[:-1] + "ies")
    else:
        forms.add(word + "s")
    if word.endswith("e"):
        forms.add(word[:-1] + "ing")
        forms.add(word + "d")
    else:
        forms.add(word + "ing")
        forms.add(word + "ed")
    return sorted(forms)


_KEYWORD_CACHE = {}


def _keyword_re(keyword):
    """Compile one keyword phrase into a word-boundary regex."""
    if keyword not in _KEYWORD_CACHE:
        words = keyword.split()
        parts = [re.escape(w) for w in words[:-1]]
        parts.append("(?:%s)" % "|".join(re.escape(f) for f in _suffix_forms(words[-1])))
        _KEYWORD_CACHE[keyword] = re.compile(
            r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)
    return _KEYWORD_CACHE[keyword]


def _has_keyword(text, ctype, table):
    """Does this text contain any keyword of this clause type?"""
    for kw in table.get(ctype, []):
        if _keyword_re(kw).search(text):
            return True
    return False


def _states_a_level(text):
    """Does this clause commit to a MEASURABLE service level?

    Clause 3.2 says 'in accordance with the Service Levels' - it points at
    the SLAs but states none. Typing it 'sla' puts the obligation in the
    wrong clause; the SLA clause is 9.1, which states 99%.
    """
    low = (text or "").lower()
    return ("%" in low) or ("availability" in low) or ("uptime" in low)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
def classify_type(text, heading=""):
    """Assign one clause type.

    Rule 1: if the HEADING names the subject, trust the heading. A clause
            headed 'Termination for Non-Payment' is a termination clause even
            though its body also mentions the word 'default'.
    Rule 1b: a heading that names a subject the vocabulary has no type for
            (a tax clause) is 'other'.
    Rule 2: otherwise fall back to keyword priority over the body.

    Deterministic on purpose: a lawyer reviewing the system must be able to
    see WHY a clause was typed the way it was.
    """
    h = (heading or "").strip()
    body = text or ""

    # '"Payment Due Date" means ...' - a quoted heading is a definition.
    if h.startswith('"'):
        return "definition"

    # A schedule is a document, not an operative clause.
    if SCHEDULE_ONLY_RE.match(body.strip()):
        return "other"

    # Rule 1: the heading.
    if h and len(h) <= 60:
        for ctype in TYPE_PRIORITY:
            if ctype == "other":
                break
            if _has_keyword(h, ctype, TYPE_KEYWORDS) \
                    or _has_keyword(h, ctype, HEADING_CUES):
                return ctype

        # Rule 1b: the heading names something we have no type for.
        if TAX_HEADING_RE.match(h):
            return "other"

    # Rule 2: the body.
    for ctype in TYPE_PRIORITY:
        if ctype == "other":
            break
        if _has_keyword(body, ctype, TYPE_KEYWORDS):
            if ctype == "sla" and not _states_a_level(body):
                continue
            return ctype

    return "other"


# ---------------------------------------------------------------------------
# extraction of structure
# ---------------------------------------------------------------------------
def find_references(text):
    """Clause numbers this clause explicitly mentions. e.g. ['7.2', '10.1']."""
    refs = set()
    for m in REF_SINGLE_RE.finditer(text):
        refs.add(m.group(1))
    for m in REF_PLURAL_RE.finditer(text):
        refs.add(m.group(1))
        refs.add(m.group(2))
    return sorted(refs)


def find_numbers(text):
    """Every number+unit in the text, as exact Decimal values.

    Example: [{'value': Decimal('30'), 'unit': 'days'}]. Decimal is used
    because a legal amount or duration must not acquire binary floating-point
    error before it is compared with a stored clause value.
    """
    out = []
    for m in NUM_RE.finditer(text or ""):
        unit = m.group(2).lower()
        if unit.startswith("per"):
            unit = "%"
        elif unit.startswith("day"):
            unit = "days"
        elif unit.startswith("month"):
            unit = "months"
        elif unit.startswith("year"):
            unit = "years"
        try:
            value = Decimal(m.group(1))
        except (InvalidOperation, ValueError):
            continue
        if not value.is_finite():
            continue
        out.append({"value": value, "unit": unit})
    return out


def _parse_word_period(value_text):
    """'thirty' -> 30; 'twenty-five' -> 25. Unknown words return None."""
    words = re.split(r"[\s-]+", (value_text or "").strip().lower())
    if len(words) == 1:
        return _WORD_UNITS.get(words[0])
    if len(words) == 2:
        tens = _WORD_UNITS.get(words[0])
        ones = _WORD_UNITS.get(words[1])
        if tens in (20, 30, 40, 50, 60, 70, 80, 90) and 1 <= (ones or 0) <= 9:
            return tens + ones
    return None


def find_within_period(text):
    """Read a 'within N days/months/years' period, failing closed on ambiguity.

    Returns None when no period is stated. Otherwise returns a dict with:
        days       int for a single supported day period, else None
        unit       normalised unit ('days', 'months', 'business_days')
        supported  True only when days is a usable calendar-day count
        ambiguous  True when the clause states competing conventions/counts
        reason     a safe explanation when unsupported or ambiguous

    Both digits and simple number words are accepted ('within 30 days',
    'within thirty days'). No conversion is performed. Months, years, business
    days, and working days are returned as unsupported; two different 'within'
    periods are ambiguous. The caller must not pick one silently.
    """
    text = text or ""
    business = list(WITHIN_BUSINESS_RE.finditer(text))
    numeric = list(WITHIN_RE.finditer(text))
    word_matches = list(WITHIN_WORD_RE.finditer(text))

    if business:
        return {
            "days": None,
            "unit": "business_days",
            "supported": False,
            "ambiguous": True,
            "reason": (
                "The clause states a business-day or working-day period. "
                "No business-day calendar is configured and no conversion to "
                "calendar days is performed."
            ),
        }

    periods = [
        (int(m.group(1)), m.group(2).lower()) for m in numeric
    ]
    for match in word_matches:
        value = _parse_word_period(match.group(1))
        if value is not None:
            periods.append((value, match.group(2).lower()))
    if not periods:
        return None

    units = sorted({unit for _value, unit in periods})
    values = sorted({value for value, _unit in periods})
    if len(units) > 1 or len(values) > 1:
        descriptions = []
        for match in numeric:
            descriptions.append(
                "within %s %s" % (match.group(1), match.group(2).lower())
            )
        for match in word_matches:
            if _parse_word_period(match.group(1)) is not None:
                descriptions.append(
                    "within %s %s" % (match.group(1).lower(),
                                      match.group(2).lower())
                )
        return {
            "days": None,
            "unit": ",".join(units),
            "supported": False,
            "ambiguous": True,
            "reason": (
                "The clause states more than one competing 'within' period "
                "(%s). The operative period cannot be selected safely."
                % ", ".join(descriptions[:4])
            ),
        }

    raw_unit = units[0]
    if raw_unit.startswith("month"):
        return {
            "days": None,
            "unit": "months",
            "supported": False,
            "ambiguous": False,
            "reason": (
                "The clause states a period in months. No month length or "
                "month-end convention is configured, so no conversion to days "
                "is performed."
            ),
        }
    if raw_unit.startswith("year"):
        return {
            "days": None,
            "unit": "years",
            "supported": False,
            "ambiguous": False,
            "reason": (
                "The clause states a period in years. No year length or "
                "month-end convention is configured, so no conversion to days "
                "is performed."
            ),
        }

    return {
        "days": values[0],
        "unit": "days",
        "supported": True,
        "ambiguous": False,
        "reason": None,
    }


def within_period_error(text):
    """A safe rejection message for an ambiguous 'within' period, else None."""
    period = find_within_period(text)
    if period is not None and period.get("ambiguous"):
        return period["reason"]
    return None


def find_within_days(text):
    """The first usable calendar-day 'within N days' deadline, or None.

    A clause with competing periods, a business-day period, or a month/year
    period returns None. Callers must not invent a conversion.
    """
    period = find_within_period(text)
    if period is None or not period.get("supported"):
        return None
    return period["days"]


# A whole-word 'dispute'. Substring matching would also match 'undisputed',
# which appears in every payment clause.
DISPUTE_RE = re.compile(r"\bdisput(?:e|es|ed|ing)\b", re.IGNORECASE)


def mentions_dispute(text):
    """Does this clause actually talk about a dispute?

    'each undisputed Invoice' does NOT count. Clause 7.2 has no
    dispute-notice obligation; only Clause 7.3 does.
    """
    return bool(DISPUTE_RE.search(text or ""))


def find_quoted_terms(text):
    """Capitalised terms in double quotes, e.g. 'Payment Due Date'."""
    return re.findall(r'"([^"]{3,60})"', text)


# 'INR 4,50,000 per month' - money. The number parser above only understands
# days / months / years / %, so rupee amounts need their own rule.
AMOUNT_RE = re.compile(r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)

# 'Service credits for availability below 99%: 5% of the monthly Charges for
#  each full percentage point below 99%, capped at 25% of the monthly Charges.'
CREDIT_THRESHOLD_RE = re.compile(r"below\s+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
CREDIT_RATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s+of the monthly charges",
                             re.IGNORECASE)
CREDIT_CAP_RE = re.compile(r"capped at\s+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)


def find_amounts(text):
    """Rupee amounts stated in the text, as exact Decimal values.

    'INR 4,50,000' -> [Decimal('450000')]. Money is never parsed as a
    duration and never converted to a day count.
    """
    out = []
    for m in AMOUNT_RE.finditer(text or ""):
        digits = m.group(1).replace(",", "")
        try:
            value = Decimal(digits)
        except (InvalidOperation, ValueError):
            continue
        if not value.is_finite():
            continue
        out.append(value)
    return out


def parse_service_credit_schedule(text):
    """Read the service-credit mechanics out of a schedule.

    Returns {'threshold_pct', 'rate_pct', 'cap_pct'} or None. None means
    'we could not read it' - which is honest, and must never be guessed into
    a number.
    """
    if not text:
        return None
    threshold = CREDIT_THRESHOLD_RE.search(text)
    rate = CREDIT_RATE_RE.search(text)
    cap = CREDIT_CAP_RE.search(text)
    if not (threshold and rate and cap):
        return None
    try:
        return {
            "threshold_pct": Decimal(threshold.group(1)),
            "rate_pct": Decimal(rate.group(1)),
            "cap_pct": Decimal(cap.group(1)),
        }
    except (InvalidOperation, ValueError):
        return None


def defined_terms_by_clause(text):
    """Terms this clause DEFINES (as opposed to merely uses)."""
    out = []
    for term in find_quoted_terms(text):
        if ("means" in text.lower() or '(the "%s")' % term in text
                or '("%s")' % term in text):
            out.append(term)
    return out


def enrich(clause):
    """Everything we know about one clause, in one dict."""
    text = clause.get("clause_text") or clause.get("text", "")
    heading = clause.get("heading", "")
    return {
        "sec_no": clause.get("section_number") or clause.get("sec_no", ""),
        "heading": heading,
        "type": classify_type(text, heading),
        "text": text,
        "references": find_references(text),
        "numbers": find_numbers(text),
        "terms": find_quoted_terms(text),
        "defines": defined_terms_by_clause(text),
    }
