"""THE SEAM between branch B (graph) and branch A (backend).

WHY THIS FILE EXISTS
    Person A owns db.py and extract.py. A is free to rename tables, change
    helper names, swap SQLite for SQLAlchemy, whatever. If branch B's code
    called db.py directly, every one of those changes would break branch B
    and we would spend a day fixing merge conflicts instead of building
    features.

    So: branch B NEVER imports db for anything except the lifecycle helpers
    this seam exposes. Branch B imports adapter. adapter is the only file on
    this branch that knows how the database is stored.

    If A changes the schema, only THIS FILE changes.

Everything here returns plain dicts and lists, so nothing downstream
depends on sqlite3 behaviour.

SCHEMA NOTES
    contracts(contract_id, filename, upload_timestamp, processing_status,
              raw_text, firm_id, current_version_id, current_graph_build_id,
              provenance_status)
    clauses  (clause_id, contract_id, section_number, clause_text, metadata,
              version_id, clause_sha256)
    nodes    (node_id, clause_reference, node_type, contract_id, detail,
              build_id)
    edges    (edge_id, source_node, target_node, relationship_type,
              contract_id, build_id)

    The graph columns were originally safe additive migrations on branch B.
    db.init_db() now creates them, and ensure_graph_schema() keeps older
    writable databases compatible.  A read-only legacy database is never
    migrated: its missing columns are handled by explicit fallback queries so
    demo mode cannot fail just because it is old.
"""

import hashlib
import json

import db

GRAPH_BUILD_VERSION = db.SCHEMA_INTEGRITY_VERSION
GRAPH_STATUS_BUILDING = "building"
GRAPH_STATUS_PUBLISHED = "published"
GRAPH_STATUS_FAILED = "failed"

VALID_RELATIONSHIPS = frozenset(
    {"creates", "depends_on", "subject_to", "causes", "references"}
)


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
    ("nodes", "build_id", "INTEGER"),
    ("edges", "contract_id", "INTEGER"),
    ("edges", "build_id", "INTEGER"),
]


def _columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _table_exists(conn, table):
    return table in {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _is_read_only(conn):
    """Return True when this connection must not run DDL.

    The explicit application configuration is checked first, then SQLite's
    own query_only flag.  This matters for demo.db: it predates the build
    columns, and a PRAGMA/ALTER attempt there would turn a readable demo into
    a startup failure.
    """

    config = db.get_database_config()
    if config is not None and config.read_only:
        return True
    row = conn.execute("PRAGMA query_only").fetchone()
    return bool(row and row[0])


def ensure_graph_schema(conn=None):
    """Make sure the graph tables exist and have the columns branch B needs.

    - If the tables do not exist at all, we call db.init_db() (A's function),
      so we never leave a half-created database behind. A 0-byte database file
      is WORSE than no file: Person A's test suite skips its real-document
      tests only when app.db is absent, so an empty app.db breaks their run.
    - The extra columns are added with safe ALTER TABLE migrations (the same
      pattern A already used for contracts.raw_text). Nothing is dropped.
    - A read-only legacy database is left untouched. Its readers use fallback
      queries instead of pretending a migration happened.

    Safe to call repeatedly.  The earlier process-global "schema is ready"
    flag was removed: it could skip migrations for a different database in
    the same process.
    """

    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        if _is_read_only(conn):
            return

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "nodes" not in tables or "edges" not in tables:
            if not own:
                raise RuntimeError(
                    "Graph schema is not initialized; call adapter.init_db() first."
                )
            conn.commit()
            conn.close()
            db.init_db()
            conn = db.get_conn()
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        for table, column, coltype in _GRAPH_MIGRATIONS:
            if table not in tables:
                continue
            existing = _columns(conn, table)
            if column not in existing:
                conn.execute(
                    "ALTER TABLE %s ADD COLUMN %s %s"
                    % (table, column, coltype)
                )

        contract_columns = _columns(conn, "contracts")
        required_contract_columns = {
            "current_version_id",
            "current_graph_build_id",
            "provenance_status",
        }
        if not required_contract_columns.issubset(contract_columns):
            if not own:
                raise RuntimeError(
                    "Contract integrity columns are missing; call adapter.init_db() first."
                )
            conn.commit()
            conn.close()
            db.init_db()
            conn = db.get_conn()

        conn.commit()
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


def create_contract(
    filename,
    raw_text,
    status="extracted",
    firm_id=None,
    provenance_status=db.PROVENANCE_UNVERIFIED,
):
    """Insert one contract row plus one trace row, return the new id.

    A's db.py has no contract INSERT helper (the upload route does it
    inline), so the seam provides one, following A's transaction pattern.
    ``firm_id=None`` keeps a row in the synthetic/demo scope.  The version row
    is created by save_clauses(), which keeps older callers working.
    """
    conn = db.get_conn()
    try:
        now = db.utc_now_iso()
        cur = conn.execute(
            """INSERT INTO contracts
                   (filename, upload_timestamp, processing_status, raw_text,
                    firm_id, provenance_status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (filename, now, status, raw_text, firm_id, provenance_status),
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


def get_current_version_id(contract_id):
    return db.get_current_version_id(contract_id)


def get_contract_provenance(contract_id):
    return db.get_contract_provenance(contract_id)


# --------------------------------------------------------------------------
# clauses
# --------------------------------------------------------------------------
def save_clauses(contract_id, clauses, version_id=None, audit_event=None):
    """clauses: [{'section_number', 'clause_text', 'metadata'?}] -> row count."""
    return db.save_clauses(
        contract_id, clauses, version_id=version_id, audit_event=audit_event
    )


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
# relationships
# --------------------------------------------------------------------------
def compute_graph_hash(nodes, edges, build_version=GRAPH_BUILD_VERSION):
    """Return a stable hash of the graph content a build publishes.

    Only clause references, node types, details, and edge endpoints/types are
    hashed.  Database ids are deliberately excluded: rebuilding identical
    input must produce the same hash even though SQLite allocates new ids.
    """

    node_entries = sorted(
        (
            str(node.get("ref") or ""),
            str(node.get("node_type") or ""),
            str(node.get("detail") or ""),
        )
        for node in nodes
    )
    edge_entries = sorted(
        (
            node_entries_key(nodes[edge["source"]]),
            node_entries_key(nodes[edge["target"]]),
            str(edge.get("relationship_type") or ""),
        )
        for edge in edges
    )
    payload = json.dumps(
        {
            "build_version": build_version,
            "nodes": node_entries,
            "edges": edge_entries,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def node_entries_key(node):
    return str(node.get("ref") or "")


def clear_graph(contract_id):
    """Delete the nodes and edges of one contract.

    This is a maintenance helper only.  A failed build must NOT call it: the
    previous published graph is left intact by replace_graph()'s rollback.
    """

    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        node_ids = [
            row[0]
            for row in conn.execute(
                "SELECT node_id FROM nodes WHERE contract_id = ?",
                (contract_id,),
            )
        ]
        if node_ids:
            marks = ",".join("?" * len(node_ids))
            conn.execute(
                "DELETE FROM edges WHERE source_node IN (%s) "
                "OR target_node IN (%s)" % (marks, marks),
                node_ids + node_ids,
            )
        conn.execute("DELETE FROM edges WHERE contract_id = ?", (contract_id,))
        conn.execute("DELETE FROM nodes WHERE contract_id = ?", (contract_id,))
        if "current_graph_build_id" in _columns(conn, "contracts"):
            conn.execute(
                "UPDATE contracts SET current_graph_build_id = NULL "
                "WHERE contract_id = ?",
                (contract_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _validate_graph_input(contract_id, nodes, edges):
    normalized_nodes = []
    for index, node in enumerate(nodes):
        ref = str(node.get("ref") or node.get("clause_reference") or "").strip()
        node_type = str(node.get("node_type") or node.get("type") or "").strip()
        if not ref or not node_type:
            raise ValueError(
                "Graph node %d must have a clause reference and node type." % index
            )
        normalized_nodes.append(
            {
                "ref": ref,
                "node_type": node_type,
                "detail": str(node.get("detail") or ""),
            }
        )

    normalized_edges = []
    for index, edge in enumerate(edges):
        try:
            source = int(edge["source"])
            target = int(edge["target"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Graph edge %d must reference local node indexes." % index
            ) from exc
        relationship = str(
            edge.get("relationship_type") or edge.get("rel") or ""
        ).strip()
        if not (0 <= source < len(normalized_nodes)):
            raise ValueError(
                "Graph edge %d has an invalid source node." % index
            )
        if not (0 <= target < len(normalized_nodes)):
            raise ValueError(
                "Graph edge %d has an invalid target node." % index
            )
        if relationship not in VALID_RELATIONSHIPS:
            raise ValueError(
                "Graph edge %d has an unknown relationship %r."
                % (index, relationship)
            )
        normalized_edges.append(
            {
                "source": source,
                "target": target,
                "relationship_type": relationship,
            }
        )

    return normalized_nodes, normalized_edges


def replace_graph(
    contract_id,
    nodes,
    edges,
    *,
    version_id=None,
    build_version=GRAPH_BUILD_VERSION,
    build_hash=None,
    started_at=None,
    audit_event=None,
):
    """Publish a fully-built graph in one transaction.

    The caller supplies the complete node and edge set in memory.  Nothing is
    written until validation has passed and the transaction commits, so a
    reader never sees a partial graph.  On failure the previous published
    graph, ``contracts.current_graph_build_id``, and all stored rows remain
    exactly as they were.

    ``nodes`` items use ``ref``, ``node_type`` and ``detail``.  ``edges``
    items use ``source``/``target`` indexes into that list plus
    ``relationship_type``.  Ids are allocated only after validation.
    """

    ensure_graph_schema()
    normalized_nodes, normalized_edges = _validate_graph_input(
        contract_id, nodes, edges
    )
    if build_hash is None:
        build_hash = compute_graph_hash(
            normalized_nodes, normalized_edges, build_version=build_version
        )
    started_at = started_at or db.utc_now_iso()

    conn = db.get_conn()
    try:
        cur = conn.cursor()
        contract = cur.execute(
            "SELECT contract_id FROM contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if contract is None:
            raise ValueError("Contract does not exist.")

        if version_id is not None:
            version = cur.execute(
                "SELECT contract_id FROM contract_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            if version is None or version["contract_id"] != contract_id:
                raise ValueError(
                    "Graph version does not belong to this contract."
                )

        cur.execute(
            """INSERT INTO graph_builds
                   (contract_id, version_id, build_version, status,
                    started_at, finished_at, node_count, edge_count,
                    build_hash, error_type, error_message)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)""",
            (
                contract_id,
                version_id,
                build_version,
                GRAPH_STATUS_BUILDING,
                started_at,
            ),
        )
        build_id = cur.lastrowid

        # Remove the previous published graph and any corrupt edge that still
        # points at one of this contract's nodes.  The delete and the insert
        # are in the same transaction, so the old graph remains visible until
        # commit.
        node_ids = [
            row[0]
            for row in cur.execute(
                "SELECT node_id FROM nodes WHERE contract_id = ?",
                (contract_id,),
            )
        ]
        if node_ids:
            marks = ",".join("?" * len(node_ids))
            cur.execute(
                "DELETE FROM edges WHERE source_node IN (%s) "
                "OR target_node IN (%s)" % (marks, marks),
                node_ids + node_ids,
            )
        cur.execute("DELETE FROM edges WHERE contract_id = ?", (contract_id,))
        cur.execute("DELETE FROM nodes WHERE contract_id = ?", (contract_id,))

        node_ids = []
        for node in normalized_nodes:
            inserted = cur.execute(
                """INSERT INTO nodes
                       (clause_reference, node_type, contract_id, detail, build_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    node["ref"],
                    node["node_type"],
                    contract_id,
                    node["detail"],
                    build_id,
                ),
            )
            node_ids.append(inserted.lastrowid)

        for edge in normalized_edges:
            cur.execute(
                """INSERT INTO edges
                       (source_node, target_node, relationship_type,
                        contract_id, build_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    node_ids[edge["source"]],
                    node_ids[edge["target"]],
                    edge["relationship_type"],
                    contract_id,
                    build_id,
                ),
            )

        finished_at = db.utc_now_iso()
        cur.execute(
            """UPDATE graph_builds
               SET status = ?, finished_at = ?, node_count = ?,
                   edge_count = ?, build_hash = ?
               WHERE build_id = ?""",
            (
                GRAPH_STATUS_PUBLISHED,
                finished_at,
                len(normalized_nodes),
                len(normalized_edges),
                build_hash,
                build_id,
            ),
        )
        if "current_graph_build_id" in _columns(conn, "contracts"):
            cur.execute(
                "UPDATE contracts SET current_graph_build_id = ? "
                "WHERE contract_id = ?",
                (build_id, contract_id),
            )

        if audit_event:
            import audit

            event = dict(audit_event)
            metadata = dict(event.pop("metadata", {}) or {})
            metadata.setdefault("contract_id", contract_id)
            metadata.setdefault("version_id", version_id)
            metadata.setdefault("build_id", build_id)
            metadata.setdefault("build_version", build_version)
            metadata.setdefault("build_hash", build_hash)
            metadata.setdefault("node_count", len(normalized_nodes))
            metadata.setdefault("edge_count", len(normalized_edges))
            audit.record_event(
                conn=conn,
                commit=False,
                target_type=event.pop("target_type", "contract"),
                target_id=event.pop("target_id", contract_id),
                metadata=metadata,
                **event,
            )

        conn.commit()
        return {
            "build_id": build_id,
            "build_hash": build_hash,
            "node_count": len(normalized_nodes),
            "edge_count": len(normalized_edges),
            "version_id": version_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_graph_failure(
    contract_id,
    error,
    *,
    version_id=None,
    build_version=GRAPH_BUILD_VERSION,
    started_at=None,
    audit_event=None,
):
    """Record a failed build without touching the last published graph."""

    ensure_graph_schema()
    started_at = started_at or db.utc_now_iso()
    message = str(error).replace("\r", " ").replace("\n", " ")[:500]

    conn = db.get_conn()
    try:
        cur = conn.cursor()
        contract = cur.execute(
            "SELECT contract_id FROM contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if contract is None:
            return None
        if version_id is not None:
            version = cur.execute(
                "SELECT contract_id FROM contract_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            if version is None or version["contract_id"] != contract_id:
                version_id = None

        cur.execute(
            """INSERT INTO graph_builds
                   (contract_id, version_id, build_version, status,
                    started_at, finished_at, node_count, edge_count,
                    build_hash, error_type, error_message)
               VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)""",
            (
                contract_id,
                version_id,
                build_version,
                GRAPH_STATUS_FAILED,
                started_at,
                db.utc_now_iso(),
                type(error).__name__,
                message,
            ),
        )
        build_id = cur.lastrowid

        if audit_event:
            import audit

            event = dict(audit_event)
            metadata = dict(event.pop("metadata", {}) or {})
            metadata.setdefault("contract_id", contract_id)
            metadata.setdefault("version_id", version_id)
            metadata.setdefault("build_id", build_id)
            metadata.setdefault("build_version", build_version)
            metadata.setdefault("error_type", type(error).__name__)
            # A diagnostics row is more important than the audit row here: the
            # caller already has the original exception and must not lose it
            # because the audit write failed.
            audit.record_event(
                conn=conn,
                commit=False,
                swallow=True,
                target_type=event.pop("target_type", "contract"),
                target_id=event.pop("target_id", contract_id),
                metadata=metadata,
                **event,
            )

        conn.commit()
        return build_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_clause_node(contract_id, sec_no, clause_type, detail="", build_id=None):
    """Create the node that represents a clause itself. Returns node_id.

    Kept for compatibility with older branch-B callers.  New builds should use
    replace_graph() so publication is atomic.
    """
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        cur = conn.execute(
            """INSERT INTO nodes
                   (clause_reference, node_type, contract_id, detail, build_id)
               VALUES (?, ?, ?, ?, ?)""",
            (sec_no, "clause:%s" % clause_type, contract_id, detail, build_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_element_node(contract_id, ref, node_type, detail="", build_id=None):
    """Create a node for something a clause creates (a payment duty, ...)."""
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        cur = conn.execute(
            """INSERT INTO nodes
                   (clause_reference, node_type, contract_id, detail, build_id)
               VALUES (?, ?, ?, ?, ?)""",
            (ref, node_type, contract_id, detail, build_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_edge(
    contract_id, source_node, target_node, relationship_type, build_id=None
):
    conn = db.get_conn()
    try:
        ensure_graph_schema(conn)
        conn.execute(
            """INSERT INTO edges
                   (source_node, target_node, relationship_type,
                    contract_id, build_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                source_node,
                target_node,
                relationship_type,
                contract_id,
                build_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_nodes(contract_id):
    """All graph nodes of one contract. Empty list if the table does not exist.

    Read-only legacy databases may lack ``detail`` or ``contract_id``.  We
    never try to migrate them here.  Without ``contract_id`` the rows cannot
    be scoped safely, so the function fails closed with an empty list rather
    than returning every contract's nodes.
    """

    conn = db.get_conn()
    try:
        if _is_read_only(conn):
            if not _table_exists(conn, "nodes"):
                return []
        else:
            ensure_graph_schema(conn)
        if not _table_exists(conn, "nodes"):
            return []
        columns = _columns(conn, "nodes")
        if "contract_id" not in columns:
            return []
        detail_expr = "detail" if "detail" in columns else "NULL AS detail"
        rows = conn.execute(
            """SELECT node_id, clause_reference, node_type, contract_id,
                      %s
               FROM nodes WHERE contract_id = ? ORDER BY node_id"""
            % detail_expr,
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_edges(contract_id):
    """All graph edges of one contract, as {'src', 'dst', 'rel'} dicts.

    Edges in A's schema point at node ids. We resolve them back to clause
    references so the graph logic can stay readable. Element nodes are
    referenced as '7.2::payment'; clause nodes as '7.2'.

    Scope is enforced on the ROW and on BOTH endpoints. It is not enough for
    the edge row to claim contract X: if a corrupt or maliciously inserted edge
    points at a node from contract Y, returning it would leak Y's clause
    reference into X's graph and evidence. Such rows are dropped (fail closed)
    rather than repaired, because the graph cannot prove which contract the
    relationship really belongs to.

    A read-only legacy database that lacks a column is handled by an explicit
    fallback; no DDL is attempted.
    """

    conn = db.get_conn()
    try:
        if _is_read_only(conn):
            if not _table_exists(conn, "edges") or not _table_exists(conn, "nodes"):
                return []
        else:
            ensure_graph_schema(conn)
        if not _table_exists(conn, "edges") or not _table_exists(conn, "nodes"):
            return []

        node_columns = _columns(conn, "nodes")
        if "contract_id" not in node_columns:
            return []
        edge_columns = _columns(conn, "edges")

        conditions = ["s.contract_id = ?", "t.contract_id = ?"]
        params = [contract_id, contract_id]
        if "contract_id" in edge_columns:
            conditions.insert(0, "e.contract_id = ?")
            params.insert(0, contract_id)

        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel
               FROM edges e
               JOIN nodes s ON s.node_id = e.source_node
               JOIN nodes t ON t.node_id = e.target_node
               WHERE %s
               ORDER BY e.edge_id"""
            % " AND ".join(conditions),
            params,
        ).fetchall()
        return [
            {"src": row["src_ref"], "dst": row["dst_ref"], "rel": row["rel"]}
            for row in rows
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# traces and audit-adjacent read helpers
# --------------------------------------------------------------------------
def list_graph_builds(contract_id):
    return db.list_graph_builds(contract_id)


def save_trace(contract_id, action, details):
    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO traces (contract_id, action, details, timestamp)
               VALUES (?, ?, ?, ?)""",
            (contract_id, action, details, db.utc_now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
