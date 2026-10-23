import os
import re
from time import monotonic as _monotonic

import pymupdf  # PyMuPDF

PDF_MAGIC = b"%PDF-"


class PDFValidationError(ValueError):
    """A PDF was rejected for a defined, client-safe reason.

    ``code`` is stable enough to use in audit metadata; ``public_message`` is
    safe to return to the caller and never contains parser internals, file
    paths, or document text.
    """

    code = "pdf_validation_failed"
    status_code = 422
    public_message = "The PDF could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ):
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.public_message = message or self.public_message
        super().__init__(self.public_message)


class UnsupportedFileTypeError(PDFValidationError):
    code = "not_a_pdf"
    # A .pdf-named upload with non-PDF bytes has passed the extension check and
    # is treated as a malformed PDF, not as an unsupported extension.
    status_code = 422
    public_message = "The uploaded file does not contain PDF data."


class MalformedPDFError(PDFValidationError):
    code = "malformed_pdf"
    public_message = (
        "The PDF is malformed or structurally damaged and cannot be processed."
    )


class EncryptedPDFError(PDFValidationError):
    code = "encrypted_pdf"
    public_message = (
        "Encrypted PDFs are not supported. Upload an unencrypted, "
        "text-selectable PDF."
    )


class PDFPageLimitError(PDFValidationError):
    code = "page_limit_exceeded"
    status_code = 413
    public_message = "The PDF exceeds the configured page limit."


class PDFTextLimitError(PDFValidationError):
    code = "text_limit_exceeded"
    status_code = 413
    public_message = "The PDF text exceeds the configured processing limit."


class PDFProcessingTimeoutError(PDFValidationError):
    code = "processing_timeout"
    public_message = "PDF processing exceeded the configured time limit."


class PDFExtractionFailedError(PDFValidationError):
    code = "extraction_failed"
    public_message = "Text could not be extracted from the PDF."


class NoExtractableTextError(PDFValidationError):
    code = "no_extractable_text"
    public_message = (
        "No usable extractable text was found. The PDF may be a scanned "
        "document and require OCR, which is outside the current scope."
    )


def _check_positive_limit(name: str, value):
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be greater than zero.")


def _check_deadline(deadline):
    if deadline is not None and _monotonic() > deadline:
        raise PDFProcessingTimeoutError()


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


def extract_text(
    pdf_path,
    *,
    max_pages=None,
    max_extracted_chars=None,
    max_processing_seconds=None,
):
    """Extract text from a text-selectable PDF, with bounded processing.

    Validation is layered: the file must start with the PDF magic bytes, the
    parser must open it, it must not be encrypted or structurally repaired, it
    must contain at least one page, and it must yield usable text.  Optional
    limits cap the page count, the accumulated extracted characters, and the
    elapsed processing time.

    The time limit is cooperative: it is checked before opening, between
    pages, and after the last page.  It cannot interrupt a single hung parser
    call; a hard preemptive timeout would require a separate worker process.

    Raises a ``PDFValidationError`` subclass for defined rejections.  Messages
    contain no parser internals, no file paths, and no document text.
    """
    _check_positive_limit("max_pages", max_pages)
    _check_positive_limit("max_extracted_chars", max_extracted_chars)
    _check_positive_limit("max_processing_seconds", max_processing_seconds)

    path = os.fspath(pdf_path)
    if not os.path.isfile(path):
        raise FileNotFoundError("PDF file not found.")

    deadline = None
    if max_processing_seconds is not None:
        deadline = _monotonic() + max_processing_seconds
    _check_deadline(deadline)

    # Read the already size-bounded staged file once and parse from memory.
    # Passing a path to PyMuPDF can leave a Windows file handle behind when a
    # malformed document fails to open, which would prevent deleting rejected
    # bytes. The upload route enforces the size limit before this point.
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise PDFExtractionFailedError() from exc
    if not data.startswith(PDF_MAGIC):
        raise UnsupportedFileTypeError()
    _check_deadline(deadline)

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise MalformedPDFError() from exc

    try:
        # Reject every encrypted document, including one that would open with
        # an empty password.  Do not attempt password authentication.
        if doc.needs_pass or getattr(doc, "is_encrypted", False):
            raise EncryptedPDFError()

        page_count = doc.page_count
        if page_count <= 0:
            raise NoExtractableTextError()
        if max_pages is not None and page_count > max_pages:
            raise PDFPageLimitError(
                f"The PDF has {page_count} pages; the maximum allowed is "
                f"{max_pages}."
            )

        # A structurally repaired file is malformed input.  Rejecting it
        # avoids treating a partially reconstructed document as authoritative.
        if getattr(doc, "is_repaired", False):
            raise MalformedPDFError()

        parts = []
        total_chars = 0
        for index in range(page_count):
            _check_deadline(deadline)
            try:
                page_text = doc.load_page(index).get_text("text")
            except Exception as exc:
                raise PDFExtractionFailedError() from exc

            separator = 1 if parts else 0
            total_chars += len(page_text) + separator
            if (
                max_extracted_chars is not None
                and total_chars > max_extracted_chars
            ):
                raise PDFTextLimitError(
                    "The PDF text exceeds the configured maximum of "
                    f"{max_extracted_chars} characters."
                )
            parts.append(page_text)

        _check_deadline(deadline)
        text = "\n".join(parts).strip()
        if not text or not any(character.isalnum() for character in text):
            raise NoExtractableTextError()
        return text
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFExtractionFailedError() from exc
    finally:
        # Always close the document, even if an error occurred above.
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
