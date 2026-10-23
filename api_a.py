"""Contract upload, retrieval, and clause-explanation routes.

The route handlers live here so that the shared application factory can mount
one canonical router. app.py remains the historical import path and re-exports
these names for existing callers.
"""

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

import audit
import auth
import clauses as clause_tools
import db
from db import (
    get_conn,
    list_clauses,
    save_clauses,
)
from extract import extract_text
import graph
from reasoning import explain_with_retrieval

logger = logging.getLogger("app")

# Reasonable upload size limit (10 MB)
MAX_UPLOAD_SIZE = 10 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024  # copy uploads in 1 MB chunks to avoid big memory use

router = APIRouter(tags=["contracts"])


def sanitize_filename(filename: str) -> str:
    """Return a safe filename: no folders, only simple characters, .pdf kept."""
    name = os.path.basename(filename or "")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "contract.pdf"


def _file_sha256(path) -> str:
    """Hash a saved upload in chunks without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _actor_fields(principal):
    if principal is not None and principal.authenticated and not principal.demo:
        return {"actor_user_id": principal.user_id, "actor_type": "user"}
    return {"actor_type": "system"}


def _upload_settings(request: Request):
    """Return settings only when this application may write uploads.

    Missing state also fails closed: a handler invoked outside the shared
    application must never assume that writing is allowed.
    """
    settings = getattr(getattr(request, "app", None), "state", None)
    settings = getattr(settings, "settings", None)
    if (
        settings is None
        or not settings.uploads_enabled
        or settings.database_read_only
        or settings.upload_dir is None
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Uploads are disabled because this application is using the "
                "read-only synthetic demo database. Start upload mode with an "
                "explicit database path to accept uploads."
            ),
        )
    return settings


def save_upload(upload: UploadFile, upload_dir: Path) -> tuple[str, str]:
    """
    Stream the uploaded file to upload_dir/ in 1 MB chunks.

    Returns (saved_path, safe_original_name). Raises HTTPException on empty
    filename or if the file exceeds MAX_UPLOAD_SIZE (partial file is removed).
    """
    original_name = sanitize_filename(upload.filename)
    if not original_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    unique_name = f"{uuid.uuid4().hex[:12]}_{original_name}"
    saved_path = Path(upload_dir) / unique_name

    total = 0
    try:
        saved_path.parent.mkdir(parents=True, exist_ok=True)
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
    filename: str,
    status: str,
    raw_text: str | None,
    action: str,
    details: str,
    firm_id: int | None = None,
    *,
    provenance_status=db.PROVENANCE_UNVERIFIED,
    created_by=None,
    source_path=None,
    source_sha256=None,
    source_size_bytes=None,
    source_mime=None,
    extracted_text_sha256=None,
    extraction_status=db.EXTRACTION_STATUS_SUCCEEDED,
    version_status=db.VERSION_STATUS_DRAFT,
    clause_splitter_version=None,
    audit_action=None,
    audit_outcome="success",
    correlation_id=None,
) -> int:
    """
    Insert one contract row, its first version, and one trace in one transaction.

    If anything fails, the transaction is rolled back so no partial record
    is left behind. Returns the new contract_id. ``firm_id`` is always derived
    from the authenticated session by the upload route, never from the client.

    The source hash, path, size, MIME type, extraction status and splitter
    version are stored on the version row, not on the contract identity.  A
    failed extraction still gets a version (status ``failed``) so the failed
    attempt has provenance without being reported as a successful import.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        now = db.utc_now_iso()
        cur.execute(
            """INSERT INTO contracts
               (filename, upload_timestamp, processing_status, raw_text,
                firm_id, provenance_status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                filename,
                now,
                status,
                raw_text,
                firm_id,
                provenance_status,
            ),
        )
        contract_id = cur.lastrowid
        version_id = db.insert_version_row(
            cur,
            contract_id,
            firm_id=firm_id,
            created_by=created_by,
            source_filename=filename,
            source_path=source_path,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            source_mime=source_mime,
            extracted_text_sha256=extracted_text_sha256,
            extraction_status=extraction_status,
            clause_splitter_version=clause_splitter_version,
            provenance_status=provenance_status,
            source_kind="upload",
            status=version_status,
        )
        cur.execute(
            """UPDATE contracts
               SET current_version_id = ?, provenance_status = ?
               WHERE contract_id = ?""",
            (version_id, provenance_status, contract_id),
        )
        cur.execute(
            """INSERT INTO traces (contract_id, action, details, timestamp)
               VALUES (?, ?, ?, ?)""",
            (contract_id, action, details, now),
        )
        if audit_action:
            audit.record_event(
                conn=conn,
                commit=False,
                action=audit_action,
                outcome=audit_outcome,
                firm_id=firm_id,
                actor_user_id=created_by,
                actor_type="user" if created_by is not None else "system",
                target_type="contract",
                target_id=contract_id,
                correlation_id=correlation_id,
                metadata={
                    "filename": filename,
                    "processing_status": status,
                    "version_id": version_id,
                    "source_sha256": source_sha256,
                    "source_size_bytes": source_size_bytes,
                    "source_mime": source_mime,
                    "extraction_status": extraction_status,
                },
            )
        conn.commit()
        return contract_id
    except Exception:
        conn.rollback()
        logger.exception("Database error while saving contract record")
        raise
    finally:
        conn.close()


def mark_clause_failure(
    contract_id: int,
    details: str,
    *,
    principal=None,
    firm_id=None,
    error_type=None,
    correlation_id=None,
) -> bool:
    """
    Best-effort correction when clause persistence fails after the contract
    row was already committed as 'extracted'.

    The draft version is marked failed so it can never be published by a
    later save. The contract status becomes 'clause_persistence_failed' and a
    trace plus audit event are recorded. This is best effort: if it fails too,
    we log and continue so the original error is preserved.
    """
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            now = db.utc_now_iso()
            cur.execute(
                "UPDATE contracts SET processing_status = ? WHERE contract_id = ?",
                ("clause_persistence_failed", contract_id),
            )
            cur.execute(
                """UPDATE contract_versions SET status = ?
                   WHERE version_id = (
                       SELECT current_version_id FROM contracts
                       WHERE contract_id = ?
                   ) AND status IN (?, ?)""",
                (
                    db.VERSION_STATUS_FAILED,
                    contract_id,
                    db.VERSION_STATUS_DRAFT,
                    db.VERSION_STATUS_EXTRACTED,
                ),
            )
            cur.execute(
                """INSERT INTO traces (contract_id, action, details, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (contract_id, "clause_extraction", details, now),
            )
            audit.record_event(
                conn=conn,
                commit=False,
                swallow=True,
                action="clauses.persist_failed",
                outcome="failure",
                firm_id=(
                    firm_id
                    if firm_id is not None
                    else getattr(principal, "firm_id", None)
                ),
                target_type="contract",
                target_id=contract_id,
                correlation_id=correlation_id,
                metadata={
                    "error_type": error_type,
                    "reason": "clause_persistence_failed",
                },
                **_actor_fields(principal),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "Could not record clause-persistence failure for contract %s", contract_id
        )
        return False


def mark_graph_failure(
    contract_id: int,
    details: str,
    *,
    principal=None,
    firm_id=None,
    error_type=None,
    correlation_id=None,
) -> bool:
    """Record that graph construction failed without deleting a good graph.

    replace_graph() publishes atomically, so a failed build has already left
    the previous published nodes and edges untouched. Clearing them here would
    destroy the last known-good graph; this function therefore only updates the
    contract status and records trace/audit evidence.
    """
    recorded = False
    try:
        conn = get_conn()
        try:
            now = db.utc_now_iso()
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
            audit.record_event(
                conn=conn,
                commit=False,
                swallow=True,
                action="contract.graph_status_failed",
                outcome="failure",
                firm_id=(
                    firm_id
                    if firm_id is not None
                    else getattr(principal, "firm_id", None)
                ),
                target_type="contract",
                target_id=contract_id,
                correlation_id=correlation_id,
                metadata={
                    "error_type": error_type,
                    "reason": "graph_build_failed",
                },
                **_actor_fields(principal),
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


@router.get("/health")
def health() -> dict:
    """Simple health check to confirm the API is running."""
    return {"status": "ok"}


@router.post("/upload")
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
    settings = _upload_settings(request)
    principal = auth.principal_from_request(request)
    firm_id = auth.require_upload_permission(principal)

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_SIZE + 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum allowed size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
        )

    saved_path, original_name = save_upload(file, settings.upload_dir)

    try:
        source_sha256 = _file_sha256(saved_path)
        source_size_bytes = os.path.getsize(saved_path)
    except OSError:
        Path(saved_path).unlink(missing_ok=True)
        logger.exception("Could not hash the saved upload %s", saved_path)
        raise HTTPException(
            status_code=500, detail="Could not read the uploaded file after saving it."
        )

    correlation_id = uuid.uuid4().hex
    source_mime = "application/pdf"

    try:
        text = extract_text(saved_path)
    except (FileNotFoundError, ValueError) as exc:
        # Extraction failed: record the failed version and trace, then tell the
        # client clearly. No clauses are saved on this path.
        try:
            save_contract_with_trace(
                filename=original_name,
                status="extraction_failed",
                raw_text=None,
                action="upload",
                details=f"Text extraction failed for {original_name}: {exc}",
                firm_id=firm_id,
                provenance_status=db.PROVENANCE_VERIFIED,
                created_by=principal.user_id,
                source_path=saved_path,
                source_sha256=source_sha256,
                source_size_bytes=source_size_bytes,
                source_mime=source_mime,
                extracted_text_sha256=None,
                extraction_status=db.EXTRACTION_STATUS_FAILED,
                version_status=db.VERSION_STATUS_FAILED,
                audit_action="contract.extraction_failed",
                audit_outcome="failure",
                correlation_id=correlation_id,
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
            firm_id=firm_id,
            provenance_status=db.PROVENANCE_VERIFIED,
            created_by=principal.user_id,
            source_path=saved_path,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            source_mime=source_mime,
            extracted_text_sha256=db.sha256_text(text),
            extraction_status=db.EXTRACTION_STATUS_SUCCEEDED,
            version_status=db.VERSION_STATUS_DRAFT,
            clause_splitter_version=clause_tools.SPLITTER_VERSION,
            audit_action="contract.created",
            correlation_id=correlation_id,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Could not save the contract record.")

    version_id = db.get_current_version_id(contract_id)
    if version_id is None:
        logger.error("Contract %s has no current version after intake", contract_id)
        mark_clause_failure(
            contract_id,
            f"Contract text was stored for {original_name}, but no version was created.",
            principal=principal,
            firm_id=firm_id,
            error_type="MissingContractVersion",
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Contract text was stored, but its version record is missing. "
                f"The contract (id={contract_id}) is marked 'clause_persistence_failed'."
            ),
        )

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
        saved_clause_count = save_clauses(
            contract_id,
            clauses,
            version_id=version_id,
            audit_event={
                "action": "clauses.persisted",
                "outcome": "success",
                "firm_id": firm_id,
                "actor_user_id": principal.user_id,
                "actor_type": "user",
                "target_type": "contract",
                "target_id": contract_id,
                "correlation_id": correlation_id,
                "metadata": {
                    "filename": original_name,
                    "source_sha256": source_sha256,
                    "clause_count": len(clauses),
                    "processing_status": "extracted",
                },
            },
        )
    except Exception as exc:
        # Contract row and version were already committed as 'extracted', so
        # flip the status and mark the draft version failed instead of
        # returning a misleading success.
        logger.exception("Failed to persist clauses for contract %s", contract_id)
        mark_clause_failure(
            contract_id,
            f"Stored {len(text)} characters but failed to save clauses for {original_name}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Contract text was stored, but its clauses could not be saved. "
                f"The contract (id={contract_id}) is marked 'clause_persistence_failed'."
            ),
        )

    try:
        graph_stats = graph.build_graph(
            contract_id,
            use_llm=False,
            audit_context={
                "firm_id": firm_id,
                "actor_user_id": principal.user_id,
                "actor_type": "user",
                "correlation_id": correlation_id,
                "filename": original_name,
            },
        )
    except Exception as exc:
        logger.exception("Failed to build graph for contract %s", contract_id)
        failure_recorded = mark_graph_failure(
            contract_id,
            f"Stored {len(text)} characters and {saved_clause_count} clauses, "
            f"but graph construction failed for {original_name}: {exc}",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
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


@router.get("/contracts")
def list_contracts_route(request: Request = None) -> list:
    """Return only contracts visible in the request's active scope."""
    principal = auth.principal_from_request(request)
    return auth.authorized_contracts(principal)


@router.get("/contracts/{contract_id}")
def get_contract_route(contract_id: int, request: Request = None) -> dict:
    """Return one stored contract's metadata and extracted text by ID."""
    principal = auth.principal_from_request(request)
    return auth.authorized_contract(contract_id, principal)


@router.get("/contracts/{contract_id}/clauses")
def get_contract_clauses_route(contract_id: int, request: Request = None) -> dict:
    """
    Return the stored clauses for one contract, ordered by section number.

    Returns 404 for an unknown contract or a contract outside the active firm,
    and an empty list (not an error) for an authorized contract with no clauses.
    """
    principal = auth.principal_from_request(request)
    auth.authorized_contract(contract_id, principal)
    return {"contract_id": contract_id, "clauses": list_clauses(contract_id)}


@router.get("/contracts/{contract_id}/clauses/{section_number}/explanation")
def get_clause_explanation_route(
    contract_id: int, section_number: str, request: Request = None
) -> dict:
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
    principal = auth.principal_from_request(request)
    auth.authorized_contract(contract_id, principal)

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
