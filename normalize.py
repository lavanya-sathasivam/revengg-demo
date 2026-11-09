"""Document normalization for extracted contract text.

Normalization cleans up parser and scanning artifacts without changing the
legal meaning of a document.  It is deliberately conservative:

* line endings are normalised to ``\\n``;
* stray form-feeds and vertical-tab artifacts introduced by PDF extraction
  are removed;
* runs of spaces/tabs within a line are collapsed to a single space;
* trailing whitespace on each line is removed;
* completely blank lines that arose from page breaks are collapsed so a
  paragraph is not split into meaningless fragments;
* section numbering, headings and paragraph boundaries are preserved exactly.

Nothing here rewrites, reflows, paraphrases or reorders legal text.  The
normalized output is only ever stored alongside the raw source text so a
reviewer can always compare the two.
"""

import re

# A form feed (page-break marker some PDFs emit) or a vertical tab.
_FORM_FEED = re.compile(r"\f")
_VERTICAL_TAB = re.compile(r"\x0b")
# A carriage return not already part of a CRLF pair.
_CR_NOT_CRLF = re.compile(r"\r(?!\n)")
# Any remaining CRLF or lone CR becomes a single newline.
_CRLF = re.compile(r"\r\n")
# Two or more consecutive newline characters (blank lines within text).
_MULTIPLE_NEWLINES = re.compile(r"\n{3,}")
# Runs of spaces or tabs inside a line.
_SPACE_RUN = re.compile(r"[ \t]{2,}")


def normalize_line_endings(text: str) -> str:
    """Return ``text`` with every line ending normalised to ``\\n``."""
    if text is None:
        return ""
    text = _CRLF.sub("\n", text)
    text = _CR_NOT_CRLF.sub("\n", text)
    return text


def strip_page_break_artifacts(text: str) -> str:
    """Remove form feeds and vertical tabs left behind by PDF extraction."""
    if text is None:
        return ""
    text = _FORM_FEED.sub("\n", text)
    text = _VERTICAL_TAB.sub("", text)
    return text


def collapse_whitespace(text: str) -> str:
    """Collapse intra-line space runs and trim trailing whitespace per line.

    Paragraph boundaries (single newlines) are preserved; only runs of three
    or more newlines (blank-line noise) are collapsed to two so the text
    never loses an intentional paragraph break.
    """
    if text is None:
        return ""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = _SPACE_RUN.sub(" ", line)
        line = line.strip()
        cleaned.append(line)
    text = "\n".join(cleaned)
    text = _MULTIPLE_NEWLINES.sub("\n\n", text)
    return text.strip()


def normalize_text(text: str) -> str:
    """Normalize extracted contract text without changing its legal meaning.

    The result keeps section numbering, headings, and paragraph boundaries
    intact.  Whitespace and page-break artifacts are removed, but no words,
    numbers, or clauses are added, removed, or reordered.
    """
    if text is None:
        return ""
    text = strip_page_break_artifacts(text)
    text = normalize_line_endings(text)
    text = collapse_whitespace(text)
    return text


def normalize_section_number(value) -> str:
    """Normalize a section number for exact comparison.

    Strips whitespace, a leading ``Section`` (case-insensitive) and a
    trailing period, so ``"Section 3."`` and ``"3"`` both become ``"3"``.
    """
    text = str(value if value is not None else "").strip()
    text = re.sub(r"^section\s+", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(".").strip()
