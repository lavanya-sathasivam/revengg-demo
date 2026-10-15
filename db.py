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
