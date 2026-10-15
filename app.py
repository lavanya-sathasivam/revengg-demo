import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile

from db import get_conn, init_db
from extract import extract_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"

# Reasonable upload size limit (10 MB)
MAX_UPLOAD_SIZE = 10 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024  # copy uploads in 1 MB chunks to avoid big memory use


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts: create tables + uploads folder.
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Smart Contract Intelligence API",
    description="Backend API for contract upload, storage, and extraction.",
    version="0.1.0",
    lifespan=lifespan,
)


def sanitize_filename(filename: str) -> str:
    """Return a safe filename: no folders, only simple characters, .pdf kept."""
    name = os.path.basename(filename or "")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "contract.pdf"


def save_upload(upload: UploadFile) -> tuple[str, str]:
    """
    Stream the uploaded file to uploads/ in 1 MB chunks.

    Returns (saved_path, safe_original_name). Raises HTTPException on empty
    filename or if the file exceeds MAX_UPLOAD_SIZE (partial file is removed).
    """
    original_name = sanitize_filename(upload.filename)
    if not original_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    unique_name = f"{uuid.uuid4().hex[:12]}_{original_name}"
    saved_path = UPLOAD_DIR / unique_name

    total = 0
    try:
        with open(saved_path, "wb") as out:
            while True:
                chunk = upload.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Maximum allowed size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
                    )
                out.write(chunk)
    except HTTPException:
        saved_path.unlink(missing_ok=True)
        raise
    except OSError:
        saved_path.unlink(missing_ok=True)
        logger.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Could not save the uploaded file.")

    if total == 0:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    return str(saved_path), original_name


def save_contract_with_trace(
    filename: str, status: str, raw_text: str | None, action: str, details: str
) -> int:
    """
    Insert one contract row plus one trace row in a single transaction.

    If anything fails, the transaction is rolled back so no partial record
    is left behind. Returns the new contract_id.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            """INSERT INTO contracts
               (filename, upload_timestamp, processing_status, raw_text)
               VALUES (?, ?, ?, ?)""",
            (filename, now, status, raw_text),
        )
        contract_id = cur.lastrowid
        cur.execute(
            """INSERT INTO traces (contract_id, action, details, timestamp)
               VALUES (?, ?, ?, ?)""",
            (contract_id, action, details, now),
        )
        conn.commit()
        return contract_id
    except Exception:
        conn.rollback()
        logger.exception("Database error while saving contract record")
        raise
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    """Simple health check to confirm the API is running."""
    return {"status": "ok"}


@app.post("/upload")
def upload_contract(request: Request, file: UploadFile = File(...)) -> dict:
    """
    Accept a PDF contract, extract its text, and store both in SQLite.

    On success: stores status 'extracted' + trace, returns contract_id.
    On extraction failure (e.g. scanned/empty PDF): stores status
    'extraction_failed' + trace, then returns HTTP 422 with a clear message.
    """
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_SIZE + 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum allowed size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
        )

    saved_path, original_name = save_upload(file)

    try:
        text = extract_text(saved_path)
    except (FileNotFoundError, ValueError) as exc:
        # Extraction failed: record the failure, then tell the client clearly.
        try:
            save_contract_with_trace(
                filename=original_name,
                status="extraction_failed",
                raw_text=None,
                action="upload",
                details=f"Text extraction failed for {original_name}: {exc}",
            )
        except Exception:
            logger.exception("Could not record extraction failure")
            raise HTTPException(status_code=500, detail="Could not save the contract record.")
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract text from '{original_name}': {exc}",
        )
    except Exception:
        logger.exception("Unexpected error during PDF extraction")
        raise HTTPException(status_code=500, detail="Unexpected error while processing the PDF.")

    try:
        contract_id = save_contract_with_trace(
            filename=original_name,
            status="extracted",
            raw_text=text,
            action="upload",
            details=f"Uploaded and extracted text from {original_name} ({len(text)} characters).",
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Could not save the contract record.")

    return {
        "message": "Contract uploaded and text extracted successfully.",
        "contract_id": contract_id,
        "filename": original_name,
        "status": "extracted",
        "extracted_characters": len(text),
    }
