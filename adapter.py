"""THE SEAM between branch B (graph) and branch A (backend).

WHY THIS FILE EXISTS
    Person A owns db.py and extract.py. A is free to rename tables, change
    helper names, swap SQLite for SQLAlchemy, whatever. If branch B's code
    called db.py directly, every one of those changes would break branch B
    and we would spend a day fixing merge conflicts instead of building
    features.

    So: branch B NEVER imports db for anything except get_conn() and
    get_db_path(). Branch B imports adapter. adapter is the only file on this
    branch that knows how the database is stored.

    If A changes the schema, only THIS FILE changes.

Everything here returns plain dicts and lists, so nothing downstream
depends on sqlite3 behaviour.

SCHEMA NOTES (Person A's db.py, verified against origin/a-backend)
    contracts(contract_id, filename, upload_timestamp, processing_status, raw_text)
    clauses  (clause_id, contract_id, section_number, clause_text, metadata)
    nodes    (node_id, clause_reference, node_type)
    edges    (edge_id, source_node, target_node, relationship_type)

    A's nodes/edges were designed for a node-to-node graph. Branch B needs
    three extra things, added here as SAFE ADDITIVE MIGRATIONS (the same
    ALTER TABLE pattern A already uses for contracts.raw_text):
        nodes.contract_id  - so "7.2" in Contract A and "7.2" in Contract B
                             are different nodes
        nodes.detail       - the human-readable text of the node
        edges.contract_id  - so edges can be scoped to one contract
    Nothing is dropped or rewritten. Person A should fold these three
    columns into db.init_db() (see docs/handoff-b.md).
"""

import json

import db

# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def init_db():
    db.init_db()


def get_db_path():
    return db.get_db_path()


def raw_connection():
    """Escape hatch for maintenance tasks (seeding, migrations)."""
    return db.get_conn()


# --------------------------------------------------------------------------
# safe additive migrations for the graph tables
# --------------------------------------------------------------------------
_GRAPH_MIGRATIONS = [
    ("nodes", "contract_id", "INTEGER"),
    ("nodes", "detail", "TEXT"),
    ("edges", "contract_id", "INTEGER"),
]

_schema_ready = False


def ensure_graph_schema(conn=None):
    """Make sure the graph tables exist and have the columns branch B needs.

    - If the tables do not exist at all, we call db.init_db() (A's function),
      so we never leave a half-created database behind. A 0-byte database file
      is WORSE than no file: Person A's test suite skips its real-document
      tests only when app.db is absent, so an empty app.db breaks their run.
    - The three extra columns are added with safe ALTER TABLE migrations (the
      same pattern A already used for contracts.raw_text). Nothing is dropped.

    Safe to call repeatedly.
    """
    global _schema_ready
    if _schema_ready and conn is None:
        return
    own = conn is None
    conn = conn or db.get_conn()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "nodes" not in tables or "edges" not in tables:
            # the database has not been initialised yet - let A create it
            conn.commit()
            db.init_db()
            conn = db.get_conn()
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, column, coltype in _GRAPH_MIGRATIONS:
            if table not in tables:
                continue
            existing = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
            if column not in existing:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coltype))
        conn.commit()
        _schema_ready = True
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------
def list_contracts():
    return db.list_contracts()


def get_contract(contract_id):
    return db.get_contract(contract_id)


def create_contract(filename, raw_text, status="extracted"):
    """Insert one contract row plus one trace row, return the new id.

    A's db.py has no contract INSERT helper (the upload route does it
    inline), so the seam provides one, following A's transaction pattern.
    """
    from datetime import datetime, timezone

    conn = db.get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO contracts (filename, upload_timestamp, processing_status, raw_text)
               VALUES (?, ?, ?, ?)""",
            (filename, now, status, raw_text),
        )
        contract_id = cur.lastrowid
        conn.execute(
            """INSERT INTO traces (contract_id, action, details, timestamp)
               VALUES (?, ?, ?, ?)""",
            (contract_id, "ingest", "Stored %d characters of extracted text."
             % len(raw_text or ""), now),
        )
        conn.commit()
        return contract_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# clauses
# --------------------------------------------------------------------------
def save_clauses(contract_id, clauses):
    """clauses: [{'section_number', 'clause_text', 'metadata'?}] -> row count."""
    return db.save_clauses(contract_id, clauses)


def list_clauses(contract_id):
    """A's list_clauses(), re-sorted into true clause order.

    A orders by CAST(section_number AS INTEGER), which puts '2.10' before
    '2.2' and 'SCHEDULE A' before section 1. We re-sort numerically so the
    graph is built in reading order.
    """
    import clauses as clause_tools

    rows = db.list_clauses(contract_id)
    return sorted(rows, key=lambda r: clause_tools.sec_sort_key(r["section_number"]))


def get_clause(contract_id, sec_no):
    for row in list_clauses(contract_id):
        if row["section_number"] == sec_no:
            return row
    return None


def clause_metadata(row):
    """Parsed metadata dict for a clause row (never raises)."""
    meta = row.get("metadata")
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta:
        try:
            parsed = json.loads(meta)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


# --------------------------------------------------------------------------
# relationship model
# --------------------------------------------------------------------------
def clear_graph(contract_id):
    """Delete the nodes and edges of one contract so the graph can be rebuilt."""
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        node_ids = [r[0] for r in conn.execute(
            "SELECT node_id FROM nodes WHERE contract_id = ?", (contract_id,))]
        if node_ids:
            marks = ",".join("?" * len(node_ids))
            conn.execute("DELETE FROM edges WHERE source_node IN (%s) "
                         "OR target_node IN (%s)" % (marks, marks),
                         node_ids + node_ids)
        conn.execute("DELETE FROM nodes WHERE contract_id = ?", (contract_id,))
        conn.commit()
    finally:
        conn.close()


def insert_clause_node(contract_id, sec_no, clause_type, detail=""):
    """Create the node that represents a clause itself. Returns node_id."""
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        cur = conn.execute(
            """INSERT INTO nodes (clause_reference, node_type, contract_id, detail)
               VALUES (?, ?, ?, ?)""",
            (sec_no, "clause:%s" % clause_type, contract_id, detail),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_element_node(contract_id, ref, node_type, detail=""):
    """Create a node for something a clause creates (a payment duty, ...)."""
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        cur = conn.execute(
            """INSERT INTO nodes (clause_reference, node_type, contract_id, detail)
               VALUES (?, ?, ?, ?)""",
            (ref, node_type, contract_id, detail),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_edge(contract_id, source_node, target_node, relationship_type):
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        conn.execute(
            """INSERT INTO edges (source_node, target_node, relationship_type, contract_id)
               VALUES (?, ?, ?, ?)""",
            (source_node, target_node, relationship_type, contract_id),
        )
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn, table):
    return table in {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def get_nodes(contract_id):
    """All graph nodes of one contract. Empty list if the table does not exist."""
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        if not _table_exists(conn, "nodes"):
            return []
        rows = conn.execute(
            """SELECT node_id, clause_reference, node_type, contract_id, detail
               FROM nodes WHERE contract_id = ? ORDER BY node_id""",
            (contract_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_edges(contract_id):
    """All graph edges of one contract, as {'src', 'dst', 'rel'} dicts.

    Edges in A's schema point at node ids. We resolve them back to clause
    references so the graph logic can stay readable. Element nodes are
    referenced as '7.2::payment'; clause nodes as '7.2'.
    """
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        if not _table_exists(conn, "edges"):
            return []
        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel
               FROM edges e
               JOIN nodes s ON s.node_id = e.source_node
               JOIN nodes t ON t.node_id = e.target_node
               WHERE e.contract_id = ?
               ORDER BY e.edge_id""",
            (contract_id,),
        ).fetchall()
        return [{"src": r["src_ref"], "dst": r["dst_ref"], "rel": r["rel"]}
                for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# traces
# --------------------------------------------------------------------------
def save_trace(contract_id, action, details):
    from datetime import datetime, timezone

    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO traces (contract_id, action, details, timestamp)
               VALUES (?, ?, ?, ?)""",
            (contract_id, action, details, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
