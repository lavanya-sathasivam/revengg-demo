"""Create the demo database, load the synthetic contracts, build the graph.

Run this once per laptop:
    py -3.11 seed.py

IMPORTANT - why this uses demo.db and not app.db
    app.db belongs to Person A's upload flow. A's test_split_clauses.py reads
    contract 1 from app.db and asserts it is a REAL 24-section contract that
    A uploaded. If we seeded our synthetic contracts into app.db we would
    overwrite A's test fixture and break A's branch.

    So branch B uses its own file: demo.db. Both are git-ignored and both are
    rebuilt locally.
"""

import os

import db

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT_DIR = os.path.join(HERE, "data", "contracts")
DEMO_DB = os.path.join(HERE, "demo.db")

CONTRACTS = [
    ("Contract A - Supplier Service Agreement (SYNTHETIC)", "contract_a.txt"),
    ("Contract B - Customer Services Agreement (SYNTHETIC)", "contract_b.txt"),
]


def use_demo_db():
    """Point db.py at demo.db for the rest of this process."""
    db.get_db_path = lambda: DEMO_DB
    return DEMO_DB


# Graph/contract tables in dependency order.  audit_events is deliberately
# absent: the audit trail is append-only, must survive a demo reseed, and must
# never have its AUTOINCREMENT sequence reset (that could reuse event ids).
_RESET_TABLES = (
    "edges",
    "nodes",
    "clauses",
    "clause_history",
    "graph_builds",
    "contract_versions",
    "contracts",
    "traces",
)


def reset_demo_tables(conn):
    """Clear synthetic contract/graph data without touching audit history.

    Foreign keys are restrictive, so the order matters: graph rows, clauses,
    history, builds and versions must go before the contract identity.  Only
    the sequences for the cleared tables are reset; auth and audit sequences
    are left alone.
    """

    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table in _RESET_TABLES:
        if table in existing:
            conn.execute("DELETE FROM %s" % table)

    reset_tables = [table for table in _RESET_TABLES if table in existing]
    if reset_tables and "sqlite_sequence" in existing:
        marks = ",".join("?" * len(reset_tables))
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN (%s)" % marks,
            reset_tables,
        )


def seed(reset=True, db_path=None):
    """Load the synthetic contracts and build their graphs.

    db_path: optional explicit database file. When given (the test suite
    uses a temp file) it wins over demo.db.
    """
    if db_path:
        db.get_db_path = lambda: db_path
    else:
        use_demo_db()

    import adapter
    import graph

    adapter.init_db()

    if reset:
        conn = adapter.raw_connection()
        try:
            reset_demo_tables(conn)
            conn.commit()
        finally:
            conn.close()

    made = []
    for title, filename in CONTRACTS:
        path = os.path.join(CONTRACT_DIR, filename)
        if not os.path.exists(path):
            print("missing %s - skipping" % filename)
            continue
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()

        cid = adapter.create_contract(
            filename,
            text,
            status="extracted",
            provenance_status=db.PROVENANCE_SYNTHETIC,
        )
        n_clauses = graph.ingest_contract(cid, text)
        stats = graph.build_graph(cid, use_llm=False)
        made.append((cid, title, n_clauses, stats))
        print("Contract %d: %s" % (cid, title))
        print("   clauses=%d nodes=%d edges=%d" %
              (stats["clauses"], stats["nodes"], stats["edges"]))

    return made


if __name__ == "__main__":
    seed()
    print("\ndone. now run:  py -3.11 cli.py --list")
