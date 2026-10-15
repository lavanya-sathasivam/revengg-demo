import os

import pymupdf  # PyMuPDF


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
