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

import extract

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
    "termination": ["terminate this agreement", "right to terminate", "termination"],
    "sla": ["service level", "availability", "uptime", "service credit"],
    "limitation": ["limitation of liability", "in no event", "aggregate liability"],
    "indemnity": ["indemnif", "hold harmless"],
    "force_majeure": ["force majeure", "beyond its reasonable control"],
    "governing_law": ["governed by the laws", "governing law", "laws of india"],
    "dispute": ["arbitration", "arbitral", "dispute resolution", "tribunal"],
    "warranty": ["warrant", "representation"],
    "confidentiality": ["confidential", "non-disclosure", "proprietary information"],
    "definition": ["shall mean", "means", "definition", "interpretation"],
    "term": ["initial term", "renewal term", "term of this agreement", "renews automatically"],
    "payment": ["payment period", "payment due date", "payable", "invoice", "charges"],
    "notice": ["notices", "deemed served", "notice shall be", "written notice"],
}

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
    r"within\s+(\d+)\s*(?:\([^)]*\)\s*)?(day|days|month|months)\b", re.IGNORECASE
)


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


def split_clauses(text):
    """Fine-grained clause list, ready for db.save_clauses().

    Returns a list of:
        {"section_number": str, "clause_text": str, "metadata": {...}}

    Top-level section detection is delegated to extract.split_clauses()
    (Person A's tested PDF-aware splitter). We then subdivide each section
    into its numbered sub-clauses.

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

        # the section anchor itself (heading only - its sub-clauses carry the text)
        anchor_heading = _clean_heading(
            re.sub(r"^\s*\d+(\.\d+)*\.?\s*", "", first_line).strip()
        ) or first_line.strip()[:70]
        out.append({
            "section_number": sec_no,
            "clause_text": first_line.strip(),
            "metadata": {"heading": anchor_heading, "level": 0,
                         "source": "section"},
        })

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
# classification
# ---------------------------------------------------------------------------
def classify_type(text, heading=""):
    """Assign one clause type.

    Rule 1: if the HEADING names the type, trust the heading. A clause headed
            'Termination for Non-Payment' is a termination clause even though
            its body also mentions the word 'default'.
    Rule 2: otherwise fall back to keyword priority over heading + body.

    Deterministic on purpose: a lawyer reviewing the system must be able to
    see WHY a clause was typed the way it was.
    """
    h = (heading or "").strip()

    if h.startswith('"'):
        return "definition"

    if h and len(h) <= 60:
        for ctype in TYPE_PRIORITY:
            if ctype == "other":
                break
            for kw in TYPE_KEYWORDS.get(ctype, []):
                if kw in h.lower():
                    return ctype

    low = ("%s %s" % (h, text)).lower()
    for ctype in TYPE_PRIORITY:
        if ctype == "other":
            break
        for kw in TYPE_KEYWORDS.get(ctype, []):
            if kw in low:
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
    """Every number+unit in the text. e.g. [{'value':30,'unit':'days'}]"""
    out = []
    for m in NUM_RE.finditer(text):
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
            value = float(m.group(1))
        except ValueError:
            continue
        out.append({"value": int(value) if value == int(value) else value,
                    "unit": unit})
    return out


def find_within_days(text):
    """The first 'within N days' deadline in the text, or None."""
    m = WITHIN_RE.search(text)
    if not m:
        return None
    if m.group(2).lower().startswith("month"):
        return None
    return int(m.group(1))


def find_quoted_terms(text):
    """Capitalised terms in double quotes, e.g. 'Payment Due Date'."""
    return re.findall(r'"([^"]{3,60})"', text)


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
