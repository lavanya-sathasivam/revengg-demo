import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_INTEGRITY_VERSION = "phase5.1"

PROVENANCE_VERIFIED = "verified"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_UNVERIFIED = "unverified"
PROVENANCE_LEGACY = "legacy_unclassified"

VERSION_STATUS_DRAFT = "draft"
VERSION_STATUS_EXTRACTED = "extracted"
VERSION_STATUS_PUBLISHED = "published"
VERSION_STATUS_SUPERSEDED = "superseded"
VERSION_STATUS_FAILED = "failed"

# Contract lifecycle statuses. Distinct from processing_status, which describes
# ingestion/extraction/graph health. lifecycle_status describes the legal
# lifecycle of the contract as managed by the firm.
LIFECYCLE_DRAFT = "draft"
LIFECYCLE_UNDER_REVIEW = "under_review"
LIFECYCLE_APPROVED = "approved"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_EXPIRED = "expired"
LIFECYCLE_ARCHIVED = "archived"

LIFECYCLE_STATUSES = frozenset(
    {
        LIFECYCLE_DRAFT,
        LIFECYCLE_UNDER_REVIEW,
        LIFECYCLE_APPROVED,
        LIFECYCLE_ACTIVE,
        LIFECYCLE_EXPIRED,
        LIFECYCLE_ARCHIVED,
    }
)


def valid_lifecycle_status(status):
    """Return the status if it is a known lifecycle status, else None."""
    if status is None:
        return None
    status = str(status).strip()
    return status if status in LIFECYCLE_STATUSES else None

EXTRACTION_STATUS_SUCCEEDED = "succeeded"
EXTRACTION_STATUS_FAILED = "failed"


def utc_now_iso() -> str:
    """Return one consistent UTC timestamp format for all persisted rows."""
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str | None) -> str | None:
    """Hash text for provenance without storing a second readable copy."""
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatabaseConfig:
    """An explicitly selected database and whether it may be written to.

    The application factory installs this for the lifetime of a request.
    Legacy scripts and tests may leave it unset, in which case get_db_path()
    keeps its historical default.
    """

    path: str
    read_only: bool = False


_database_config: ContextVar[DatabaseConfig | None] = ContextVar(
    "database_config", default=None
)


def set_database_config(config: DatabaseConfig):
    """Activate a database configuration in the current context."""
    return _database_config.set(config)


def reset_database_config(token) -> None:
    """Restore the previous database configuration."""
    _database_config.reset(token)


def get_database_config():
    """Return the active configuration, or None for legacy callers."""
    return _database_config.get()


@contextmanager
def database_context(path, read_only=False):
    """Temporarily use one database path in the current context.

    Used by startup, tests, and maintenance scripts. The previous setting is
    always restored, so no process-wide monkeypatch is needed.
    """
    token = set_database_config(DatabaseConfig(str(path), read_only=read_only))
    try:
        yield
    finally:
        reset_database_config(token)


def get_db_path():
    """Return the explicitly selected path, or the legacy app.db default."""
    config = _database_config.get()
    if config is not None:
        return config.path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "app.db")


def get_conn():
    """Return a new SQLite connection with safe defaults.

    A read-only configuration opens the file in SQLite read-only mode and
    enables query_only as a second layer. A missing demo database therefore
    fails closed instead of being silently created.
    """
    config = _database_config.get()
    if config is None:
        conn = sqlite3.connect(get_db_path())
    elif config.read_only:
        uri = Path(config.path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(config.path)
    conn.row_factory = sqlite3.Row
    # Enable foreign key enforcement
    conn.execute("PRAGMA foreign_keys = ON")
    if config is not None and config.read_only:
        conn.execute("PRAGMA query_only = ON")
    return conn


def get_contract(contract_id: int):
    """Return one contract as a dict, or None if the ID does not exist."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT contract_id, filename, upload_timestamp,
                      processing_status, raw_text, %s
               FROM contracts
               WHERE contract_id = ?""" % _contract_select_extras(conn),
            (contract_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_contracts():
    """Return all contracts (lightweight fields only) ordered by contract_id."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT contract_id, filename, upload_timestamp, processing_status,
                      %s
               FROM contracts
               ORDER BY contract_id ASC""" % _contract_select_extras(conn)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _contract_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(contracts)")}


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def _contract_select_extras(conn) -> str:
    """Columns added by the integrity migration, or explicit legacy defaults.

    Demo mode opens a read-only database that may predate these columns.  It
    must remain readable without running a migration, and its rows must be
    labelled as unknown provenance rather than silently treated as verified.
    """

    columns = _contract_columns(conn)
    extras = []
    extras.append(
        "current_version_id"
        if "current_version_id" in columns
        else "NULL AS current_version_id"
    )
    extras.append(
        "current_graph_build_id"
        if "current_graph_build_id" in columns
        else "NULL AS current_graph_build_id"
    )
    extras.append(
        "provenance_status"
        if "provenance_status" in columns
        else "'%s' AS provenance_status" % PROVENANCE_LEGACY
    )
    extras.append(
        "firm_id" if "firm_id" in columns else "NULL AS firm_id"
    )
    extras.append(
        "lifecycle_status"
        if "lifecycle_status" in columns
        else "'%s' AS lifecycle_status" % LIFECYCLE_DRAFT
    )
    return ", ".join(extras)


def _contract_scope(conn, firm_id):
    """Return (SQL fragment, params) for a demo or firm-scoped contract query.

    Demo mode reads only rows whose ``firm_id`` is NULL.  Firm mode reads only
    the active firm.  A database that predates the additive migration has no
    ``firm_id`` column: demo reads remain valid (all rows are synthetic), but
    a firm-scoped read fails closed instead of ignoring the tenant filter.
    """

    if "firm_id" not in _contract_columns(conn):
        if firm_id is None:
            return "", ()
        return " AND 0", ()
    if firm_id is None:
        return " AND firm_id IS NULL", ()
    return " AND firm_id = ?", (firm_id,)


def get_contract_for_firm(contract_id: int, firm_id):
    """Return a contract only when it belongs to the requested scope.

    ``firm_id=None`` is the read-only synthetic demo scope.  Any other value is
    an authenticated firm id.  Foreign and unknown ids both return None.
    """

    conn = get_conn()
    try:
        scope_sql, params = _contract_scope(conn, firm_id)
        cur = conn.execute(
            """SELECT contract_id, filename, upload_timestamp,
                      processing_status, raw_text, %s
               FROM contracts
               WHERE contract_id = ?""" % _contract_select_extras(conn) + scope_sql,
            (contract_id,) + params,
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_contracts_for_firm(
    firm_id,
    *,
    search=None,
    processing_status=None,
    lifecycle_status=None,
):
    """Return lightweight contract rows scoped to the demo or one firm.

    Optional filters are combined with the firm scope. ``search`` matches the
    filename (case-insensitive substring). ``processing_status`` and
    ``lifecycle_status`` match exactly.
    """

    conn = get_conn()
    try:
        scope_sql, params = _contract_scope(conn, firm_id)
        filters = []
        values = list(params)
        if search:
            filters.append("filename LIKE ?")
            values.append("%" + search + "%")
        if processing_status is not None:
            filters.append("processing_status = ?")
            values.append(processing_status)
        if lifecycle_status is not None:
            filters.append("lifecycle_status = ?")
            values.append(lifecycle_status)
        filter_sql = ""
        if filters:
            filter_sql = " AND " + " AND ".join(filters)
        cur = conn.execute(
            """SELECT contract_id, filename, upload_timestamp, processing_status,
                      %s
               FROM contracts
               WHERE 1 = 1""" % _contract_select_extras(conn)
            + scope_sql
            + filter_sql
            + """
               ORDER BY contract_id ASC""",
            values,
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _cursor_columns(cur, table: str) -> set[str]:
    return {row[1] for row in cur.execute("PRAGMA table_info(%s)" % table)}


def _cursor_table_exists(cur, table: str) -> bool:
    return bool(
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def _contract_row_for_write(cur, contract_id):
    columns = _cursor_columns(cur, "contracts")
    fields = ["contract_id", "filename", "processing_status"]
    for optional in (
        "firm_id",
        "current_version_id",
        "current_graph_build_id",
        "provenance_status",
        "raw_text",
    ):
        if optional in columns:
            fields.append(optional)
    row = cur.execute(
        "SELECT %s FROM contracts WHERE contract_id = ?" % ", ".join(fields),
        (contract_id,),
    ).fetchone()
    return dict(row) if row else None


def _next_version_number(cur, contract_id) -> int:
    row = cur.execute(
        """SELECT COALESCE(MAX(version_number), 0) + 1
           FROM contract_versions WHERE contract_id = ?""",
        (contract_id,),
    ).fetchone()
    return int(row[0])


def insert_version_row(
    cur,
    contract_id,
    *,
    firm_id=None,
    created_by=None,
    source_filename=None,
    source_path=None,
    source_sha256=None,
    source_size_bytes=None,
    source_mime=None,
    extracted_text_sha256=None,
    extraction_status=EXTRACTION_STATUS_SUCCEEDED,
    clause_splitter_version=None,
    provenance_status=None,
    source_kind="programmatic",
    status=VERSION_STATUS_DRAFT,
    version_number=None,
):
    """Insert one immutable version row and return its id.

    The caller owns the transaction.  Version numbers are scoped to the
    contract, so two contracts may both have version 1 without colliding.
    This low-level helper is public so an intake transaction can create the
    contract row and its first version atomically.
    """

    if version_number is None:
        version_number = _next_version_number(cur, contract_id)
    cur.execute(
        """INSERT INTO contract_versions
               (contract_id, version_number, created_at, created_by, firm_id,
                source_filename, source_path, source_sha256, source_size_bytes,
                source_mime, extracted_text_sha256, extraction_status,
                clause_splitter_version, provenance_status, source_kind, status,
                published_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            contract_id,
            version_number,
            utc_now_iso(),
            created_by,
            firm_id,
            source_filename,
            source_path,
            source_sha256,
            source_size_bytes,
            source_mime,
            extracted_text_sha256,
            extraction_status,
            clause_splitter_version,
            provenance_status or PROVENANCE_UNVERIFIED,
            source_kind,
            status,
        ),
    )
    return cur.lastrowid


def create_contract_version(
    contract_id,
    *,
    firm_id=None,
    created_by=None,
    source_filename=None,
    source_path=None,
    source_sha256=None,
    source_size_bytes=None,
    source_mime=None,
    extracted_text_sha256=None,
    extraction_status=EXTRACTION_STATUS_SUCCEEDED,
    clause_splitter_version=None,
    provenance_status=None,
    source_kind="programmatic",
    status=VERSION_STATUS_DRAFT,
):
    """Public additive version creation helper.

    The version's firm must match the contract's firm.  A demo/legacy contract
    (firm_id NULL) cannot be silently assigned to a firm by creating a version
    for it.
    """

    conn = get_conn()
    try:
        cur = conn.cursor()
        contract = _contract_row_for_write(cur, contract_id)
        if contract is None:
            raise ValueError("Contract does not exist.")
        contract_firm = contract.get("firm_id")
        if firm_id is not None and contract_firm != firm_id:
            raise ValueError("Version firm does not match the contract firm.")
        version_provenance = (
            provenance_status
            or contract.get("provenance_status")
            or PROVENANCE_UNVERIFIED
        )
        version_id = insert_version_row(
            cur,
            contract_id,
            firm_id=contract_firm,
            created_by=created_by,
            source_filename=source_filename,
            source_path=source_path,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            source_mime=source_mime,
            extracted_text_sha256=extracted_text_sha256,
            extraction_status=extraction_status,
            clause_splitter_version=clause_splitter_version,
            provenance_status=version_provenance,
            source_kind=source_kind,
            status=status,
        )
        columns = _cursor_columns(cur, "contracts")
        assignments = ["current_version_id = ?"]
        values = [version_id]
        if "current_graph_build_id" in columns:
            # A new current version invalidates the graph that was built from
            # the old one. The graph rows remain for audit, but no reader may
            # treat them as current.
            assignments.append("current_graph_build_id = NULL")
        if "provenance_status" in columns:
            assignments.append("provenance_status = ?")
            values.append(version_provenance)
        values.append(contract_id)
        cur.execute(
            "UPDATE contracts SET %s WHERE contract_id = ?"
            % ", ".join(assignments),
            values,
        )
        conn.commit()
        return version_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resolve_version_for_save(cur, contract):
    """Choose the version that a clause save belongs to.

    A published version is immutable.  If the current version already has
    clauses, a direct save creates a new version rather than overwriting the
    prior one.  A draft/extracted version with no clauses is still writable.
    """

    contract_id = contract["contract_id"]
    current_id = contract.get("current_version_id")
    if current_id is not None:
        row = cur.execute(
            """SELECT version_id, contract_id, status, firm_id
               FROM contract_versions WHERE version_id = ?""",
            (current_id,),
        ).fetchone()
        if row is not None and row["contract_id"] == contract_id:
            has_clauses = cur.execute(
                "SELECT 1 FROM clauses WHERE contract_id = ? LIMIT 1",
                (contract_id,),
            ).fetchone()
            if row["status"] in (VERSION_STATUS_DRAFT, VERSION_STATUS_EXTRACTED) \
                    and (not has_clauses or row["status"] == VERSION_STATUS_DRAFT):
                return row["version_id"]

    current_provenance = contract.get("provenance_status") or PROVENANCE_UNVERIFIED
    if current_provenance not in (
        PROVENANCE_VERIFIED,
        PROVENANCE_SYNTHETIC,
        PROVENANCE_UNVERIFIED,
        PROVENANCE_LEGACY,
    ):
        current_provenance = PROVENANCE_UNVERIFIED
    source_kind = {
        PROVENANCE_SYNTHETIC: "synthetic",
        PROVENANCE_LEGACY: "legacy",
    }.get(current_provenance, "programmatic")
    raw_text = contract.get("raw_text")
    version_id = insert_version_row(
        cur,
        contract_id,
        firm_id=contract.get("firm_id"),
        source_filename=contract.get("filename"),
        extracted_text_sha256=sha256_text(raw_text),
        extraction_status=EXTRACTION_STATUS_SUCCEEDED,
        provenance_status=current_provenance,
        source_kind=source_kind,
        status=VERSION_STATUS_DRAFT,
    )
    cur.execute(
        "UPDATE contracts SET current_version_id = ? WHERE contract_id = ?",
        (version_id, contract_id),
    )
    return version_id


def _validate_explicit_version(cur, contract_id, version_id):
    row = cur.execute(
        """SELECT version_id, contract_id, status, firm_id
           FROM contract_versions WHERE version_id = ?""",
        (version_id,),
    ).fetchone()
    if row is None or row["contract_id"] != contract_id:
        raise ValueError("Version does not belong to this contract.")
    if row["status"] in (VERSION_STATUS_PUBLISHED, VERSION_STATUS_SUPERSEDED):
        raise ValueError(
            "Published contract versions are immutable; create a new version."
        )
    if row["status"] == VERSION_STATUS_FAILED:
        raise ValueError("A failed version cannot be overwritten; create a new version.")
    return row


def _archive_current_clauses(
    cur, contract_id, old_version_id, new_version_id, archived_at
):
    """Copy the current clause set into immutable history before replacement."""

    if not _cursor_table_exists(cur, "clause_history"):
        return 0
    clause_columns = _cursor_columns(cur, "clauses")
    version_expr = "version_id" if "version_id" in clause_columns else "NULL"
    hash_expr = "clause_sha256" if "clause_sha256" in clause_columns else "NULL"
    rows = cur.execute(
        """SELECT clause_id, section_number, clause_text, metadata,
                  %s AS version_id, %s AS clause_sha256
           FROM clauses WHERE contract_id = ?""" % (version_expr, hash_expr),
        (contract_id,),
    ).fetchall()
    if not rows:
        return 0

    # The current-version pointer may already point at the new draft (for
    # example when create_contract_version() was called first).  Archive based
    # on where the stored rows actually came from, so replacing a published
    # set is never mistaken for editing the same draft.
    stored_versions = {row["version_id"] for row in rows}
    if stored_versions == {new_version_id}:
        # Replacing the same draft is not a published-version overwrite.
        return 0
    reason = (
        "version_replaced"
        if any(version is not None for version in stored_versions)
        else "legacy_replaced"
    )
    for row in rows:
        cur.execute(
            """INSERT INTO clause_history
                   (clause_id, contract_id, version_id, section_number,
                    clause_text, metadata, clause_sha256, archived_at,
                    archive_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["clause_id"],
                contract_id,
                row["version_id"],
                row["section_number"],
                row["clause_text"],
                row["metadata"],
                row["clause_sha256"],
                archived_at,
                reason,
            ),
        )
    return len(rows)


def save_clauses(contract_id, clauses, version_id=None, audit_event=None):
    """
    Store the current clause set for one contract/version.

    ``version_id`` is optional for backward compatibility.  When omitted, the
    save uses the current draft/extracted version, or creates a new version if
    the current version is already published.  A published version can never
    be overwritten through this function; an explicit attempt raises
    ``ValueError`` and leaves the stored set untouched.

    The clauses table remains the materialised *current* set, so the graph and
    impact engine keep working unchanged.  Replaced rows are copied to
    ``clause_history`` before deletion.  The whole operation is one SQLite
    transaction; any failure rolls back clause changes, history, version
    publication, and the current-version pointer together.

    Returns the number of clause rows saved (0 for an empty list).
    """

    conn = get_conn()
    try:
        cur = conn.cursor()
        contract = _contract_row_for_write(cur, contract_id)
        if contract is None:
            # Preserve the historical failure type expected by callers.
            raise sqlite3.IntegrityError("contract does not exist")

        if version_id is None:
            if not _cursor_table_exists(cur, "contract_versions"):
                raise RuntimeError(
                    "Contract version schema is missing; run db.init_db() "
                    "against a writable database before saving clauses."
                )
            version_id = _resolve_version_for_save(cur, contract)
        else:
            _validate_explicit_version(cur, contract_id, version_id)

        version_row = cur.execute(
            """SELECT version_id, provenance_status, status
               FROM contract_versions WHERE version_id = ?""",
            (version_id,),
        ).fetchone()

        old_version_id = contract.get("current_version_id")
        archived_at = utc_now_iso()
        _archive_current_clauses(
            cur, contract_id, old_version_id, version_id, archived_at
        )

        cur.execute("DELETE FROM clauses WHERE contract_id = ?", (contract_id,))
        clause_columns = _cursor_columns(cur, "clauses")
        for item in clauses:
            metadata = item.get("metadata") or {}
            values = [
                contract_id,
                item["section_number"],
                item["clause_text"],
                json.dumps(metadata),
            ]
            columns = ["contract_id", "section_number", "clause_text", "metadata"]
            metadata = item.get("metadata") or {}
            for optional, key in (
                ("version_id", "version_id"),
                ("clause_sha256", "clause_sha256"),
                ("page_number", "page_number"),
                ("char_start", "char_start"),
                ("char_end", "char_end"),
                ("parent_id", "parent_id"),
                ("normalized_text", "normalized_text"),
            ):
                if optional not in clause_columns:
                    continue
                value = item.get(key)
                if value is None and key == "version_id":
                    # Every stored clause belongs to the version being saved,
                    # even when the caller did not repeat the id on each item.
                    value = version_id
                if value is None:
                    value = metadata.get(key)
                if value is None and key == "clause_sha256":
                    value = sha256_text(item["clause_text"])
                columns.append(optional)
                values.append(value)
            cur.execute(
                "INSERT INTO clauses (%s) VALUES (%s)"
                % (", ".join(columns), ", ".join("?" * len(columns))),
                values,
            )

        if version_row is not None:
            cur.execute(
                """UPDATE contract_versions
                   SET status = ?, published_at = ?
                   WHERE version_id = ?""",
                (VERSION_STATUS_PUBLISHED, archived_at, version_id),
            )
            cur.execute(
                """UPDATE contract_versions
                   SET status = ?
                   WHERE contract_id = ? AND version_id <> ?
                     AND status = ?""",
                (
                    VERSION_STATUS_SUPERSEDED,
                    contract_id,
                    version_id,
                    VERSION_STATUS_PUBLISHED,
                ),
            )

        contract_columns = _cursor_columns(cur, "contracts")
        if "current_version_id" in contract_columns:
            cur.execute(
                "UPDATE contracts SET current_version_id = ? WHERE contract_id = ?",
                (version_id, contract_id),
            )
        if "current_graph_build_id" in contract_columns:
            # Clause rows have just been replaced, so any published graph was
            # built from a different clause set. Invalidate the pointer in the
            # SAME transaction; a reader can never see new clauses with an old
            # graph pointer.
            cur.execute(
                "UPDATE contracts SET current_graph_build_id = NULL "
                "WHERE contract_id = ?",
                (contract_id,),
            )
        if version_row is not None and "provenance_status" in contract_columns:
            cur.execute(
                "UPDATE contracts SET provenance_status = ? WHERE contract_id = ?",
                (version_row["provenance_status"], contract_id),
            )

        if audit_event:
            import audit

            event = dict(audit_event)
            metadata = dict(event.pop("metadata", {}) or {})
            metadata.setdefault("contract_id", contract_id)
            metadata.setdefault("version_id", version_id)
            metadata.setdefault("clause_count", len(clauses))
            audit.record_event(
                conn=conn,
                commit=False,
                target_type=event.pop("target_type", "contract"),
                target_id=event.pop("target_id", contract_id),
                metadata=metadata,
                **event,
            )

        conn.commit()
        return len(clauses)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


PROCESSING_STAGE_EXTRACT = "extract"
PROCESSING_STAGE_SEGMENT = "segment"
PROCESSING_STAGE_CLASSIFY = "classify"
PROCESSING_STAGE_PERSIST = "persist"
PROCESSING_STAGE_GRAPH = "graph"

PROCESSING_STATUS_STARTED = "started"
PROCESSING_STATUS_SUCCEEDED = "succeeded"
PROCESSING_STATUS_FAILED = "failed"


def record_processing_stage(
    contract_id,
    stage,
    status,
    *,
    version_id=None,
    attempt_no=1,
    started_at=None,
    finished_at=None,
    error_type=None,
    error_message=None,
    correlation_id=None,
):
    """Record one ingestion-pipeline stage attempt.

    Each stage (extract, segment, classify, persist, graph) is logged so a
    failed run can be retried from the last completed stage and operators can
    see where a document stalled.  No contract or clause text is stored here.
    """
    started_at = started_at or utc_now_iso()
    if finished_at is None and status in (
        PROCESSING_STATUS_SUCCEEDED,
        PROCESSING_STATUS_FAILED,
    ):
        finished_at = utc_now_iso()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO processing_log
               (contract_id, version_id, stage, status, attempt_no,
                started_at, finished_at, error_type, error_message,
                correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                contract_id,
                version_id,
                stage,
                status,
                attempt_no,
                started_at,
                finished_at,
                error_type,
                error_message,
                correlation_id,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def next_processing_attempt(contract_id, stage):
    """Return the next attempt number for one stage of one contract.

    Used by retries so every attempt is numbered and the log remains a
    complete, ordered history rather than overwriting the failed attempt.
    """
    conn = get_conn()
    try:
        if not _table_exists(conn, "processing_log"):
            return 1
        row = conn.execute(
            """SELECT COALESCE(MAX(attempt_no), 0)
               FROM processing_log WHERE contract_id = ? AND stage = ?""",
            (contract_id, stage),
        ).fetchone()
        return int(row[0] or 0) + 1
    finally:
        conn.close()


def list_processing_log(contract_id):
    """Return the ingestion-stage history for one contract, oldest first.

    A legacy read-only database without ``processing_log`` returns an empty
    list rather than failing the whole contract view.
    """
    conn = get_conn()
    try:
        if not _table_exists(conn, "processing_log"):
            return []
        rows = conn.execute(
            """SELECT log_id, contract_id, version_id, stage, status,
                      attempt_no, started_at, finished_at, error_type,
                      error_message, correlation_id
               FROM processing_log
               WHERE contract_id = ?
               ORDER BY log_id ASC""",
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_processing_status(contract_id, processing_status):
    """Set one contract's processing status. Returns True when a row changed.

    Authorization and audit recording remain the caller's responsibility.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE contracts SET processing_status = ? WHERE contract_id = ?",
            (processing_status, contract_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def link_clause_hierarchy(contract_id):
    """Rebuild parent_id links for one contract's clauses.

    A sub-clause names its parent in metadata["parent_section"].  This walks
    the stored clauses, matches each sub-clause to its parent by section
    number, and writes parent_id.  Clauses with no matching parent keep a
    NULL parent_id (fail safe: a link is never invented).

    Returns the number of links written.  Safe to run repeatedly.
    """
    import json as _json

    conn = get_conn()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """SELECT clause_id, section_number, metadata
               FROM clauses WHERE contract_id = ?""",
            (contract_id,),
        ).fetchall()
        by_section = {}
        for row in rows:
            if row["section_number"] is not None:
                by_section[row["section_number"]] = row["clause_id"]
        links = 0
        for row in rows:
            parent_section = None
            raw_meta = row["metadata"]
            if isinstance(raw_meta, str):
                try:
                    raw_meta = _json.loads(raw_meta)
                except (TypeError, ValueError):
                    raw_meta = {}
            if isinstance(raw_meta, dict):
                parent_section = raw_meta.get("parent_section")
            if not parent_section or parent_section not in by_section:
                continue
            parent_id = by_section[parent_section]
            if parent_id == row["clause_id"]:
                continue
            cur.execute(
                "UPDATE clauses SET parent_id = ? WHERE clause_id = ?",
                (parent_id, row["clause_id"]),
            )
            links += 1
        conn.commit()
        return links
    finally:
        conn.close()


def list_clauses(contract_id):
    """
    Return one contract's clauses ordered by section number.

    Each row is a dict with clause_id, contract_id, section_number,
    clause_text, metadata, version_id and clause_sha256. The last two are
    explicit ``NULL`` values on a legacy database that predates those
    columns; no read path migrates or rewrites it. Metadata is parsed back
    into a Python value; rows with missing or invalid JSON keep the stored
    value / get {}. The connection is always closed, including on errors.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        columns = _table_columns(conn, "clauses")
        version_expr = (
            "version_id" if "version_id" in columns
            else "NULL AS version_id"
        )
        hash_expr = (
            "clause_sha256" if "clause_sha256" in columns
            else "NULL AS clause_sha256"
        )
        extra_exprs = []
        for col in ("page_number", "char_start", "char_end", "parent_id", "normalized_text"):
            extra_exprs.append(
                col if col in columns else "NULL AS %s" % col
            )
        cur.execute(
            """SELECT clause_id, contract_id, section_number, clause_text,
                      metadata, %s, %s, %s
               FROM clauses
               WHERE contract_id = ?
               ORDER BY CAST(section_number AS INTEGER) ASC, section_number ASC"""
            % (version_expr, hash_expr, ", ".join(extra_exprs)),
            (contract_id,),
        )
        results = []
        for row in cur.fetchall():
            clause = dict(row)
            raw_metadata = clause.get("metadata")
            if raw_metadata is None:
                clause["metadata"] = {}
            else:
                try:
                    clause["metadata"] = json.loads(raw_metadata)
                except (TypeError, ValueError):
                    pass  # keep the raw stored value if it is not valid JSON
            results.append(clause)
        return results
    finally:
        conn.close()


def get_current_version_id(contract_id):
    conn = get_conn()
    try:
        if "current_version_id" not in _contract_columns(conn):
            return None
        row = conn.execute(
            "SELECT current_version_id FROM contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        return row["current_version_id"] if row else None
    finally:
        conn.close()


def get_contract_version(version_id):
    conn = get_conn()
    try:
        if not _table_exists(conn, "contract_versions"):
            return None
        row = conn.execute(
            "SELECT * FROM contract_versions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_contract_versions(contract_id):
    conn = get_conn()
    try:
        if not _table_exists(conn, "contract_versions"):
            return []
        rows = conn.execute(
            """SELECT * FROM contract_versions
               WHERE contract_id = ?
               ORDER BY version_number ASC""",
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_contract_lifecycle_status(contract_id, lifecycle_status, firm_id):
    """Advance the lifecycle status of a firm-scoped contract.

    Returns the updated contract row as a dict, or None when the contract does
    not exist or is outside the firm scope.  The status is validated against
    the known lifecycle set before writing.
    """

    lifecycle_status = valid_lifecycle_status(lifecycle_status)
    if lifecycle_status is None:
        return None
    conn = get_conn()
    try:
        contract = get_contract_for_firm(contract_id, firm_id)
        if contract is None:
            return None
        cur = conn.cursor()
        cur.execute(
            "UPDATE contracts SET lifecycle_status = ? WHERE contract_id = ?",
            (lifecycle_status, contract_id),
        )
        conn.commit()
        return get_contract_for_firm(contract_id, firm_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_clause_history(contract_id, version_id=None):
    conn = get_conn()
    try:
        if not _table_exists(conn, "clause_history"):
            return []
        sql = """SELECT history_id, clause_id, contract_id, version_id,
                        section_number, clause_text, metadata, clause_sha256,
                        archived_at, archive_reason
                 FROM clause_history WHERE contract_id = ?"""
        params = [contract_id]
        if version_id is not None:
            sql += " AND version_id = ?"
            params.append(version_id)
        sql += " ORDER BY history_id ASC"
        results = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            raw_metadata = item.get("metadata")
            if raw_metadata is None:
                item["metadata"] = {}
            else:
                try:
                    item["metadata"] = json.loads(raw_metadata)
                except (TypeError, ValueError):
                    pass
            results.append(item)
        return results
    finally:
        conn.close()


def get_contract_provenance(contract_id):
    """Return the current version/source provenance, or an explicit legacy state.

    Read-only demo databases may predate the version tables.  Nothing is
    created or rewritten here; such a row is reported as
    ``legacy_unclassified`` because its source cannot be verified.
    """

    conn = get_conn()
    try:
        columns = _contract_columns(conn)
        fields = ["contract_id", "filename", "processing_status"]
        for optional in (
            "firm_id",
            "current_version_id",
            "current_graph_build_id",
            "provenance_status",
        ):
            if optional in columns:
                fields.append(optional)
        contract = conn.execute(
            "SELECT %s FROM contracts WHERE contract_id = ?" % ", ".join(fields),
            (contract_id,),
        ).fetchone()
        if contract is None:
            return None
        contract = dict(contract)
        status = contract.get("provenance_status") or PROVENANCE_LEGACY
        version = None
        if _table_exists(conn, "contract_versions"):
            version_id = contract.get("current_version_id")
            if version_id is not None:
                row = conn.execute(
                    "SELECT * FROM contract_versions WHERE version_id = ?",
                    (version_id,),
                ).fetchone()
                version = dict(row) if row else None
            if version is None:
                row = conn.execute(
                    """SELECT * FROM contract_versions
                       WHERE contract_id = ?
                       ORDER BY version_number DESC LIMIT 1""",
                    (contract_id,),
                ).fetchone()
                version = dict(row) if row else None
        if version is not None:
            status = version.get("provenance_status") or status
        return {
            "contract_id": contract_id,
            "filename": contract.get("filename"),
            "processing_status": contract.get("processing_status"),
            "firm_id": contract.get("firm_id"),
            "provenance_status": status,
            "legacy_unclassified": status == PROVENANCE_LEGACY,
            "current_version_id": contract.get("current_version_id"),
            "current_graph_build_id": contract.get("current_graph_build_id"),
            "version": version,
            "source": (
                {
                    "filename": version.get("source_filename"),
                    "path": version.get("source_path"),
                    "sha256": version.get("source_sha256"),
                    "size_bytes": version.get("source_size_bytes"),
                    "mime": version.get("source_mime"),
                    "extracted_text_sha256": version.get(
                        "extracted_text_sha256"
                    ),
                    "source_kind": version.get("source_kind"),
                }
                if version is not None
                else None
            ),
        }
    finally:
        conn.close()


def list_graph_builds(contract_id):
    conn = get_conn()
    try:
        if not _table_exists(conn, "graph_builds"):
            return []
        rows = conn.execute(
            """SELECT * FROM graph_builds
               WHERE contract_id = ?
               ORDER BY build_id ASC""",
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def init_db():
    """Initialize database tables safely. Can run multiple times without deleting data.

    All changes are additive.  Existing rows are never rewritten or assigned a
    guessed source.  A database that predates the integrity migration keeps its
    rows and reports them as ``legacy_unclassified`` until a human explicitly
    re-imports them as a version.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        # contracts: the stable contract identity and current-version pointer.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contracts (
                contract_id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                upload_timestamp TEXT NOT NULL,
                processing_status TEXT NOT NULL DEFAULT 'uploaded',
                raw_text TEXT,
                firm_id INTEGER,
                current_version_id INTEGER,
                current_graph_build_id INTEGER,
                provenance_status TEXT NOT NULL DEFAULT 'legacy_unclassified'
            )
            """
        )

        # Safe migrations: add columns if they are missing on an existing
        # database. Never drops or recreates tables/records and never
        # backfills a source that cannot be verified.
        cur.execute("PRAGMA table_info(contracts)")
        contract_columns = [row[1] for row in cur.fetchall()]
        contract_migrations = (
            ("raw_text", "TEXT"),
            ("firm_id", "INTEGER"),
            ("current_version_id", "INTEGER"),
            ("current_graph_build_id", "INTEGER"),
            (
                "provenance_status",
                "TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            ),
            (
                "lifecycle_status",
                "TEXT NOT NULL DEFAULT 'draft' "
                "CHECK (lifecycle_status IN ('draft', 'under_review', 'approved', "
                "'active', 'expired', 'archived'))",
            ),
        )
        for column, coltype in contract_migrations:
            if column not in contract_columns:
                cur.execute(
                    "ALTER TABLE contracts ADD COLUMN %s %s" % (column, coltype)
                )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_contracts_firm_id ON contracts(firm_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_contracts_lifecycle_status "
            "ON contracts(lifecycle_status)"
        )

        # Authentication / firm membership. These are additive tables only:
        # no existing contract or clause row is rewritten. Demo rows keep
        # firm_id NULL; uploads always receive the server-derived firm id.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS firms (
                firm_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in cur.fetchall()]
        if "password_hash" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS firm_memberships (
                firm_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN (
                    'administrator', 'lawyer', 'paralegal', 'reviewer', 'viewer'
                )),
                created_at TEXT NOT NULL,
                PRIMARY KEY (firm_id, user_id),
                FOREIGN KEY (firm_id) REFERENCES firms(firm_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memberships_user ON firm_memberships(user_id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                active_firm_id INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (active_firm_id) REFERENCES firms(firm_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(user_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON auth_sessions(expires_at)"
        )

        # contract_versions: one immutable provenance record per ingestion or
        # explicit re-import. The version row owns the source hash/path and the
        # extraction metadata, so a clause never has to stand alone.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contract_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                version_number INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                created_by INTEGER,
                firm_id INTEGER,
                source_filename TEXT,
                source_path TEXT,
                source_sha256 TEXT,
                source_size_bytes INTEGER,
                source_mime TEXT,
                extracted_text_sha256 TEXT,
                extraction_status TEXT NOT NULL DEFAULT 'draft',
                clause_splitter_version TEXT,
                provenance_status TEXT NOT NULL DEFAULT 'unverified',
                source_kind TEXT NOT NULL DEFAULT 'programmatic',
                status TEXT NOT NULL DEFAULT 'draft',
                published_at TEXT,
                UNIQUE (contract_id, version_number),
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE,
                FOREIGN KEY (firm_id) REFERENCES firms(firm_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_versions_number "
            "ON contract_versions(contract_id, version_number)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_contract_versions_contract "
            "ON contract_versions(contract_id)"
        )

        # clauses: the materialised current set used by the deterministic graph
        # and impact engine. version_id identifies the source version.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clauses (
                clause_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                section_number TEXT,
                clause_text TEXT,
                metadata TEXT,
                version_id INTEGER,
                clause_sha256 TEXT,
                page_number INTEGER,
                char_start INTEGER,
                char_end INTEGER,
                parent_id INTEGER,
                normalized_text TEXT,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (version_id) REFERENCES contract_versions(version_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES clauses(clause_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        clause_columns = [row[1] for row in cur.execute("PRAGMA table_info(clauses)")]
        for column, coltype in (
            ("version_id", "INTEGER"),
            ("clause_sha256", "TEXT"),
            ("page_number", "INTEGER"),
            ("char_start", "INTEGER"),
            ("char_end", "INTEGER"),
            ("parent_id", "INTEGER"),
            ("normalized_text", "TEXT"),
        ):
            if column not in clause_columns:
                cur.execute(
                    "ALTER TABLE clauses ADD COLUMN %s %s" % (column, coltype)
                )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clauses_contract_id ON clauses(contract_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clauses_version ON clauses(version_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clauses_parent ON clauses(parent_id)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_clauses_section ON clauses(contract_id, section_number)"
        )

        # Phase 4: embedding model metadata, per-clause vectors and an
        # auditable incremental-index run log.  These tables are additive and
        # contain no contract text beyond the clause identity hashes.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_models (
                model_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                distance_metric TEXT NOT NULL DEFAULT 'cosine',
                config_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clause_embeddings (
                embedding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                version_id INTEGER,
                clause_id INTEGER,
                section_number TEXT NOT NULL,
                clause_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                model_key TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                indexed_at TEXT NOT NULL,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id)
                    ON DELETE CASCADE ON UPDATE CASCADE,
                FOREIGN KEY (model_key) REFERENCES embedding_models(model_key)
                    ON DELETE RESTRICT ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_clause_embeddings_identity "
            "ON clause_embeddings(contract_id, IFNULL(version_id, 0), "
            "section_number, model_key)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_clause_embeddings_scope "
            "ON clause_embeddings(model_key, contract_id, version_id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS retrieval_index_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                version_id INTEGER,
                model_key TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                clause_count INTEGER NOT NULL DEFAULT 0,
                embedded_count INTEGER NOT NULL DEFAULT 0,
                reused_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                removed_count INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                error_message TEXT,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id)
                    ON DELETE CASCADE ON UPDATE CASCADE,
                FOREIGN KEY (model_key) REFERENCES embedding_models(model_key)
                    ON DELETE RESTRICT ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_retrieval_runs_contract "
            "ON retrieval_index_runs(contract_id, run_id)"
        )

        # Immutable copy of replaced clause sets. The current set stays in
        # clauses so existing readers do not change.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clause_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                clause_id INTEGER,
                contract_id INTEGER NOT NULL,
                version_id INTEGER,
                section_number TEXT,
                clause_text TEXT,
                metadata TEXT,
                clause_sha256 TEXT,
                archived_at TEXT NOT NULL,
                archive_reason TEXT NOT NULL,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (version_id) REFERENCES contract_versions(version_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_clause_history_contract "
            "ON clause_history(contract_id)"
        )

        # processing_log: per-stage ingestion/extraction/graph tracking for one
        # contract version. Every pipeline attempt is recorded here so failed
        # runs can be retried idempotently and operators can see where a
        # document stalled. It is append-friendly (rows are updated in place for
        # the latest attempt of a stage, but history is preserved via the audit
        # trail) and never contains contract or clause text.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                version_id INTEGER,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_type TEXT,
                error_message TEXT,
                correlation_id TEXT,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (version_id) REFERENCES contract_versions(version_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_processing_log_contract "
            "ON processing_log(contract_id, stage)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_processing_log_status "
            "ON processing_log(status)"
        )

        # graph_builds: provenance for the published graph and diagnostics for
        # failed builds. A failed build never becomes the current graph.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_builds (
                build_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                version_id INTEGER,
                build_version TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                node_count INTEGER,
                edge_count INTEGER,
                build_hash TEXT,
                error_type TEXT,
                error_message TEXT,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (version_id) REFERENCES contract_versions(version_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_builds_contract "
            "ON graph_builds(contract_id)"
        )

        # nodes: graph nodes (referencing clauses)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                clause_reference TEXT,
                node_type TEXT,
                contract_id INTEGER,
                detail TEXT,
                build_id INTEGER
            )
            """
        )
        node_columns = [row[1] for row in cur.execute("PRAGMA table_info(nodes)")]
        for column, coltype in (
            ("contract_id", "INTEGER"),
            ("detail", "TEXT"),
            ("build_id", "INTEGER"),
        ):
            if column not in node_columns:
                cur.execute(
                    "ALTER TABLE nodes ADD COLUMN %s %s" % (column, coltype)
                )

        # edges: relationships between nodes
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS edges (
                edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node INTEGER NOT NULL,
                target_node INTEGER NOT NULL,
                relationship_type TEXT,
                contract_id INTEGER,
                build_id INTEGER,
                FOREIGN KEY (source_node) REFERENCES nodes(node_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (target_node) REFERENCES nodes(node_id) ON DELETE RESTRICT ON UPDATE CASCADE
            )
            """
        )
        edge_columns = [row[1] for row in cur.execute("PRAGMA table_info(edges)")]
        for column, coltype in (("contract_id", "INTEGER"), ("build_id", "INTEGER")):
            if column not in edge_columns:
                cur.execute(
                    "ALTER TABLE edges ADD COLUMN %s %s" % (column, coltype)
                )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_build ON edges(build_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_build ON nodes(build_id)")

        # authorities: legal authorities (for Person C)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS authorities (
                authority_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                citation TEXT,
                source_metadata TEXT
            )
            """
        )

        # traces: legacy application activity log retained for compatibility.
        # It is NOT the durable audit trail; see audit_events below.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_traces_contract_id ON traces(contract_id)")

        # audit_events: durable, append-only security and lifecycle evidence.
        # No raw contract text, clause text, token, or secret belongs here.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                firm_id INTEGER,
                actor_user_id INTEGER,
                actor_type TEXT NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                correlation_id TEXT,
                metadata_json TEXT,
                FOREIGN KEY (firm_id) REFERENCES firms(firm_id) ON DELETE SET NULL ON UPDATE CASCADE,
                FOREIGN KEY (actor_user_id) REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_firm_time "
            "ON audit_events(firm_id, occurred_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_action "
            "ON audit_events(action)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_target "
            "ON audit_events(target_type, target_id)"
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only');
            END
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only');
            END
            """
        )

        conn.commit()
    finally:
        conn.close()
