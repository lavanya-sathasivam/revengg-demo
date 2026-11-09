"""Contract upload, retrieval, and clause-explanation routes.

The route handlers live here so that the shared application factory can mount
one canonical router. app.py remains the historical import path and re-exports
these names for existing callers.
"""

import hashlib
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

import audit
import auth
import clauses as clause_tools
import db
import ingest
from app_config import DEFAULT_UPLOAD_MAX_BYTES
from db import get_conn, list_clauses, save_clauses
from extract import PDFValidationError, extract_text
from reasoning import explain_with_retrieval

logger = logging.getLogger("app")

# The historical constant remains for callers that import it. The actual
# per-request value comes from AppSettings and is configurable.
MAX_UPLOAD_SIZE = DEFAULT_UPLOAD_MAX_BYTES
CHUNK_SIZE = 1024 * 1024  # copy uploads in 1 MB chunks to avoid big memory use
TEMP_PREFIX = ".upload-"
TEMP_SUFFIX = ".part"

router = APIRouter(tags=["contracts"])


class UploadValidationError(HTTPException):
    """An HTTP rejection with a stable reason code for audit metadata."""

    def __init__(self, status_code: int, detail: str, *, reason: str):
        super().__init__(status_code=status_code, detail=detail)
        self.reason = reason


@dataclass(frozen=True)
class StagedUpload:
    """A validated-size upload in temporary storage, not yet published."""

    temp_path: Path
    original_name: str
    size_bytes: int
    sha256: str


def sanitize_filename(filename: str) -> str:
    """Return a safe display filename: no folders, only simple characters.

    The result is metadata only. It is never used as a filesystem path; storage
    names are generated independently.
    """
    name = str(filename or "").replace("\\", "/")
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if len(name) > 255:
        stem, dot, suffix = name.rpartition(".")
        if dot and 0 < len(suffix) <= 10:
            name = stem[: 255 - len(suffix) - 1] + "." + suffix
        else:
            name = name[:255]
    return name or "contract.pdf"


def _format_size_limit(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024:
        return f"{max_bytes // (1024 * 1024)} MB"
    if max_bytes >= 1024:
        return f"{max_bytes // 1024} KB"
    return f"{max_bytes} bytes"


def _is_within_root(root, candidate) -> bool:
    try:
        root_path = os.path.normcase(str(Path(root).resolve()))
        candidate_path = os.path.normcase(str(Path(candidate).resolve()))
        return os.path.commonpath([root_path, candidate_path]) == root_path
    except (OSError, ValueError):
        return False


def _confined_path(root, candidate) -> Path:
    if not _is_within_root(root, candidate):
        raise UploadValidationError(
            500,
            "The configured upload storage is not usable.",
            reason="storage_path_invalid",
        )
    return Path(candidate).resolve()


def _firm_storage_dir(root: Path, firm_id: int) -> Path:
    if not isinstance(firm_id, int) or isinstance(firm_id, bool):
        raise UploadValidationError(
            500,
            "The configured upload storage is not usable.",
            reason="storage_firm_invalid",
        )
    return _confined_path(root, Path(root) / ("firm-%d" % firm_id))


def _delete_file(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove an upload file")


def cleanup_stale_upload_temps(upload_dir) -> int:
    """Remove abandoned staging files on startup.

    Only files created by this application's staging prefix are removed. The
    database and accepted documents are never touched.
    """
    root = Path(upload_dir)
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob(TEMP_PREFIX + "*" + TEMP_SUFFIX):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("Could not remove an abandoned upload staging file")
    return removed


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


def _record_upload_event(
    action,
    outcome,
    principal,
    firm_id,
    *,
    metadata=None,
    correlation_id=None,
):
    """Record a non-success upload event without turning it into a 500."""
    audit.record_event(
        action,
        outcome,
        firm_id=firm_id,
        target_type="upload",
        correlation_id=correlation_id,
        metadata=metadata or {},
        swallow=True,
        **_actor_fields(principal),
    )


def _record_upload_rejection(
    principal,
    firm_id,
    original_name,
    reason,
    *,
    status_code=None,
    declared_mime=None,
    size_bytes=None,
    sha256=None,
    correlation_id=None,
):
    metadata = {"reason": reason, "filename": original_name}
    if status_code is not None:
        metadata["status_code"] = status_code
    if declared_mime:
        metadata["declared_mime"] = str(declared_mime)[:120]
    if size_bytes is not None:
        metadata["size_bytes"] = size_bytes
    if sha256:
        metadata["source_sha256"] = sha256
    _record_upload_event(
        "upload.rejected",
        "failure",
        principal,
        firm_id,
        metadata=metadata,
        correlation_id=correlation_id,
    )


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


def stage_upload(
    upload: UploadFile,
    upload_dir: Path,
    *,
    max_bytes: int = MAX_UPLOAD_SIZE,
    original_name: str | None = None,
) -> StagedUpload:
    """Stream an upload into a temporary file under the storage root.

    The filename is never used as a path. The temp file is created with a
    server-generated name, and the caller must publish it only after content
    validation succeeds. On any failure the temp file is removed.
    """
    display_name = sanitize_filename(
        original_name if original_name is not None else upload.filename
    )
    if not display_name.lower().endswith(".pdf"):
        raise UploadValidationError(
            400, "Only PDF files are accepted.", reason="unsupported_extension"
        )

    try:
        root = Path(upload_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UploadValidationError(
            500, "Upload storage is not available.", reason="storage_unavailable"
        ) from exc

    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=root
        )
    except OSError as exc:
        raise UploadValidationError(
            500, "Upload storage is not available.", reason="storage_unavailable"
        ) from exc

    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    total = 0
    try:
        try:
            out = os.fdopen(descriptor, "wb")
        except Exception:
            os.close(descriptor)
            raise
        with out:
            while True:
                chunk = upload.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadValidationError(
                        413,
                        "File too large. Maximum allowed size is "
                        f"{_format_size_limit(max_bytes)}.",
                        reason="file_too_large",
                    )
                digest.update(chunk)
                out.write(chunk)
        if total == 0:
            raise UploadValidationError(
                400, "Uploaded file is empty.", reason="empty_file"
            )
        return StagedUpload(
            temp_path=temp_path,
            original_name=display_name,
            size_bytes=total,
            sha256=digest.hexdigest(),
        )
    except UploadValidationError:
        _delete_file(temp_path)
        raise
    except OSError as exc:
        _delete_file(temp_path)
        logger.exception("Could not stage the uploaded file")
        raise UploadValidationError(
            500, "Could not save the uploaded file.", reason="storage_write_failed"
        ) from exc
    except Exception:
        _delete_file(temp_path)
        raise


def publish_staged_upload(
    staged: StagedUpload, upload_dir: Path, firm_id: int
) -> Path:
    """Move a staged file to its firm-scoped, server-named final path."""
    root = Path(upload_dir).expanduser().resolve()
    _confined_path(root, staged.temp_path)
    firm_dir = _firm_storage_dir(root, firm_id)
    try:
        firm_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UploadValidationError(
            500, "Upload storage is not available.", reason="storage_unavailable"
        ) from exc

    final_path = _confined_path(root, firm_dir / ("%s.pdf" % uuid.uuid4().hex))
    try:
        os.replace(staged.temp_path, final_path)
    except OSError as exc:
        _delete_file(staged.temp_path)
        raise UploadValidationError(
            500, "Could not publish the uploaded file.", reason="storage_publish_failed"
        ) from exc
    return final_path


def save_upload(upload: UploadFile, upload_dir: Path) -> tuple[str, str]:
    """Backward-compatible staging helper.

    Returns the temporary path and safe display name. Callers that accept an
    upload must validate it and then call ``publish_staged_upload``. The route
    uses ``stage_upload`` directly so it can also inspect the hash and size.
    """
    staged = stage_upload(upload, upload_dir, max_bytes=MAX_UPLOAD_SIZE)
    return str(staged.temp_path), staged.original_name


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
    audit_metadata=None,
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
            metadata = {
                "filename": filename,
                "processing_status": status,
                "version_id": version_id,
                "source_sha256": source_sha256,
                "source_size_bytes": source_size_bytes,
                "source_mime": source_mime,
                "extraction_status": extraction_status,
            }
            if audit_metadata:
                metadata.update(audit_metadata)
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
                metadata=metadata,
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


def _extract_pages_for_upload(path, **kwargs):
    """Page-aware extraction that keeps ``api_a.extract_text`` as the seam.

    ``extract_text`` is resolved from this module at call time so existing
    integrations (and tests) that patch ``api_a.extract_text`` still intercept
    PDF extraction.  ``return_pages=True`` asks extract.py for the full
    page-position result without a second parse.
    """
    return extract_text(path, return_pages=True, **kwargs)


def _record_stage_safe(
    contract_id,
    stage,
    status,
    *,
    version_id=None,
    attempt_no=1,
    started_at=None,
    finished_at=None,
    error_type=None,
    correlation_id=None,
):
    """Best-effort processing-log write; never hides the pipeline error."""
    try:
        db.record_processing_stage(
            contract_id,
            stage,
            status,
            version_id=version_id,
            attempt_no=attempt_no,
            started_at=started_at,
            finished_at=finished_at,
            error_type=error_type,
            correlation_id=correlation_id,
        )
    except Exception:
        logger.exception(
            "Could not record processing stage %s/%s for contract %s",
            stage,
            status,
            contract_id,
        )


@router.get("/health")
def health() -> dict:
    """Simple health check to confirm the API is running."""
    return {"status": "ok"}


@router.post("/upload")
def upload_contract(request: Request, file: UploadFile = File(...)) -> dict:
    """Accept, validate, persist, and analyze one text-selectable PDF.

    Authorization is checked before any upload bytes are staged or stored.
    The upload is streamed to a temporary file, validated for content and
    resource limits, and only then published under a server-generated path in
    the active firm's storage directory. A failed validation never publishes a
    file or a successful lifecycle event; a parse-stage failure records a
    failed version with provenance but no stored bytes.
    """
    settings = _upload_settings(request)
    principal = auth.principal_from_request(request)
    firm_id = auth.require_upload_permission(principal)

    max_bytes = getattr(settings, "upload_max_bytes", MAX_UPLOAD_SIZE)
    max_pages = getattr(settings, "upload_max_pages", None)
    max_extracted_chars = getattr(
        settings, "upload_max_extracted_chars", None
    )
    max_processing_seconds = getattr(
        settings, "upload_max_processing_seconds", None
    )
    correlation_id = uuid.uuid4().hex
    original_name = sanitize_filename(file.filename)

    if not original_name.lower().endswith(".pdf"):
        _record_upload_rejection(
            principal,
            firm_id,
            original_name,
            "unsupported_extension",
            status_code=400,
            declared_mime=file.content_type,
            correlation_id=correlation_id,
        )
        raise UploadValidationError(
            400, "Only PDF files are accepted.", reason="unsupported_extension"
        )

    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > max_bytes + 1024
    ):
        _record_upload_rejection(
            principal,
            firm_id,
            original_name,
            "file_too_large",
            status_code=413,
            declared_mime=file.content_type,
            correlation_id=correlation_id,
        )
        raise UploadValidationError(
            413,
            "File too large. Maximum allowed size is "
            f"{_format_size_limit(max_bytes)}.",
            reason="file_too_large",
        )

    try:
        staged = stage_upload(
            file,
            settings.upload_dir,
            max_bytes=max_bytes,
            original_name=original_name,
        )
    except UploadValidationError as exc:
        _record_upload_rejection(
            principal,
            firm_id,
            original_name,
            exc.reason,
            status_code=exc.status_code,
            declared_mime=file.content_type,
            correlation_id=correlation_id,
        )
        raise
    except Exception:
        logger.exception("Unexpected error while staging the upload")
        _record_upload_event(
            "upload.processing_failed",
            "failure",
            principal,
            firm_id,
            metadata={
                "reason": "upload_read_failed",
                "filename": original_name,
            },
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500, detail="Could not save the uploaded file."
        )

    # Duplicate-processing guard: the same bytes already ingested for this
    # firm are not re-processed into a second contract.
    duplicate = ingest.find_duplicate_contract(firm_id, staged.sha256)
    if duplicate is not None:
        _delete_file(staged.temp_path)
        try:
            audit.record_event(
                action="upload.duplicate",
                outcome="success",
                firm_id=firm_id,
                actor_user_id=principal.user_id,
                actor_type="user",
                target_type="contract",
                target_id=duplicate["contract_id"],
                correlation_id=correlation_id,
                metadata={
                    "reason": "duplicate_upload",
                    "filename": staged.original_name,
                    "source_sha256": staged.sha256,
                    "duplicate_of_contract_id": duplicate["contract_id"],
                },
            )
        except Exception:
            logger.exception("Could not record duplicate-upload audit event")
        return ingest.duplicate_result(duplicate)

    extract_started_at = db.utc_now_iso()
    try:
        text, pages, _page_count = ingest.extract_document(
            staged,
            max_pages=max_pages,
            max_extracted_chars=max_extracted_chars,
            max_processing_seconds=max_processing_seconds,
            extractor=_extract_pages_for_upload,
        )
    except PDFValidationError as exc:
        extract_finished_at = db.utc_now_iso()
        _delete_file(staged.temp_path)
        # Every failure after staging is content- or parser-related: the
        # extension and size checks already passed. Keep a failed version for
        # provenance, but do not retain the rejected bytes.
        failed_contract_id = None
        try:
            failed_contract_id = save_contract_with_trace(
                filename=staged.original_name,
                status="extraction_failed",
                raw_text=None,
                action="upload",
                details=(
                    "Text extraction failed for "
                    f"{staged.original_name}: {exc.code}"
                ),
                firm_id=firm_id,
                provenance_status=db.PROVENANCE_VERIFIED,
                created_by=principal.user_id,
                source_path=None,
                source_sha256=staged.sha256,
                source_size_bytes=staged.size_bytes,
                source_mime="application/pdf",
                extracted_text_sha256=None,
                extraction_status=db.EXTRACTION_STATUS_FAILED,
                version_status=db.VERSION_STATUS_FAILED,
                audit_action="contract.extraction_failed",
                audit_outcome="failure",
                audit_metadata={"reason_code": exc.code},
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception("Could not record extraction failure")
            _record_upload_event(
                "upload.processing_failed",
                "failure",
                principal,
                firm_id,
                metadata={
                    "reason": "failure_record_write_failed",
                    "filename": staged.original_name,
                },
                correlation_id=correlation_id,
            )
            raise HTTPException(
                status_code=500, detail="Could not save the contract record."
            )
        _record_stage_safe(
            failed_contract_id,
            db.PROCESSING_STAGE_EXTRACT,
            db.PROCESSING_STATUS_FAILED,
            version_id=db.get_current_version_id(failed_contract_id),
            started_at=extract_started_at,
            finished_at=extract_finished_at,
            error_type=exc.code,
            correlation_id=correlation_id,
        )
        raise UploadValidationError(
            exc.status_code,
            f"Could not process '{staged.original_name}': "
            f"{exc.public_message}",
            reason=exc.code,
        ) from exc
    except Exception as exc:
        _delete_file(staged.temp_path)
        logger.exception("Unexpected error during PDF extraction")
        _record_upload_event(
            "upload.processing_failed",
            "failure",
            principal,
            firm_id,
            metadata={
                "reason": "parser_exception",
                "filename": staged.original_name,
                "error_type": type(exc).__name__,
            },
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error while processing the PDF.",
        )

    try:
        published_path = publish_staged_upload(
            staged, settings.upload_dir, firm_id
        )
    except UploadValidationError as exc:
        _delete_file(staged.temp_path)
        _record_upload_event(
            "upload.processing_failed",
            "failure",
            principal,
            firm_id,
            metadata={
                "reason": exc.reason,
                "filename": staged.original_name,
            },
            correlation_id=correlation_id,
        )
        raise
    except Exception:
        _delete_file(staged.temp_path)
        logger.exception("Unexpected error while publishing the upload")
        _record_upload_event(
            "upload.processing_failed",
            "failure",
            principal,
            firm_id,
            metadata={
                "reason": "storage_publish_failed",
                "filename": staged.original_name,
            },
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500, detail="Could not publish the uploaded file."
        )

    source_sha256 = staged.sha256
    source_size_bytes = staged.size_bytes
    source_mime = "application/pdf"
    saved_path = str(published_path)

    try:
        contract_id = save_contract_with_trace(
            filename=staged.original_name,
            status="extracted",
            raw_text=text,
            action="upload",
            details=(
                "Uploaded and extracted text from "
                f"{staged.original_name} ({len(text)} characters)."
            ),
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
        _delete_file(published_path)
        logger.exception("Could not persist the accepted upload")
        _record_upload_event(
            "upload.processing_failed",
            "failure",
            principal,
            firm_id,
            metadata={
                "reason": "contract_record_failed",
                "filename": staged.original_name,
            },
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500, detail="Could not save the contract record."
        )

    original_name = staged.original_name
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

    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_EXTRACT,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=version_id,
        started_at=extract_started_at,
        correlation_id=correlation_id,
    )

    # Segment the extracted text into clauses with page and character
    # provenance. split_clauses never invents sections: it returns [] when no
    # reliable sequence is found, and an empty list is stored as "no clauses"
    # rather than fabricated content.
    segment_started_at = db.utc_now_iso()
    try:
        clauses = ingest.segment_document(text, pages)
    except Exception as exc:
        logger.exception("Failed to segment clauses for contract %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_SEGMENT,
            db.PROCESSING_STATUS_FAILED,
            version_id=version_id,
            started_at=segment_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        mark_clause_failure(
            contract_id,
            f"Stored {len(text)} characters but failed to segment clauses for {original_name}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Contract text was stored, but its clauses could not be segmented. "
                f"The contract (id={contract_id}) is marked 'clause_persistence_failed'."
            ),
        )
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_SEGMENT,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=version_id,
        started_at=segment_started_at,
        correlation_id=correlation_id,
    )

    classify_started_at = db.utc_now_iso()
    try:
        clauses = ingest.classify_clauses(clauses)
    except Exception as exc:
        logger.exception("Failed to classify clauses for contract %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_CLASSIFY,
            db.PROCESSING_STATUS_FAILED,
            version_id=version_id,
            started_at=classify_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        mark_clause_failure(
            contract_id,
            f"Stored {len(text)} characters but failed to classify clauses for {original_name}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Contract text was stored, but its clauses could not be classified. "
                f"The contract (id={contract_id}) is marked 'clause_persistence_failed'."
            ),
        )
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_CLASSIFY,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=version_id,
        started_at=classify_started_at,
        correlation_id=correlation_id,
    )

    persist_started_at = db.utc_now_iso()
    try:
        saved_clause_count = ingest.persist_clauses(
            contract_id,
            version_id,
            clauses,
            correlation_id,
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
            saver=save_clauses,
        )
    except Exception as exc:
        # Contract row and version were already committed as 'extracted', so
        # flip the status and mark the draft version failed instead of
        # returning a misleading success.
        logger.exception("Failed to persist clauses for contract %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_PERSIST,
            db.PROCESSING_STATUS_FAILED,
            version_id=version_id,
            started_at=persist_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
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
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_PERSIST,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=version_id,
        started_at=persist_started_at,
        correlation_id=correlation_id,
    )

    graph_started_at = db.utc_now_iso()
    try:
        graph_stats = ingest.build_contract_graph(
            contract_id,
            {
                "firm_id": firm_id,
                "actor_user_id": principal.user_id,
                "actor_type": "user",
                "correlation_id": correlation_id,
                "filename": original_name,
            },
        )
    except Exception as exc:
        logger.exception("Failed to build graph for contract %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_GRAPH,
            db.PROCESSING_STATUS_FAILED,
            version_id=version_id,
            started_at=graph_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
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
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_GRAPH,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=version_id,
        started_at=graph_started_at,
        correlation_id=correlation_id,
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


@router.get("/contracts/{contract_id}/processing")
def get_contract_processing_route(contract_id: int, request: Request = None) -> dict:
    """Return processing health and the per-stage ingestion history.

    Stage rows contain only operational metadata (stage, status, attempt,
    timestamps, error type).  They never contain contract or clause text.
    """
    principal = auth.principal_from_request(request)
    contract = auth.authorized_contract(contract_id, principal)
    return {
        "contract_id": contract_id,
        "processing_status": contract.get("processing_status"),
        "retryable": contract.get("processing_status")
        in ("clause_persistence_failed", "graph_build_failed"),
        "stages": db.list_processing_log(contract_id),
    }


def _settings_limits(settings):
    return (
        getattr(settings, "upload_max_pages", None),
        getattr(settings, "upload_max_extracted_chars", None),
        getattr(settings, "upload_max_processing_seconds", None),
    )


@router.post("/contracts/{contract_id}/retry")
def retry_contract_route(contract_id: int, request: Request = None) -> dict:
    """Retry a failed ingestion stage without duplicating current clauses.

    Two partial-failure states are retryable:

    * ``clause_persistence_failed``: segmentation/classification/persistence is
      re-run from the stored raw text into a NEW draft version.  Current clause
      rows are replaced atomically (the old set is archived to clause history),
      so a retry can never create a duplicate live clause set.
    * ``graph_build_failed``: only the graph is rebuilt; the existing clause
      set is left untouched.

    ``extraction_failed`` is not retryable because rejected bytes are
    deliberately not retained; the user must re-upload the source PDF.
    """
    principal = auth.principal_from_request(request)
    firm_id = auth.require_upload_permission(principal)
    contract = auth.authorized_contract(contract_id, principal)
    status = contract.get("processing_status")
    correlation_id = uuid.uuid4().hex

    if status == "extraction_failed":
        raise HTTPException(
            status_code=409,
            detail=(
                "The original PDF was not retained for a failed extraction. "
                "Re-upload the source file to retry ingestion."
            ),
        )
    if status not in ("clause_persistence_failed", "graph_build_failed"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This contract is not in a retryable failed state "
                "(processing_status=%s)." % status
            ),
        )

    settings = _upload_settings(request)
    max_pages, max_extracted_chars, max_processing_seconds = _settings_limits(
        settings
    )
    version_id = db.get_current_version_id(contract_id)

    if status == "graph_build_failed":
        if not list_clauses(contract_id):
            raise HTTPException(
                status_code=409,
                detail="No stored clauses are available to rebuild the graph.",
            )
        started_at = db.utc_now_iso()
        attempt_no = db.next_processing_attempt(
            contract_id, db.PROCESSING_STAGE_GRAPH
        )
        try:
            graph_stats = ingest.build_contract_graph(
                contract_id,
                {
                    "firm_id": firm_id,
                    "actor_user_id": principal.user_id,
                    "actor_type": "user",
                    "correlation_id": correlation_id,
                    "filename": contract.get("filename"),
                },
            )
        except Exception as exc:
            logger.exception("Retry failed to rebuild graph for %s", contract_id)
            _record_stage_safe(
                contract_id,
                db.PROCESSING_STAGE_GRAPH,
                db.PROCESSING_STATUS_FAILED,
                version_id=version_id,
                attempt_no=attempt_no,
                started_at=started_at,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
            mark_graph_failure(
                contract_id,
                f"Retry could not rebuild the graph for {contract.get('filename')}.",
                principal=principal,
                firm_id=firm_id,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
            raise HTTPException(
                status_code=500,
                detail="Graph rebuild failed; the previous graph was left intact.",
            )
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_GRAPH,
            db.PROCESSING_STATUS_SUCCEEDED,
            version_id=version_id,
            attempt_no=attempt_no,
            started_at=started_at,
            correlation_id=correlation_id,
        )
        db.set_processing_status(contract_id, "extracted")
        audit.record_event(
            action="contract.retry_succeeded",
            outcome="success",
            firm_id=firm_id,
            actor_user_id=principal.user_id,
            actor_type="user",
            target_type="contract",
            target_id=contract_id,
            correlation_id=correlation_id,
            metadata={
                "stage": db.PROCESSING_STAGE_GRAPH,
                "version_id": version_id,
                "graph": graph_stats,
            },
        )
        return {
            "message": "Graph rebuilt successfully.",
            "contract_id": contract_id,
            "status": "extracted",
            "retried_stage": db.PROCESSING_STAGE_GRAPH,
            "graph": graph_stats,
        }

    # clause_persistence_failed: rebuild clauses from stored raw text.
    raw_text = contract.get("raw_text")
    if not raw_text or not raw_text.strip():
        raise HTTPException(
            status_code=409,
            detail="Stored contract text is unavailable; re-upload the source PDF.",
        )

    old_version = db.get_contract_version(version_id) if version_id else None
    if old_version is None:
        raise HTTPException(
            status_code=409,
            detail="No contract version is available to retry.",
        )

    # Page provenance is reconstructed from the retained source file when it
    # still exists.  A missing file only lowers page precision; the stored raw
    # text remains authoritative and offsets stay exact within it.
    pages = None
    source_path = old_version.get("source_path")
    if source_path and os.path.isfile(source_path):
        try:
            _retry_text, pages, _retry_pages = ingest.extract_document(
                source_path,
                max_pages=max_pages,
                max_extracted_chars=max_extracted_chars,
                max_processing_seconds=max_processing_seconds,
            )
        except Exception:
            logger.warning(
                "Retry could not re-read source pages for contract %s; "
                "continuing with stored text and no page mapping.",
                contract_id,
                exc_info=True,
            )
            pages = None

    try:
        retry_version_id = db.create_contract_version(
            contract_id,
            firm_id=firm_id,
            created_by=principal.user_id,
            source_filename=old_version.get("source_filename")
            or contract.get("filename"),
            source_path=source_path,
            source_sha256=old_version.get("source_sha256"),
            source_size_bytes=old_version.get("source_size_bytes"),
            source_mime=old_version.get("source_mime"),
            extracted_text_sha256=db.sha256_text(raw_text),
            extraction_status=db.EXTRACTION_STATUS_SUCCEEDED,
            clause_splitter_version=clause_tools.SPLITTER_VERSION,
            provenance_status=old_version.get("provenance_status")
            or contract.get("provenance_status"),
            source_kind=old_version.get("source_kind") or "upload",
            status=db.VERSION_STATUS_DRAFT,
        )
    except Exception as exc:
        logger.exception("Could not create retry version for %s", contract_id)
        raise HTTPException(
            status_code=500,
            detail="Could not create a new contract version for the retry.",
        ) from exc

    segment_started_at = db.utc_now_iso()
    segment_attempt = db.next_processing_attempt(
        contract_id, db.PROCESSING_STAGE_SEGMENT
    )
    try:
        clauses = ingest.segment_document(raw_text, pages)
    except Exception as exc:
        logger.exception("Retry failed to segment clauses for %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_SEGMENT,
            db.PROCESSING_STATUS_FAILED,
            version_id=retry_version_id,
            attempt_no=segment_attempt,
            started_at=segment_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        mark_clause_failure(
            contract_id,
            f"Retry failed to segment clauses for {contract.get('filename')}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500, detail="Clause segmentation failed during retry."
        )
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_SEGMENT,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=retry_version_id,
        attempt_no=segment_attempt,
        started_at=segment_started_at,
        correlation_id=correlation_id,
    )

    classify_started_at = db.utc_now_iso()
    classify_attempt = db.next_processing_attempt(
        contract_id, db.PROCESSING_STAGE_CLASSIFY
    )
    try:
        clauses = ingest.classify_clauses(clauses)
    except Exception as exc:
        logger.exception("Retry failed to classify clauses for %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_CLASSIFY,
            db.PROCESSING_STATUS_FAILED,
            version_id=retry_version_id,
            attempt_no=classify_attempt,
            started_at=classify_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        mark_clause_failure(
            contract_id,
            f"Retry failed to classify clauses for {contract.get('filename')}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500, detail="Clause classification failed during retry."
        )
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_CLASSIFY,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=retry_version_id,
        attempt_no=classify_attempt,
        started_at=classify_started_at,
        correlation_id=correlation_id,
    )

    persist_started_at = db.utc_now_iso()
    persist_attempt = db.next_processing_attempt(
        contract_id, db.PROCESSING_STAGE_PERSIST
    )
    try:
        saved_clause_count = ingest.persist_clauses(
            contract_id,
            retry_version_id,
            clauses,
            correlation_id,
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
                    "filename": contract.get("filename"),
                    "clause_count": len(clauses),
                    "retry": True,
                    "processing_status": "extracted",
                },
            },
            saver=save_clauses,
        )
    except Exception as exc:
        logger.exception("Retry failed to persist clauses for %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_PERSIST,
            db.PROCESSING_STATUS_FAILED,
            version_id=retry_version_id,
            attempt_no=persist_attempt,
            started_at=persist_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        mark_clause_failure(
            contract_id,
            f"Retry failed to save clauses for {contract.get('filename')}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500, detail="Clause persistence failed during retry."
        )
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_PERSIST,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=retry_version_id,
        attempt_no=persist_attempt,
        started_at=persist_started_at,
        correlation_id=correlation_id,
    )

    graph_started_at = db.utc_now_iso()
    graph_attempt = db.next_processing_attempt(
        contract_id, db.PROCESSING_STAGE_GRAPH
    )
    try:
        graph_stats = ingest.build_contract_graph(
            contract_id,
            {
                "firm_id": firm_id,
                "actor_user_id": principal.user_id,
                "actor_type": "user",
                "correlation_id": correlation_id,
                "filename": contract.get("filename"),
            },
        )
    except Exception as exc:
        logger.exception("Retry failed to build graph for %s", contract_id)
        _record_stage_safe(
            contract_id,
            db.PROCESSING_STAGE_GRAPH,
            db.PROCESSING_STATUS_FAILED,
            version_id=retry_version_id,
            attempt_no=graph_attempt,
            started_at=graph_started_at,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        mark_graph_failure(
            contract_id,
            f"Clauses were saved but graph construction failed on retry for "
            f"{contract.get('filename')}.",
            principal=principal,
            firm_id=firm_id,
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Clauses were saved on retry, but analysis could not be completed. "
                "The contract is marked 'graph_build_failed'."
            ),
        )
    _record_stage_safe(
        contract_id,
        db.PROCESSING_STAGE_GRAPH,
        db.PROCESSING_STATUS_SUCCEEDED,
        version_id=retry_version_id,
        attempt_no=graph_attempt,
        started_at=graph_started_at,
        correlation_id=correlation_id,
    )

    db.set_processing_status(contract_id, "extracted")
    audit.record_event(
        action="contract.retry_succeeded",
        outcome="success",
        firm_id=firm_id,
        actor_user_id=principal.user_id,
        actor_type="user",
        target_type="contract",
        target_id=contract_id,
        correlation_id=correlation_id,
        metadata={
            "stage": db.PROCESSING_STAGE_PERSIST,
            "version_id": retry_version_id,
            "clause_count": saved_clause_count,
            "graph": graph_stats,
        },
    )
    return {
        "message": "Contract reprocessed successfully.",
        "contract_id": contract_id,
        "status": "extracted",
        "retried_stage": db.PROCESSING_STAGE_PERSIST,
        "version_id": retry_version_id,
        "clauses_saved": saved_clause_count,
        "analysis_status": "ready" if saved_clause_count else "unavailable",
        "graph": graph_stats,
    }


@router.get("/contracts")
def list_contracts_route(
    request: Request = None,
    search: str = None,
    processing_status: str = None,
    lifecycle_status: str = None,
) -> list:
    """Return only contracts visible in the request's active scope.

    Optional query parameters filter the listing:
    ``search`` matches the filename, ``processing_status`` and
    ``lifecycle_status`` match exactly.  Results are always scoped to the
    active firm (or the demo scope).
    """
    principal = auth.principal_from_request(request)
    return auth.authorized_contracts(
        principal,
        search=search,
        processing_status=processing_status,
        lifecycle_status=lifecycle_status,
    )


@router.get("/contracts/{contract_id}/versions")
def list_contract_versions_route(contract_id: int, request: Request = None) -> list:
    """Return the version history of one contract in the active scope.

    Every version records who created it, when, the source file hash/size/MIME,
    extraction status, and the lifecycle status of the contract at that time.
    Older versions are immutable (published/superseded).
    """
    principal = auth.principal_from_request(request)
    auth.authorized_contract(contract_id, principal)
    return db.list_contract_versions(contract_id)


@router.patch("/contracts/{contract_id}/lifecycle")
async def set_contract_lifecycle_route(
    contract_id: int, request: Request = None
) -> dict:
    """Update the lifecycle status of one contract in the active scope.

    Body: ``{"lifecycle_status": "draft|under_review|approved|active|expired|archived"}``.

    Only members with upload-level roles (administrator, lawyer, paralegal)
    may advance the lifecycle.  The change is audited with the acting user,
    firm, contract, previous and new status, and timestamp.
    """

    principal = auth.principal_from_request(request)
    contract = auth.authorized_contract(contract_id, principal)
    if principal.role not in auth.UPLOAD_ROLES:
        auth._record_audit(
            "contract.lifecycle_denied",
            "denied",
            principal=principal,
            firm_id=principal.firm_id,
            target_type="contract",
            target_id=contract_id,
            metadata={
                "reason": "insufficient_role",
                "role": principal.role,
                "requested": None,
            },
            swallow=True,
        )
        raise HTTPException(
            status_code=403,
            detail="Your firm role does not permit contract lifecycle changes.",
        )
    body = None
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    new_status = db.valid_lifecycle_status(body.get("lifecycle_status"))
    if new_status is None:
        raise HTTPException(
            status_code=400,
            detail="lifecycle_status must be one of: %s"
            % ", ".join(sorted(db.LIFECYCLE_STATUSES)),
        )
    old_status = contract.get("lifecycle_status")
    if old_status == new_status:
        return {
            "ok": True,
            "contract_id": contract_id,
            "lifecycle_status": new_status,
            "message": "Lifecycle status unchanged.",
        }
    updated = db.set_contract_lifecycle_status(
        contract_id, new_status, principal.firm_id
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    auth._record_audit(
        "contract.lifecycle_changed",
        principal=principal,
        firm_id=principal.firm_id,
        target_type="contract",
        target_id=contract_id,
        metadata={
            "previous": old_status,
            "current": new_status,
            "filename": contract.get("filename"),
        },
    )
    return {
        "ok": True,
        "contract_id": contract_id,
        "lifecycle_status": new_status,
        "previous": old_status,
        "message": "Contract lifecycle status updated.",
    }


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
