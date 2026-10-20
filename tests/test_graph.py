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

# The test suite must never call a real API, whatever .env says. This has to
# be set BEFORE llm.py is imported, because llm.py loads .env on import.
os.environ["LLM_PROVIDER"] = "mock"

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
    def test_review_points_rank_by_severity_impact_and_evidence(self):
        doubts = [
            {"code": "low_direct", "severity": "low",
             "review_point": "Low direct", "evidence": [{"id": "8.1"}]},
            {"code": "high_indirect", "severity": "high",
             "review_point": "High indirect", "evidence": [{"id": "10.1"}]},
            {"code": "high_direct_broad", "severity": "high",
             "review_point": "High direct broad",
             "evidence": [{"id": "8.1"}, {"id": "8.2"}]},
            {"code": "high_direct_narrow", "severity": "high",
             "review_point": "High direct narrow", "evidence": [{"id": "8.1"}]},
        ]
        impact_hits = [
            {"sec_no": "8.1", "hops": 1},
            {"sec_no": "8.2", "hops": 1},
            {"sec_no": "10.1", "hops": 2},
        ]

        ranked = impact._rank_review_points(doubts, impact_hits)

        self.assertEqual(
            [item["code"] for item in ranked],
            ["high_direct_broad", "high_direct_narrow", "high_indirect", "low_direct"],
        )
        self.assertEqual([item["rank"] for item in ranked], [1, 2, 3, 4])
        self.assertEqual(ranked[0]["impact_hops"], 1)
        self.assertEqual(ranked[0]["evidence_clause_count"], 2)
        self.assertIn("High severity", ranked[0]["priority_reason"])
        self.assertEqual(ranked[0]["evidence"], doubts[2]["evidence"])

    def test_analysis_keeps_legacy_review_points_in_ranked_order(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        self.assertEqual(
            report["review_points"],
            [item["review_point"] for item in report["ranked_review_points"]],
        )
        self.assertEqual(
            [item["rank"] for item in report["ranked_review_points"]],
            list(range(1, len(report["ranked_review_points"]) + 1)),
        )
        doubt_by_code = {doubt["code"]: doubt for doubt in report["doubts"]}
        for item in report["ranked_review_points"]:
            self.assertEqual(item["severity"], doubt_by_code[item["code"]]["severity"])
            self.assertEqual(item["evidence"], doubt_by_code[item["code"]]["evidence"])

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

    def test_report_surfaces_legal_meaning_not_just_clause_numbers(self):
        """MVP spec items 3, 4, 5: related obligations, conditions affected,
        consequences. 'Clause 8.1 is affected' is useless; 'the obligation to
        pay late charges is affected' is what a lawyer needs."""
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)

        for key in ("obligations", "conditions", "consequences", "deadlines"):
            self.assertIn(key, report)
            self.assertIsInstance(report[key], list)

        obligation_clauses = {o["clause"] for o in report["obligations"]}
        self.assertIn("8.1", obligation_clauses)
        self.assertIn("8.2", obligation_clauses)
        self.assertTrue(any("late charge" in o["detail"].lower()
                            for o in report["obligations"]))

        condition_clauses = {c["clause"] for c in report["conditions"]}
        self.assertIn("10.1", condition_clauses)

        consequence_clauses = {c["clause"] for c in report["consequences"]}
        self.assertIn("12.3", consequence_clauses)

        deadline_clauses = {d["clause"] for d in report["deadlines"]}
        self.assertIn("10.2", deadline_clauses)

        # every semantic item must point back at a clause
        for bucket in ("obligations", "conditions", "consequences", "deadlines"):
            for item in report[bucket]:
                self.assertTrue(item["clause"])
                self.assertTrue(item["detail"])

    def test_changed_clause_effects_are_separate(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        own = report["changed_clause_effects"]
        self.assertTrue(own["obligations"])
        self.assertEqual({o["clause"] for o in own["obligations"]}, {"7.2"})

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


class TestApiSurface(GraphTestCase):
    """The HTTP surface Person C will build the screen against."""

    def test_router_exposes_the_endpoints(self):
        import api_b
        paths = {r.path for r in api_b.router.routes}
        for expected in ("/graph/{contract_id}", "/clauses/{contract_id}",
                         "/impact", "/compare/{contract_a}/{contract_b}",
                         "/demo/{contract_id}/suggest"):
            self.assertIn(expected, paths)

    def test_impact_endpoint_returns_all_twelve_mvp_items(self):
        import api_b
        report = api_b.analyze(api_b.ImpactRequest(
            contract_id=CONTRACT_A, clause_id="7.2",
            old_value=30, new_value=15))
        for key in ("changed_clause", "direct", "indirect", "obligations",
                    "conditions", "consequences", "counterfactual", "doubts",
                    "conflicts", "evidence", "review_points", "summary"):
            self.assertIn(key, report, "MVP spec item %s missing" % key)

    def test_impact_endpoint_rejects_unknown_clause(self):
        import api_b
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            api_b.analyze(api_b.ImpactRequest(
                contract_id=CONTRACT_A, clause_id="99.9",
                old_value=1, new_value=2))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_impact_endpoint_rejects_unknown_contract(self):
        import api_b
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            api_b.analyze(api_b.ImpactRequest(
                contract_id=9999, clause_id="7.2", old_value=30, new_value=15))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_suggest_endpoint_finds_the_payment_clause(self):
        import api_b
        result = api_b.suggest(CONTRACT_A)
        self.assertEqual(result["clause_id"], "7.2")
        self.assertEqual(result["old_value"], 30)
        self.assertEqual(result["new_value"], 15)
        self.assertIn("request", result)

    def test_graph_endpoint_returns_nodes_and_edges(self):
        import api_b
        payload = api_b.get_graph(CONTRACT_A)
        self.assertGreater(payload["counts"]["nodes"], 0)
        self.assertGreater(payload["counts"]["edges"], 0)
        self.assertIn("src", payload["edges"][0])
        self.assertIn("rel", payload["edges"][0])

    def test_compare_endpoint(self):
        import api_b
        payload = api_b.compare(CONTRACT_A, CONTRACT_B)
        self.assertTrue(payload["mismatches"])


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

    def test_graph_helpers_work_before_init_db(self):
        """Reading the graph on a database that has no tables must not raise
        'no such table' - a demo must never die on a setup mistake."""
        import shutil as _shutil
        empty_dir = tempfile.mkdtemp(prefix="revengg_empty_")
        empty_db = os.path.join(empty_dir, "empty.db")
        orig = db.get_db_path
        db.get_db_path = lambda: empty_db
        try:
            adapter.ensure_graph_schema()
            self.assertEqual(adapter.get_nodes(1), [])
            self.assertEqual(adapter.get_edges(1), [])
        finally:
            db.get_db_path = orig
            _shutil.rmtree(empty_dir, ignore_errors=True)

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
