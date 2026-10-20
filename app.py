import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from db import (
    get_conn,
    get_contract,
    init_db,
    list_clauses,
    list_contracts,
    save_clauses,
)
import adapter
import clauses as clause_tools
from extract import extract_text
import graph
from reasoning import explain_with_retrieval

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
TEMPLATES_DIR = BASE_DIR / "templates"
INDEX_HTML = TEMPLATES_DIR / "index.html"
# Person C's clause-explainer UI, kept as its own page. The landing page is
# now the impact-analysis view (templates/index.html).
EXPLAIN_HTML = TEMPLATES_DIR / "explain.html"

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


def mark_clause_failure(contract_id: int, details: str) -> None:
    """
    Best-effort correction when clause persistence fails after the contract
    row was already committed as 'extracted'.

    Contract storage and clause storage are two separate transactions, so we
    cannot make them atomic without changing db.py. Instead of leaving a
    misleading 'extracted' status, update the contract's processing_status to
    'clause_persistence_failed' and append a trace row. This is best effort:
    if it fails too, we log and continue so the original error is preserved.
    """
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            cur.execute(
                "UPDATE contracts SET processing_status = ? WHERE contract_id = ?",
                ("clause_persistence_failed", contract_id),
            )
            cur.execute(
                """INSERT INTO traces (contract_id, action, details, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (contract_id, "clause_extraction", details, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "Could not record clause-persistence failure for contract %s", contract_id
        )


def mark_graph_failure(contract_id: int, details: str) -> bool:
    """Clear a partial graph and record that contract analysis did not finish.

    Graph writes currently commit incrementally. Clearing this contract's
    graph is therefore best-effort recovery, not a transaction spanning the
    whole build. The status and trace are recorded together where possible.
    """
    try:
        adapter.clear_graph(contract_id)
    except Exception:
        logger.exception("Could not clear partial graph for contract %s", contract_id)

    recorded = False
    try:
        conn = get_conn()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.cursor()
            cur.execute(
                "UPDATE contracts SET processing_status = ? WHERE contract_id = ?",
                ("graph_build_failed", contract_id),
            )
            cur.execute(
                """INSERT INTO traces (contract_id, action, details, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (contract_id, "graph_construction", details, now),
            )
            conn.commit()
            recorded = True
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "Could not record graph-build failure for contract %s", contract_id
        )
    return recorded


def _normalize_section_number(value) -> str:
    """Normalize a section number for exact comparison.

    Strips whitespace, a leading 'section' (case-insensitive) and trailing
    period, so 'Section 3.' and '3' both become '3'.
    """
    text = str(value if value is not None else "").strip()
    text = re.sub(r"^section\s+", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(".").strip()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the static demo UI from templates/index.html."""
    if not INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="UI template not found")
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/explain", include_in_schema=False)
def explain_page() -> FileResponse:
    """Serve Person C's clause-explainer UI from templates/explain.html."""
    if not EXPLAIN_HTML.is_file():
        raise HTTPException(status_code=404, detail="UI template not found")
    return FileResponse(EXPLAIN_HTML, media_type="text/html")


@app.get("/health")
def health() -> dict:
    """Simple health check to confirm the API is running."""
    return {"status": "ok"}


@app.post("/upload")
def upload_contract(request: Request, file: UploadFile = File(...)) -> dict:
    """
    Accept a PDF contract, extract its text, and store both in SQLite.

    On success: stores status 'extracted' + trace, splits the text into
    clauses, classifies them, builds the contract graph, and returns its ID
    and processing summary.
    On extraction failure (e.g. scanned/empty PDF): stores status
    'extraction_failed' + trace, then returns HTTP 422 with a clear message.
    Clauses are only ever saved after a successful extraction.
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
        # No clauses are saved on this path.
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

    # Split the extracted text into clauses. split_clauses never invents
    # sections: it returns [] when no reliable sequence is found, and an
    # empty list is stored as "no clauses" rather than fabricated content.
    clauses = clause_tools.split_clauses(text, preserve_text=True)
    for clause in clauses:
        metadata = dict(clause.get("metadata") or {})
        metadata["type"] = clause_tools.classify_type(
            clause.get("clause_text") or "", metadata.get("heading", "")
        )
        clause["metadata"] = metadata

    try:
        saved_clause_count = save_clauses(contract_id, clauses)
    except Exception:
        # Contract row was already committed as 'extracted', so flip its
        # status and log a trace instead of returning a misleading success.
        logger.exception("Failed to persist clauses for contract %s", contract_id)
        mark_clause_failure(
            contract_id,
            f"Stored {len(text)} characters but failed to save clauses for {original_name}.",
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Contract text was stored, but its clauses could not be saved. "
                f"The contract (id={contract_id}) is marked 'clause_persistence_failed'."
            ),
        )

    try:
        graph_stats = graph.build_graph(contract_id, use_llm=False)
    except Exception as exc:
        logger.exception("Failed to build graph for contract %s", contract_id)
        failure_recorded = mark_graph_failure(
            contract_id,
            f"Stored {len(text)} characters and {saved_clause_count} clauses, "
            f"but graph construction failed for {original_name}: {exc}",
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Contract text and clauses were saved, but analysis could not "
                f"be completed. The contract (id={contract_id}) "
                + (
                    "is marked 'graph_build_failed'."
                    if failure_recorded
                    else "could not be marked 'graph_build_failed'."
                )
            ),
        )

    if saved_clause_count == 0:
        logger.warning(
            "No clauses detected for contract %s (%s); contract kept as 'extracted'.",
            contract_id,
            original_name,
        )
        clause_status = "no_clauses_detected"
        message = (
            "Contract uploaded and text extracted, but no numbered clauses "
            "were detected; analysis is unavailable."
        )
    else:
        clause_status = "ok"
        message = (
            "Contract uploaded, clauses classified and stored, and analysis "
            "is ready."
        )

    return {
        "message": message,
        "contract_id": contract_id,
        "filename": original_name,
        "status": "extracted",
        "extracted_characters": len(text),
        "clauses_saved": saved_clause_count,
        "clause_split_status": clause_status,
        "analysis_status": "ready" if saved_clause_count else "unavailable",
        "graph": graph_stats,
    }


@app.get("/contracts")
def list_contracts_route() -> list:
    """Return a lightweight list of all stored contracts (no extracted text)."""
    return list_contracts()


@app.get("/contracts/{contract_id}")
def get_contract_route(contract_id: int) -> dict:
    """Return one stored contract's metadata and extracted text by ID."""
    contract = get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    return contract


@app.get("/contracts/{contract_id}/clauses")
def get_contract_clauses_route(contract_id: int) -> dict:
    """
    Return the stored clauses for one contract, ordered by section number.

    Returns 404 for an unknown contract, and an empty list (not an error)
    for a valid contract that has no stored clauses.
    """
    contract = get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    return {"contract_id": contract_id, "clauses": list_clauses(contract_id)}


@app.get("/contracts/{contract_id}/clauses/{section_number}/explanation")
def get_clause_explanation_route(contract_id: int, section_number: str) -> dict:
    """
    Return a deterministic, template-based explanation for one stored clause.

    The requested section number is matched against the contract's stored
    clauses using the same normalization for both sides: surrounding
    whitespace is stripped and a leading 'Section ' prefix and trailing '.'
    are removed, so '3', ' 3 ', 'Section 3' and '3.' all resolve to the
    stored section '3'. Matching is exact (not a prefix/substring match), so
    '3' will not match '30'.

    Returns 404 if the contract does not exist, or if no stored clause for
    that contract has the requested section number.
    """
    contract = get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")

    requested = _normalize_section_number(section_number)
    match = None
    for clause in list_clauses(contract_id):
        if _normalize_section_number(clause.get("section_number")) == requested:
            match = clause
            break

    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"Clause with section number '{section_number}' not found for this contract",
        )

    return explain_with_retrieval(match.get("clause_text") or "")
