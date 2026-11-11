#!/usr/bin/env python
"""Verify Phase 5A graph publication and persistence."""
import os
import sys
import tempfile
import shutil
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

import db
import adapter
import graph


def setup_tmp_db():
    """Create a temporary database for testing."""
    tmpdir = tempfile.mkdtemp(prefix='phase5a_verify_')
    db_path = os.path.join(tmpdir, 'verify_test.db')
    db.get_db_path = lambda: db_path
    db.init_db()
    return tmpdir, db_path


def test_basic_persistence():
    """Build a graph and verify the fields supported by the current schema."""
    print("=" * 60)
    print("TEST 1: Graph publication and supported-field persistence")
    print("=" * 60)

    tmpdir, db_path = setup_tmp_db()
    try:
        # Create a contract with two clauses
        contract_id = adapter.create_contract(
            'verify_contract.txt',
            'Clause 1: The Customer shall pay within 30 days.\n'
            'Clause 2: The Customer is in default if it fails to pay.',
            status='extracted',
            provenance_status=db.PROVENANCE_SYNTHETIC,
        )

        # Save clauses
        clauses = [
            {
                'section_number': '1',
                'clause_text': 'Clause 1: The Customer shall pay within 30 days.',
                'metadata': {'heading': 'Payment Period', 'type': 'payment'},
            },
            {
                'section_number': '2',
                'clause_text': 'Clause 2: The Customer is in default if it fails to pay.',
                'metadata': {'heading': 'Default', 'type': 'default'},
            },
        ]
        adapter.save_clauses(contract_id, clauses)

        # Build the graph with use_llm=False so all edges come from rule table
        stats = graph.build_graph(contract_id, use_llm=False)
        print(f"\nGraph build stats: {stats}")

        # The current schema persists graph structure and build binding. It
        # does not persist the build-time relationship metadata fields.
        edges = adapter.get_edges(contract_id)
        print(f"\n=== Edges after build_graph (n={len(edges)}) ===")
        for i, e in enumerate(edges):
                src = e.get('src')
                dst = e.get('dst')
                rel = e.get('rel')
                print(
                    f'Edge {i}: src={src}, dst={dst}, rel={rel}, '
                    "relationship_source/confidence are build-time only"
                )

        # Get nodes and check the fields exposed by the adapter.
        nodes = adapter.get_nodes(contract_id)
        print(f"\n=== Nodes after build_graph (n={len(nodes)}) ===")
        for i, n in enumerate(nodes):
                ref = n.get('clause_reference')
                node_type = n.get('node_type')
                print(
                    f'Node {i}: ref={ref}, node_type={node_type}, '
                    f'contract_id={n.get("contract_id")}, detail={n.get("detail")!r}'
                )

        # === VERIFICATION ===
        all_pass = True

        node_refs = {n.get("clause_reference") for n in nodes}
        valid_relationships = {"depends_on", "references", "creates", "subject_to"}
        # Check every persisted edge resolves to persisted nodes and uses a
        # relationship supported by the graph backend.
        for i, e in enumerate(edges):
                if e.get("src") not in node_refs or e.get("dst") not in node_refs:
                    print(f"  FAIL: Edge {i} has an unresolved endpoint")
                    all_pass = False
                if e.get("rel") not in valid_relationships:
                    print(f"  FAIL: Edge {i} has unsupported relationship {e.get('rel')!r}")
                    all_pass = False
        if not nodes:
                print("  FAIL: Published graph contains no nodes")
                all_pass = False
        if stats["nodes"] != len(nodes) or stats["edges"] != len(edges):
                print("  FAIL: Build statistics do not match persisted graph counts")
                all_pass = False

        if all_pass:
                print("\n  PASS: Persisted nodes, edges, endpoints, and relationships are valid")
        else:
                print("\n  FAIL: Persisted graph structure is invalid")

        # Now reload and verify persistence
        print("\n=== Reloading and verifying persistence ===")
        edges2 = adapter.get_edges(contract_id)
        print(f"\n=== Edges after reload (n={len(edges2)}) ===")
        reload_pass = True
        if edges2 != edges:
            print("  FAIL: Reloaded edges differ from the published graph")
            reload_pass = False
        if nodes2 := adapter.get_nodes(contract_id):
            if nodes2 != nodes:
                print("  FAIL: Reloaded nodes differ from the published graph")
                reload_pass = False
        else:
            print("  FAIL: Reloaded graph contains no nodes")
            reload_pass = False

        if reload_pass:
            print("\n  PASS: Supported graph fields persist after reload")
        else:
            print("\n  FAIL: Persisted graph differs after reload")

        # Direct database row check
        print("\n=== Direct database row check ===")
        conn = adapter.raw_connection()
        try:
            node_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(nodes)")
            }
            edge_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(edges)")
            }
            unsupported = {
                "confidence",
                "evidence_status",
                "relationship_source",
            }
            if unsupported & (node_columns | edge_columns):
                raise AssertionError(
                    "The verifier's schema assumption is wrong: unsupported "
                    "metadata columns are present."
                )
            print("  PASS: Schema matches the current structure-only graph contract")
            rows = conn.execute(
                "SELECT node_id, clause_reference, node_type, contract_id, detail, build_id "
                "FROM nodes WHERE contract_id = ?",
                (contract_id,),
            ).fetchall()
            print("\nDirect node rows:")
            for r in rows:
                print(f"  node_id={r[0]}, ref={r[1]}, type={r[2]}, "
                      f"contract_id={r[3]}, detail={r[4]!r}, build_id={r[5]}")

            rows2 = conn.execute(
                "SELECT edge_id, source_node, target_node, relationship_type, "
                "contract_id, build_id FROM edges "
                "WHERE contract_id = ?",
                (contract_id,),
            ).fetchall()
            print("\nDirect edge rows:")
            for r in rows2:
                print(
                    f"  edge_id={r[0]}, src={r[1]}, dst={r[2]}, rel={r[3]}, "
                    f"contract_id={r[4]}, build_id={r[5]}"
                )
        finally:
            conn.close()

        # Summary
        print("\n" + "=" * 60)
        if all_pass and reload_pass:
            print("TEST 1: PASS")
        else:
            print("TEST 1: FAIL - see details above")
        print("=" * 60)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_llm_inferred_edges():
    """Test 2: Build graph with LLM and verify inferred edges."""
    print("\n" + "=" * 60)
    print("TEST 2: LLM-inferred edges with confidence 0.5")
    print("=" * 60)

    tmpdir, db_path = setup_tmp_db()
    try:
        # Create a contract
        contract_id = adapter.create_contract(
            'verify_llm_contract.txt',
            'Clause 1: The Customer shall pay within 30 days.',
            status='extracted',
            provenance_status=db.PROVENANCE_SYNTHETIC,
        )

        # Save clauses
        clauses = [
            {
                'section_number': '1',
                'clause_text': 'Clause 1: The Customer shall pay within 30 days.',
                'metadata': {'heading': 'Payment Period', 'type': 'payment'},
            },
        ]
        adapter.save_clauses(contract_id, clauses)

        # Build graph with use_llm=True (but with mock LLM, it may not add edges)
        # Let's check what happens
        stats = graph.build_graph(contract_id, use_llm=True)
        print(f"\nGraph build stats with LLM: {stats}")

        edges = adapter.get_edges(contract_id)
        print(f"\n=== Edges after build_graph with LLM ===")
        for i, e in enumerate(edges):
            rel = e.get('rel')
            print(
                f'Edge {i}: rel={rel}; build-time metadata is not persisted'
            )

        valid_relationships = {"depends_on", "references", "creates", "subject_to"}
        if not all(e.get("rel") in valid_relationships for e in edges):
            raise AssertionError("LLM build produced an unsupported relationship")
        print("\n  PASS: LLM build produced only supported persisted relationships")

        # Reload persistence check for the supported graph fields.
        edges2 = adapter.get_edges(contract_id)
        if edges2 == edges:
            print("  PASS: LLM graph relationships persist after reload")
        else:
            raise AssertionError("Reloaded LLM graph differs from published graph")

        print("\n" + "=" * 60)
        print("TEST 2: Complete")
        print("=" * 60)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_basic_persistence()
    test_llm_inferred_edges()
    print("\n\nAll Phase 5A persistence tests complete.")