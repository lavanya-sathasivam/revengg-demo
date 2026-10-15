import os
import re

import pymupdf  # PyMuPDF

# --- Detection rules for split_clauses() -------------------------------------
# 1. A candidate heading line starts with a 1-2 digit number followed by '.'
#    (lettered subsections A./B. can never match this pattern).
# 2. It must show an ALL-CAPS heading word (3+ capital letters) either on the
#    same line after the number, or - when the number sits alone on its line -
#    on the next non-blank line within _LOOKAHEAD_LINES lines
#    (e.g. '3.' followed by 'TERM. This Agreement ...').
#    Sentence-case list items ('Total invoice/payment requested;') fail this
#    check and are never treated as sections.
# 3. Everything from an 'IN WITNESS WHEREOF' signature marker onwards is not
#    scanned, so signature-block items ('1.  CONSULTANT', '3. APPROVED AS TO
#    INSURANCE:') never become candidates.
# 4. Among surviving candidates only the longest run of strictly consecutive
#    numbers that STARTS AT 1 is accepted; a non-restarting gap after that run
#    means the document is ambiguous and [] is returned instead of guessing.
_HEADING_LINE_RE = re.compile(r"^[ \t]*(\d{1,2})\.[ \t]*(.*)$")
_CAPS_WORD_RE = re.compile(r"^[A-Z][A-Z0-9&'-]*\.?$")
_SIGNATURE_RE = re.compile(r"^\s*IN\s+WITNESS\s+WHEREOF", re.IGNORECASE)
_LOOKAHEAD_LINES = 3


def extract_text(pdf_path):
    """
    Extract text from a PDF file, page by page.

    Returns a single string containing all page text joined with newlines
    between pages. Raises clear exceptions on missing, invalid, or unreadable
    PDFs. Raises ValueError when no extractable text is found (e.g. scanned
    PDFs that require OCR).
    """
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {os.path.basename(pdf_path)}")

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise ValueError(
            f"Unable to open PDF. The file may be corrupted or not a valid PDF: {os.path.basename(pdf_path)}"
        ) from exc

    try:
        pages = []
        for page in doc:
            pages.append(page.get_text("text"))
        text = "\n".join(pages).strip()
        if not text:
            raise ValueError(
                f"No extractable text found in PDF: {os.path.basename(pdf_path)}. "
                "The PDF may be a scanned document and require OCR, which is "
                "outside the initial scope."
            )
        return text
    except ValueError:
        # Re-raise the clear "no extractable text" error unchanged
        raise
    except Exception as exc:
        raise ValueError(
            f"Failed while reading pages from PDF: {os.path.basename(pdf_path)}"
        ) from exc
    finally:
        # Always close the document, even if an error occurred above
        doc.close()


def _is_caps_heading(fragment: str) -> bool:
    """
    True when the FIRST word of a fragment is an ALL-CAPS heading word.

    The word must start with an uppercase letter, contain only uppercase
    letters, digits and heading punctuation (& ' , . -), and have at least
    3 letters. This accepts 'DUTIES.', 'TERM.', 'EARLY', 'REBATES,' ...
    and rejects sentence-case list items such as 'Labor', 'Total',
    "CONSULTANT's" (lowercase) and single letters like 'A.'.
    """
    words = fragment.split()
    if not words:
        return False
    token = words[0]
    if not re.fullmatch(r"[A-Z][A-Z0-9&',.\-]*", token):
        return False
    letters = [ch for ch in token if ch.isalpha()]
    return len(letters) >= 3 and all(ch.isupper() for ch in letters)


def split_clauses(text):
    """
    Split extracted contract text into top-level numbered clauses.

    Returns a list of {"section_number": str, "clause_text": str} dicts, or []
    (a clearly defined failure result) when no reliable sequence exists.

    Detection combines three signals (never numbering alone):
      1. STRUCTURE - a heading line looks like `N.` with an ALL-CAPS heading
         word either on the same line, or (for bare `3.` lines) on the next
         non-blank line within _LOOKAHEAD_LINES lines. Lettered lines
         (`A.`, `B.`, `I.`) can never match. Scanning stops at an
         'IN WITNESS WHEREOF' signature marker, so signature-block items
         are never considered.
      2. FORMAT   - the heading word must be ALL-CAPS (>=3 letters), which
         rejects sentence-case numbered invoice lists.
      3. NUMBERING- candidates must form a strictly consecutive sequence
         STARTING AT 1. Numbers below the expected next section are ignored
         (nested/restarted numbering); a forward gap means the sequence is
         unreliable and [] is returned instead of guessing or silently
         dropping sections.

    Clause text is taken by slicing the ORIGINAL string at character offsets;
    nothing is normalized, reconstructed, or rejoined. Text before section 1
    (preamble) and text from the signature marker onward are not part of any
    clause.

    Known limitations: headings written in mixed/sentence case are not
    detected (this fails safe to [] via the sequence rule); a nested list
    item that is itself numbered consecutively AND in ALL-CAPS could be
    mistaken for a section - plain text cannot distinguish these without
    font/layout information.
    """
    if not text or not text.strip():
        return []

    lines = text.split("\n")
    line_starts = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1  # +1 for the '\n' separator

    # Never scan at/after the signature marker (document-structure guard).
    scan_end = len(lines)
    for i, line in enumerate(lines):
        if _SIGNATURE_RE.match(line):
            scan_end = i
            break

    # Pass 1: collect heading candidates that pass the ALL-CAPS format rule.
    candidates = []  # (line_index, section_number)
    for i in range(scan_end):
        m = _HEADING_LINE_RE.match(lines[i])
        if not m:
            continue
        number = int(m.group(1))
        heading_fragment = m.group(2)
        if not heading_fragment.strip():
            # Number alone on its line: heading may be on the next line(s).
            heading_fragment = ""
            for j in range(i + 1, min(i + 1 + _LOOKAHEAD_LINES, scan_end)):
                if lines[j].strip():
                    heading_fragment = lines[j]
                    break
        if heading_fragment and _is_caps_heading(heading_fragment):
            candidates.append((i, number))

    # Pass 2: accept only the strictly consecutive sequence starting at 1.
    accepted = []
    expected = 1
    for line_idx, number in candidates:
        if number == expected:
            accepted.append((line_idx, number))
            expected += 1
        elif number < expected:
            continue  # nested or restarted numbering inside a section
        else:
            return []  # forward gap -> unreliable sequence, defined failure

    if not accepted:
        return []

    # Pass 3: slice the ORIGINAL text by character offsets (exact preservation).
    seq_end = line_starts[scan_end] if scan_end < len(lines) else len(text)
    clauses = []
    for k, (line_idx, number) in enumerate(accepted):
        start = line_starts[line_idx]
        if k + 1 < len(accepted):
            end = line_starts[accepted[k + 1][0]]
        else:
            end = seq_end  # last clause ends before the signature marker
        clauses.append(
            {"section_number": str(number), "clause_text": text[start:end]}
        )
    return clauses
