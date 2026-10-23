import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


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
                      processing_status, raw_text
               FROM contracts
               WHERE contract_id = ?""",
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
            """SELECT contract_id, filename, upload_timestamp, processing_status
               FROM contracts
               ORDER BY contract_id ASC"""
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _contract_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(contracts)")}


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
                      processing_status, raw_text
               FROM contracts
               WHERE contract_id = ?""" + scope_sql,
            (contract_id,) + params,
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_contracts_for_firm(firm_id):
    """Return lightweight contract rows scoped to the demo or one firm."""

    conn = get_conn()
    try:
        scope_sql, params = _contract_scope(conn, firm_id)
        cur = conn.execute(
            """SELECT contract_id, filename, upload_timestamp, processing_status
               FROM contracts
               WHERE 1 = 1""" + scope_sql + """
               ORDER BY contract_id ASC""",
            params,
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def save_clauses(contract_id, clauses):
    """
    Store the clauses of one contract, replacing any previously stored set.

    `clauses` is the list of dicts produced by extract.split_clauses():
    each item has "section_number", "clause_text" and optionally "metadata".
    Missing metadata is stored as {} (serialized as JSON text, matching the
    TEXT metadata column).

    Duplicate safety: the clauses table has no UNIQUE constraint, so this
    uses replace-all semantics instead - inside ONE transaction every
    existing clause row for this contract is deleted and the new set is
    inserted. Saving the same contract's clauses twice can therefore never
    create duplicate rows, and the stored text always matches the latest
    call. SQLite allows a single writer at a time, so concurrent saves
    serialize and the final state is exactly one complete set.

    A foreign key on clauses.contract_id is enforced (PRAGMA foreign_keys=ON
    in get_conn), so an unknown contract_id makes the INSERT fail with
    sqlite3.IntegrityError and the whole transaction is rolled back - no
    partial rows are left behind. Any other failure rolls back as well.

    Returns the number of clause rows saved (0 for an empty list, which
    clears previously stored clauses for the contract).
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        # The DELETE below starts the transaction; inserts join the same one.
        cur.execute("DELETE FROM clauses WHERE contract_id = ?", (contract_id,))
        for item in clauses:
            metadata = item.get("metadata") or {}
            cur.execute(
                """INSERT INTO clauses (contract_id, section_number, clause_text, metadata)
                   VALUES (?, ?, ?, ?)""",
                (
                    contract_id,
                    item["section_number"],
                    item["clause_text"],
                    json.dumps(metadata),
                ),
            )
        conn.commit()
        return len(clauses)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_clauses(contract_id):
    """
    Return one contract's clauses ordered by section number.

    Each row is a dict with clause_id, contract_id, section_number,
    clause_text and metadata (metadata is parsed back into a Python value;
    rows with missing or invalid JSON keep the stored value / get {}).
    The connection is always closed, including on errors.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT clause_id, contract_id, section_number, clause_text, metadata
               FROM clauses
               WHERE contract_id = ?
               ORDER BY CAST(section_number AS INTEGER) ASC, section_number ASC""",
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


def init_db():
    """Initialize database tables safely. Can run multiple times without deleting data."""
    conn = get_conn()
    try:
        cur = conn.cursor()

        # contracts: metadata about uploaded contracts
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contracts (
                contract_id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                upload_timestamp TEXT NOT NULL,
                processing_status TEXT NOT NULL DEFAULT 'uploaded',
                raw_text TEXT,
                firm_id INTEGER
            )
            """
        )

        # Safe migrations: add raw_text/firm_id if they are missing on an
        # existing database. Never drops or recreates tables/records.
        cur.execute("PRAGMA table_info(contracts)")
        contract_columns = [row[1] for row in cur.fetchall()]
        if "raw_text" not in contract_columns:
            cur.execute("ALTER TABLE contracts ADD COLUMN raw_text TEXT")
        if "firm_id" not in contract_columns:
            cur.execute("ALTER TABLE contracts ADD COLUMN firm_id INTEGER")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_contracts_firm_id ON contracts(firm_id)"
        )

        # Authentication / firm membership.  These are additive tables only:
        # no existing contract or clause row is rewritten.  Demo rows keep
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

        # clauses: individual sections extracted from contracts
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clauses (
                clause_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                section_number TEXT,
                clause_text TEXT,
                metadata TEXT,
                FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT ON UPDATE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clauses_contract_id ON clauses(contract_id)")

        # nodes: graph nodes (referencing clauses)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                clause_reference TEXT,
                node_type TEXT
            )
            """
        )

        # edges: relationships between nodes
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS edges (
                edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node INTEGER NOT NULL,
                target_node INTEGER NOT NULL,
                relationship_type TEXT,
                FOREIGN KEY (source_node) REFERENCES nodes(node_id) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (target_node) REFERENCES nodes(node_id) ON DELETE RESTRICT ON UPDATE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node)")

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

        # traces: audit trail of actions
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

        conn.commit()
    finally:
        conn.close()
