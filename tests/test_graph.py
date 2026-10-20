"""Tests for the relationship model and the impact analysis.

Owner: Person B (branch b-graph).

Run:
    py -3.11 -m unittest discover -s tests -v

These tests are our evidence for the report. The database is redirected to a
temporary path (same pattern as Person A's test_api.py), so the demo
database is never touched.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db          # noqa: E402  (Person A's module - only for redirecting the db path)
import adapter     # noqa: E402
import clauses     # noqa: E402
import graph       # noqa: E402
import impact      # noqa: E402

with open(os.path.join(ROOT, "data", "ground_truth.json"), "r", encoding="utf-8") as fh:
    GT = json.load(fh)

CONTRACT_A = 1
CONTRACT_B = 2


class GraphTestCase(unittest.TestCase):
    """Base class: every test runs against a throwaway database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.mkdtemp(prefix="revengg_test_")
        cls.db_path = os.path.join(cls.tmp_dir, "test.db")
        cls._orig_get_db_path = db.get_db_path
        db.get_db_path = lambda: cls.db_path
        db.init_db()

        import seed
        seed.seed(db_path=cls.db_path)
    @classmethod
    def tearDownClass(cls):
        db.get_db_path = cls._orig_get_db_path
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)


class TestSegmentation(GraphTestCase):
    def test_clause_count(self):
        rows = adapter.list_clauses(CONTRACT_A)
        self.assertEqual(len(rows), GT["contract_a"]["expected_clause_count"])

    def test_known_clause_numbers_exist(self):
        secs = {r["section_number"] for r in adapter.list_clauses(CONTRACT_A)}
        for sec in ["2.4", "7.2", "8.1", "8.2", "9.1", "10.1", "10.2", "12.3", "14.1"]:
            self.assertIn(sec, secs)

    def test_clause_text_is_not_empty(self):
        for r in adapter.list_clauses(CONTRACT_A):
            self.assertTrue((r["clause_text"] or "").strip(),
                            "empty clause %s" % r["section_number"])

    def test_headings_extracted(self):
        by_sec = {r["section_number"]: r for r in adapter.list_clauses(CONTRACT_A)}
        self.assertEqual(adapter.clause_metadata(by_sec["7.2"])["heading"],
                         "Payment Period")
        self.assertEqual(adapter.clause_metadata(by_sec["10.2"])["heading"],
                         "Cure Period")
        self.assertEqual(adapter.clause_metadata(by_sec["12.3"])["heading"],
                         "Termination for Non-Payment")

    def test_subclauses_are_found(self):
        """A's splitter finds top-level sections; we add the sub-clause layer."""
        secs = {r["section_number"] for r in adapter.list_clauses(CONTRACT_A)}
        self.assertIn("7", secs)          # top-level section
        self.assertIn("7.2", secs)        # sub-clause


class TestClassification(GraphTestCase):
    def test_clause_types(self):
        by_sec = {r["section_number"]: r for r in adapter.list_clauses(CONTRACT_A)}
        for sec, expected in GT["contract_a"]["expected_types"].items():
            actual = adapter.clause_metadata(by_sec[sec])["type"]
            self.assertEqual(actual, expected, "wrong type for %s" % sec)

    def test_numbers_extracted(self):
        by_sec = {r["section_number"]: r for r in adapter.list_clauses(CONTRACT_A)}
        self.assertIn({"value": 30, "unit": "days"},
                      clauses.find_numbers(by_sec["7.2"]["clause_text"]))
        self.assertIn({"value": 15, "unit": "days"},
                      clauses.find_numbers(by_sec["10.2"]["clause_text"]))
        self.assertIn({"value": 99, "unit": "%"},
                      clauses.find_numbers(by_sec["9.1"]["clause_text"]))


class TestRelationshipModel(GraphTestCase):
    def test_dependency_edges(self):
        edges = {(e["src"], e["dst"], e["rel"]) for e in adapter.get_edges(CONTRACT_A)}
        for src, dst in GT["contract_a"]["expected_dependencies"]:
            self.assertIn((src, dst, "depends_on"), edges,
                          "missing dependency %s -> %s" % (src, dst))

    def test_reference_edges(self):
        edges = {(e["src"], e["dst"], e["rel"]) for e in adapter.get_edges(CONTRACT_A)}
        for src, dst in GT["contract_a"]["expected_references"]:
            self.assertIn((src, dst, "references"), edges,
                          "missing reference %s -> %s" % (src, dst))

    def test_definition_clause_is_not_a_hard_dependency(self):
        """Clause 2.4 defines a term, so it is a DOUBT, not an impact."""
        edges = {(e["src"], e["dst"], e["rel"]) for e in adapter.get_edges(CONTRACT_A)}
        self.assertNotIn(("7.2", "2.4", "depends_on"), edges)

    def test_nodes_exist(self):
        kinds = {n["node_type"] for n in adapter.get_nodes(CONTRACT_A)}
        for kind in ["payment", "deadline", "condition", "termination", "obligation"]:
            self.assertIn(kind, kinds)

    def test_nodes_are_scoped_to_their_contract(self):
        """Two contracts can both have a clause '7.2' - they must not collide."""
        a_refs = {n["clause_reference"] for n in adapter.get_nodes(CONTRACT_A)}
        b_refs = {n["clause_reference"] for n in adapter.get_nodes(CONTRACT_B)}
        self.assertTrue(a_refs & b_refs, "test is weak: contracts share no clause numbers")
        for n in adapter.get_nodes(CONTRACT_B):
            self.assertEqual(n["contract_id"], CONTRACT_B)

    def test_edges_do_not_leak_between_contracts(self):
        """Both contracts use similar clause numbers, so edge reference pairs
        CAN legitimately overlap. What must not leak is the ROW: every edge
        returned for a contract must connect nodes that belong to that same
        contract."""
        for cid in (CONTRACT_A, CONTRACT_B):
            conn = adapter.raw_connection()
            try:
                own_node_ids = {r[0] for r in conn.execute(
                    "SELECT node_id FROM nodes WHERE contract_id = ?", (cid,))}
                rows = conn.execute(
                    """SELECT e.source_node, e.target_node, e.contract_id
                       FROM edges e JOIN nodes s ON s.node_id = e.source_node
                       JOIN nodes t ON t.node_id = e.target_node"""
                ).fetchall()
            finally:
                conn.close()
            for src, dst, edge_cid in rows:
                if edge_cid != cid:
                    continue
                self.assertIn(src, own_node_ids,
                              "edge of contract %d points at a foreign node" % cid)
                self.assertIn(dst, own_node_ids,
                              "edge of contract %d points at a foreign node" % cid)

    def test_same_clause_number_in_two_contracts_is_two_nodes(self):
        """Contract A and B both have a clause '1.1' - they must be separate
        rows, otherwise one contract's graph would corrupt the other."""
        conn = adapter.raw_connection()
        try:
            rows = conn.execute(
                """SELECT n.contract_id, COUNT(*) FROM nodes n
                   WHERE n.clause_reference = '1.1' GROUP BY n.contract_id"""
            ).fetchall()
        finally:
            conn.close()
        contract_ids = [r[0] for r in rows]
        self.assertEqual(len(contract_ids), 2,
                         "clause 1.1 is shared between contracts: %s" % contract_ids)


class TestImpact(GraphTestCase):
    def test_direct_and_indirect(self):
        imp = impact.impact_of(CONTRACT_A, "7.2")
        expected = GT["contract_a"]["expected_impact"]["7.2"]
        self.assertEqual([h["sec_no"] for h in imp["direct"]], expected["direct"])
        self.assertEqual([h["sec_no"] for h in imp["indirect"]], expected["indirect"])

    def test_chain(self):
        imp = impact.impact_of(CONTRACT_A, "7.2")
        self.assertEqual(imp["chain"],
                         GT["contract_a"]["expected_impact"]["7.2"]["chain"])

    def test_counterfactual_dates(self):
        exp = GT["contract_a"]["expected_counterfactual"]["7.2"]
        cf = impact.counterfactual(CONTRACT_A, "7.2", exp["old"], exp["new"])
        rows = {r["metric"]: r for r in cf["rows"]}
        default_row = rows["Default can be declared on (payment + cure)"]
        self.assertEqual(default_row["old"], exp["default_trigger_old"])
        self.assertEqual(default_row["new"], exp["default_trigger_new"])

        cure_row = [r for r in cf["rows"] if r["metric"].startswith("Cure period")][0]
        self.assertFalse(cure_row["changed"])
        notice_row = [r for r in cf["rows"] if r["metric"].startswith("Notice")][0]
        self.assertFalse(notice_row["changed"])

    def test_doubts_detected(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        codes = {d["code"] for d in report["doubts"]}
        for code in GT["contract_a"]["expected_doubt_codes"]:
            self.assertIn(code, codes)

    def test_every_doubt_has_evidence(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        self.assertTrue(report["doubts"])
        for doubt in report["doubts"]:
            self.assertTrue(doubt["evidence"], "doubt without evidence")
            for ev in doubt["evidence"]:
                self.assertTrue(ev["id"])

    def test_summary_reports_counts(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        self.assertIn("3 clause(s) directly", report["summary"])

    def test_impact_does_not_cross_contracts(self):
        """Changing 7.2 in Contract A must not reach into Contract B."""
        imp = impact.impact_of(CONTRACT_A, "7.2")
        for hit in imp["all"]:
            self.assertNotIn(hit["sec_no"], {"5.2", "6.1"})


class TestConflictAndCrossContract(GraphTestCase):
    def test_definition_drift_in_contract_b(self):
        clashes = impact.clash_check(CONTRACT_B)
        self.assertTrue(clashes, "expected a definition drift in Contract B")
        drift = clashes[0]
        exp = GT["contract_b"]["expected_definition_drift"]
        self.assertEqual(drift["definition_clause"], exp["definition_clause"])
        self.assertEqual(drift["using_clause"], exp["using_clause"])

    def test_no_false_conflict_in_contract_a(self):
        self.assertEqual(impact.clash_check(CONTRACT_A), [])

    def test_cross_contract_mismatch(self):
        result = impact.compare_contracts(CONTRACT_A, CONTRACT_B)
        metrics = {m["metric"] for m in result["mismatches"]}
        for metric in GT["cross_contract"]["expected_mismatch_metrics"]:
            self.assertIn(metric, metrics)
        self.assertEqual(result["contract_a"]["payment_days"],
                         GT["cross_contract"]["expected_payment_days"]["A"])
        self.assertEqual(result["contract_b"]["payment_days"],
                         GT["cross_contract"]["expected_payment_days"]["B"])


class TestRobustness(GraphTestCase):
    def test_unknown_clause_returns_error_not_crash(self):
        report = impact.analyze_change(CONTRACT_A, "99.9", 1, 2)
        self.assertIn("error", report)

    def test_garbage_text_does_not_crash_segmentation(self):
        blocks = clauses.split_clauses("!!! ??? ###\nrandom text without numbers\n")
        self.assertIsInstance(blocks, list)

    def test_empty_contract_builds_empty_graph(self):
        cid = adapter.create_contract("empty.txt", "", status="extracted")
        stats = graph.build_graph(cid, use_llm=False)
        self.assertEqual(stats["clauses"], 0)

    def test_llm_failure_does_not_break_graph(self):
        """llm.ask_json must never raise, even with no API key configured."""
        import llm
        self.assertIsInstance(llm.ask_json("give me json"), dict)

    def test_person_a_tests_still_pass(self):
        """Branch B must not break branch A.

        Run in a subprocess with a clean environment so A's tests use A's
        database path and their real-document fixtures, not our temp file.
        """
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "unittest",
             "test_api", "test_db_clauses", "test_split_clauses"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         "Person A's tests broke:\n%s" % result.stderr[-3000:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
