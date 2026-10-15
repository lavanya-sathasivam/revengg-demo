import json
import os
import sqlite3
from datetime import datetime


def get_db_path():
    """Return the path to app.db located next to this db.py file."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "app.db")


def get_conn():
    """Return a new SQLite connection with safe defaults."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    # Enable foreign key enforcement
    conn.execute("PRAGMA foreign_keys = ON")
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
                raw_text TEXT
            )
            """
        )

        # Safe migration: add raw_text to contracts if it is missing on an
        # existing database. Never drops or recreates tables/records.
        cur.execute("PRAGMA table_info(contracts)")
        contract_columns = [row[1] for row in cur.fetchall()]
        if "raw_text" not in contract_columns:
            cur.execute("ALTER TABLE contracts ADD COLUMN raw_text TEXT")

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
