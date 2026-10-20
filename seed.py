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
            for table in ("edges", "nodes", "clauses", "contracts", "traces"):
                conn.execute("DELETE FROM %s" % table)
            conn.execute("DELETE FROM sqlite_sequence")
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

        cid = adapter.create_contract(filename, text, status="extracted")
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
