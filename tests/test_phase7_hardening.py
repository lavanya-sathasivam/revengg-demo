"""Phase 7 regression tests: contract-analysis correctness and provenance.

Every test creates its own throwaway database and upload directory. The
project's ``demo.db``, ``app.db`` and real uploads are never opened or
modified.

Run:
    python -m unittest discover -s tests -p "test_phase7_hardening.py" -v
    python -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Never let the test suite reach a real provider, whatever .env says.
os.environ["LLM_PROVIDER"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

import adapter  # noqa: E402
import clauses as clause_tools  # noqa: E402
import db  # noqa: E402
import graph  # noqa: E402
import impact  # noqa: E402
from application import create_app  # noqa: E402


def clause(section_number, text, heading, clause_type="other", level=1):
    return {
        "section_number": section_number,
        "clause_text": text,
        "metadata": {
            "heading": heading,
            "type": clause_type,
            "level": level,
            "source": "sub_clause",
        },
    }


class Phase7HardeningTestCase(unittest.TestCase):
    """Base class: a fresh writable database and demo app for every test."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase7_hardening_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, token)
        db.init_db()

        self.application = create_app(
            mode="demo", database_path=self.db_path
        )
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)

    # -- fixtures ---------------------------------------------------------
    def _canonical_clauses(
        self,
        payment_days=30,
        definition_days=None,
        include_interest=True,
    ):
        definition_days = (
            payment_days if definition_days is None else definition_days
        )
        items = [
            clause(
                "2.4",
                '"Payment Period" means the period of %s days after the '
                "invoice date." % definition_days,
                "Payment Period",
                "definition",
            ),
            clause(
                "7.2",
                "7.2 Payment Period. The Customer shall pay each undisputed "
                "Invoice within %s days, subject to Clause 2.4." % payment_days,
                "Payment Period",
                "payment",
            ),
            clause(
                "8.1",
                "8.1 Late Charges. The Customer shall pay late charges on "
                "overdue amounts under Clause 7.2.",
                "Late Charges",
                "fee",
            ),
            clause(
                "9.0",
                '"Service Level" means the 99% availability target.',
                "Service Level",
                "definition",
            ),
            clause(
                "9.1",
                "9.1 Service Levels. The Supplier shall achieve 99% "
                "availability each calendar month, subject to Clause 9.0 and "
                "the service credits in Clause 9.2.",
                "Service Levels",
                "sla",
            ),
            clause(
                "9.2",
                "9.2 Service Credits. Service credits for availability below "
                "99%: 5% of the monthly Charges for each full percentage "
                "point below 99%, capped at 25% of the monthly Charges.",
                "Service Credits",
                "fee",
            ),
            clause(
                "10.1",
                "10.1 Default. The Customer is in default if it fails to pay "
                "under Clause 8.1 or Clause 7.2 and does not cure under "
                "Clause 10.2.",
                "Default",
                "default",
            ),
            clause(
                "10.2",
                "10.2 Cure Period. The Customer has 15 days from notice to "
                "cure that failure.",
                "Cure Period",
                "cure",
            ),
            clause(
                "12.3",
                "12.3 Termination for Non-Payment. The Supplier may terminate "
                "this agreement if the Customer remains in default under "
                "Clause 10.1.",
                "Termination for Non-Payment",
                "termination",
            ),
            clause(
                "14.1",
                "14.1 Notices. Any notice is deemed served 7 days after "
                "despatch.",
                "Notices",
                "notice",
            ),
            clause(
                "SCHEDULE B",
                "SCHEDULE B - CHARGES. The monthly Charges are INR 4,50,000 "
                "per month.",
                "Charges",
                "other",
            ),
        ]
        if include_interest:
            items.insert(
                3,
                clause(
                    "8.2",
                    "8.2 Interest. Interest shall accrue on late amounts at "
                    "1.5% per month from the Payment Due Date under "
                    "Clause 7.2.",
                    "Interest",
                    "interest",
                ),
            )
        return items

    def _make_contract(self, clauses=None, *, build=True, filename="contract.pdf"):
        clauses = list(clauses if clauses is not None else self._canonical_clauses())
        raw_text = "\n\n".join(item["clause_text"] for item in clauses)
        contract_id = adapter.create_contract(filename, raw_text, status="extracted")
        adapter.save_clauses(contract_id, clauses)
        if build:
            graph.build_graph(contract_id, use_llm=False)
        return contract_id

    def _payment_report(self, contract_id, old_value=30, new_value=15):
        return impact.analyze_change(contract_id, "7.2", old_value, new_value)

    def _assert_safe_error(self, response, status_code, error_code):
        self.assertEqual(response.status_code, status_code, response.text)
        payload = response.json()
        self.assertEqual(payload["error_code"], error_code)
        self.assertIsInstance(payload["detail"], str)
        self.assertTrue(payload["detail"])
        return payload


class DurationAndUnitTests(Phase7HardeningTestCase):
    """Workstream A: units are explicit; ambiguity fails closed."""

    def test_within_period_accepts_only_calendar_days_and_simple_words(self):
        self.assertEqual(
            clause_tools.find_within_period("pay within 30 days"),
            {
                "days": 30,
                "unit": "days",
                "supported": True,
                "ambiguous": False,
                "reason": None,
            },
        )
        self.assertEqual(
            clause_tools.find_within_days("pay within thirty days"), 30
        )
        self.assertEqual(
            clause_tools.find_within_days("pay within twenty-five days"), 25
        )

    def test_months_years_and_business_days_are_not_converted(self):
        for text, unit in (
            ("pay within 2 months", "months"),
            ("pay within 1 year", "years"),
            ("pay within 10 business days", "business_days"),
            ("pay within 10 working days", "business_days"),
        ):
            with self.subTest(text=text):
                period = clause_tools.find_within_period(text)
                self.assertIsNotNone(period)
                self.assertEqual(period["unit"], unit)
                self.assertFalse(period["supported"])
                self.assertIsNone(period["days"])
                self.assertIsNone(clause_tools.find_within_days(text))

    def test_competing_within_periods_are_ambiguous(self):
        text = "pay within 30 days or within 15 days, at the Supplier's option"
        period = clause_tools.find_within_period(text)
        self.assertTrue(period["ambiguous"])
        self.assertFalse(period["supported"])
        self.assertIsNone(period["days"])
        self.assertIn("competing", period["reason"])
        self.assertIsNone(clause_tools.find_within_days(text))
        self.assertIsNotNone(clause_tools.within_period_error(text))

    def test_percentages_and_money_are_never_day_counts(self):
        mixed = (
            "The Customer shall pay INR 4,50,000 within 30 days; interest "
            "accrues at 1.5% per annum."
        )
        self.assertEqual(
            impact._days_in({"text": mixed}), [Decimal("30")]
        )
        self.assertEqual(
            impact._days_in({"text": "Interest accrues at 1.5% per month."}),
            [],
        )
        self.assertEqual(
            impact._days_in({"text": "The monthly Charges are INR 4,50,000."}),
            [],
        )
        numbers = clause_tools.find_numbers(mixed)
        self.assertIn({"value": Decimal("30"), "unit": "days"}, numbers)
        self.assertIn({"value": Decimal("1.5"), "unit": "%"}, numbers)
        self.assertNotIn(Decimal("450000"), [n["value"] for n in numbers])
        self.assertEqual(
            clause_tools.find_amounts(mixed), [Decimal("450000")]
        )

    def test_payment_change_in_months_or_years_has_no_implicit_conversion(self):
        cases = (
            ("within 2 months", "unsupported_unit", "months"),
            ("within 1 year", "unsupported_unit", "years"),
            ("within 30 days and within 15 days", "ambiguous_clause_value",
             "competing"),
            ("within 10 business days", "ambiguous_clause_value",
             "business-day"),
        )
        for text, code, fragment in cases:
            with self.subTest(text=text):
                contract_id = self._make_contract(
                    [clause("1", "1. Payment Period. " + text, "Payment Period",
                            "payment")]
                )
                report = impact.analyze_change(contract_id, "1", 2, 1)
                self.assertEqual(report["error_code"], code, report)
                self.assertIn(fragment, report["error"])
                self.assertNotIn("counterfactual", report)

    def test_percentage_only_payment_clause_is_not_read_as_days(self):
        contract_id = self._make_contract(
            [clause(
                "1",
                "1. Payment. The Customer shall pay interest at 2% per month.",
                "Payment",
                "payment",
            )]
        )
        report = impact.analyze_change(contract_id, "1", 2, 1)
        self.assertEqual(report["error_code"], "unsupported_unit")
        self.assertIn("%", report["error"])
        self.assertIn("days", report["error"])

    def test_decimal_formatting_preserves_exact_amounts(self):
        self.assertEqual(impact._fmt_inr(Decimal("450000.50")),
                         "INR 4,50,000.5")
        self.assertEqual(impact._fmt_inr(Decimal("450000.005")),
                         "INR 4,50,000.005")
        self.assertEqual(impact._fmt_inr(Decimal("2.5")), "INR 2.5")
        self.assertEqual(impact._fmt_inr(Decimal("3.5")), "INR 3.5")
        self.assertEqual(impact._fmt_num(Decimal("99.50")), "99.5")
        self.assertEqual(impact._fmt_num(Decimal("30.0")), "30")

    def test_sla_bounds_include_zero_and_one_hundred(self):
        contract_id = self._make_contract(
            [clause(
                "9",
                "9. Service Levels. The Supplier shall achieve 0% availability.",
                "Service Levels",
                "sla",
            )]
        )
        report = impact.analyze_change(contract_id, "9", 0, 100)
        self.assertNotIn("error", report)
        row = report["counterfactual"]["rows"][0]
        self.assertEqual(row["old"], "0%")
        self.assertEqual(row["new"], "100%")

        too_high = impact.analyze_change(contract_id, "9", 0, Decimal("100.01"))
        self.assertEqual(too_high["error_code"], "sla_out_of_range")
        self.assertIn("100", too_high["error"])

        stored_out_of_range = impact.analyze_change(
            contract_id, "9", Decimal("100.01"), 50
        )
        self.assertEqual(stored_out_of_range["error_code"], "sla_out_of_range")


class CounterfactualEvidenceTests(Phase7HardeningTestCase):
    """Workstream C: evidence, statuses, traces, and honest omissions."""

    def test_interest_accrual_row_is_removed_and_explained(self):
        contract_id = self._make_contract()
        report = self._payment_report(contract_id)
        rows = report["counterfactual"]["rows"]
        metrics = [row["metric"] for row in rows]
        self.assertFalse(any("interest" in metric.lower() for metric in metrics),
                         metrics)
        self.assertTrue(any("interest" in note.lower()
                            for note in report["counterfactual"]["notes"]))
        self.assertEqual(report["counterfactual"]["status"],
                         impact.ANALYSIS_STATUS_PARTIAL)

    def test_every_counterfactual_row_has_a_rule_trace_and_assumptions(self):
        contract_id = self._make_contract()
        report = self._payment_report(contract_id)
        counterfactual = report["counterfactual"]
        self.assertEqual(counterfactual["rules_fingerprint"],
                         impact.rules_fingerprint())
        self.assertRegex(counterfactual["rules_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(counterfactual["rule_ids"],
                         sorted(set(counterfactual["rule_ids"])))
        self.assertTrue(counterfactual["rule_ids"])
        for row in counterfactual["rows"]:
            self.assertIn(row["rule_id"], impact._RULE_CATALOG)
            self.assertIsInstance(row["assumptions"], list)
            self.assertTrue(row["assumptions"])
            self.assertEqual(row["calculation"]["rule_id"], row["rule_id"])
            self.assertTrue(row["calculation"]["formula"])
            self.assertIsInstance(row["calculation"]["inputs"], dict)

    def test_evidence_keeps_full_text_and_marks_explicit_excerpts(self):
        long_text = (
            "7.2 Payment Period. The Customer shall pay each undisputed "
            "Invoice within 30 days. "
            + ("Additional commercial context without a number. " * 70)
        )
        contract_id = self._make_contract(
            [clause("7.2", long_text, "Payment Period", "payment")]
        )
        report = self._payment_report(contract_id)
        changed = report["changed_clause"]
        self.assertEqual(changed["text"], long_text)
        self.assertEqual(changed["text_length"], len(long_text))
        self.assertTrue(changed["text_excerpt_truncated"])
        self.assertIn("EXPLICIT EXCERPT", changed["text_excerpt"])

        evidence = next(item for item in report["evidence"]
                        if item["id"] == "7.2")
        self.assertEqual(evidence["text"], long_text)
        self.assertEqual(evidence["text_length"], len(long_text))
        self.assertTrue(evidence["excerpt_truncated"])
        self.assertIn("EXPLICIT EXCERPT", evidence["excerpt"])
        self.assertIn("full clause text", evidence["excerpt"])

        response = self.client.post(
            "/impact",
            json={
                "contract_id": contract_id,
                "clause_id": "7.2",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["changed_clause"]["text"], long_text)
        self.assertTrue(any(item["text"] == long_text
                            for item in payload["evidence"]))

    def test_status_taxonomy_is_explicit_for_supported_and_unmodelled(self):
        supported_contract = self._make_contract([
            clause("1",
                   "1. Payment Period. The Customer shall pay within 30 days.",
                   "Payment Period", "payment"),
            clause("2",
                   "2. Cure Period. The Customer has 15 days from notice to "
                   "cure that failure.",
                   "Cure Period", "cure"),
            clause("3",
                   "3. Notices. Any notice is deemed served 7 days after "
                   "despatch.",
                   "Notices", "notice"),
        ])
        supported = impact.analyze_change(supported_contract, "1", 30, 15)
        self.assertEqual(supported["analysis_status"],
                         impact.ANALYSIS_STATUS_SUPPORTED)
        self.assertEqual(supported["counterfactual"]["status"],
                         impact.ANALYSIS_STATUS_SUPPORTED)

        unmodelled = impact.analyze_change(supported_contract, "3", 7, 5)
        self.assertEqual(unmodelled["analysis_status"],
                         impact.ANALYSIS_STATUS_SUPPORTED)
        self.assertEqual(unmodelled["counterfactual"]["status"],
                         impact.ANALYSIS_STATUS_UNSUPPORTED)
        self.assertEqual(unmodelled["counterfactual"]["rows"], [])
        self.assertTrue(unmodelled["counterfactual"]["notes"])

        for report in (supported, unmodelled):
            for item in report["evidence"]:
                self.assertIn(item["status"], {
                    impact.ANALYSIS_STATUS_SUPPORTED,
                    impact.ANALYSIS_STATUS_PARTIAL,
                    impact.ANALYSIS_STATUS_UNSUPPORTED,
                })

    def test_payment_and_sla_doubts_use_type_specific_wording_and_evidence(self):
        contract_id = self._make_contract()

        payment = self._payment_report(contract_id)
        collision = next(
            doubt for doubt in payment["doubts"]
            if doubt["code"] == "definition_collision"
        )
        self.assertIn("payment period", collision["question"])
        self.assertIn("15 days", collision["question"])
        self.assertEqual(collision["rule_id"], "doubt.definition_collision")
        self.assertEqual(collision["status"],
                         impact.ANALYSIS_STATUS_SUPPORTED)
        self.assertTrue(collision["evidence"])
        self.assertTrue(all(item["status"] == impact.ANALYSIS_STATUS_SUPPORTED
                            for item in collision["evidence"]))

        sla = impact.analyze_change(contract_id, "9.1", 99, 98)
        sla_collision = next(
            doubt for doubt in sla["doubts"]
            if doubt["code"] == "definition_collision"
        )
        self.assertIn("service level", sla_collision["question"])
        self.assertIn("98%", sla_collision["question"])
        self.assertNotIn("payment period", sla_collision["question"])
        self.assertTrue(any(doubt["code"] == "sla_schedule_mismatch"
                            for doubt in sla["doubts"]))

    def test_conflicts_carry_rule_id_status_and_bound_evidence(self):
        contract_id = self._make_contract(
            self._canonical_clauses(definition_days=45)
        )
        conflicts = impact.clash_check(contract_id)
        self.assertTrue(conflicts)
        conflict = next(item for item in conflicts
                        if item["definition_clause"] == "2.4")
        self.assertEqual(conflict["rule_id"], "conflict.definition_drift")
        self.assertEqual(conflict["status"], impact.ANALYSIS_STATUS_SUPPORTED)
        self.assertIn("45 days", conflict["message"])
        self.assertIn("30 days", conflict["message"])
        for evidence in conflict["evidence"]:
            self.assertTrue(evidence["text"])
            self.assertTrue(evidence["clause_sha256"])


class GraphBindingTests(Phase7HardeningTestCase):
    """Workstream B: one authoritative graph, bound to one clause version."""

    def _pointer(self, contract_id):
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT current_graph_build_id FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            return row["current_graph_build_id"] if row else None
        finally:
            conn.close()

    def test_current_binding_exposes_build_hash_and_version(self):
        contract_id = self._make_contract()
        binding = adapter.get_graph_binding(contract_id)
        self.assertEqual(binding["state"], adapter.GRAPH_STATE_CURRENT)
        self.assertTrue(binding["analysis_ready"])
        self.assertFalse(binding["legacy_unbound"])
        self.assertEqual(binding["current_graph_build_id"],
                         self._pointer(contract_id))
        self.assertTrue(binding["build_hash"])
        self.assertEqual(binding["current_version_id"],
                         binding["build_version_id"])

        builds = db.list_graph_builds(contract_id)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["build_id"], binding["current_graph_build_id"])
        self.assertEqual(builds[0]["build_hash"], binding["build_hash"])

        report = self._payment_report(contract_id)
        analysis_binding = report["analysis_binding"]
        self.assertEqual(analysis_binding, report["counterfactual"]["analysis_binding"])
        self.assertEqual(analysis_binding["contract_id"], contract_id)
        self.assertEqual(analysis_binding["clause_id"], "7.2")
        self.assertEqual(analysis_binding["contract_version_id"],
                         binding["current_version_id"])
        self.assertEqual(analysis_binding["graph_build_id"],
                         binding["current_graph_build_id"])
        self.assertEqual(analysis_binding["graph_build_hash"],
                         binding["build_hash"])
        self.assertEqual(analysis_binding["graph_state"],
                         adapter.GRAPH_STATE_CURRENT)
        self.assertEqual(analysis_binding["rules_fingerprint"],
                         report["rules_fingerprint"])
        self.assertEqual(analysis_binding["rules_fingerprint"],
                         impact.rules_fingerprint())
        self.assertEqual(analysis_binding["clause_sha256"],
                         report["changed_clause"]["clause_sha256"])

    def test_saving_clauses_atomically_invalidates_the_graph_pointer(self):
        contract_id = self._make_contract()
        old_binding = adapter.get_graph_binding(contract_id)
        self.assertIsNotNone(old_binding["build_hash"])
        self.assertIsNotNone(self._pointer(contract_id))

        adapter.save_clauses(
            contract_id, self._canonical_clauses(payment_days=15)
        )

        self.assertIsNone(self._pointer(contract_id))
        binding = adapter.get_graph_binding(contract_id)
        self.assertEqual(binding["state"], adapter.GRAPH_STATE_STALE)
        self.assertEqual(binding["reason_code"], "graph_pointer_invalidated")
        self.assertFalse(binding["analysis_ready"])
        self.assertEqual(binding["current_graph_build_id"], None)
        self.assertIsNone(binding["build_hash"])
        self.assertEqual(adapter.get_nodes(contract_id), [])
        self.assertEqual(adapter.get_edges(contract_id), [])

    def test_failed_clause_save_rolls_back_and_keeps_the_graph_current(self):
        contract_id = self._make_contract()
        pointer = self._pointer(contract_id)
        nodes_before = adapter.get_nodes(contract_id)
        clauses_before = [
            row["clause_text"] for row in adapter.list_clauses(contract_id)
        ]

        conn = db.get_conn()
        try:
            conn.execute(
                """
                CREATE TRIGGER phase7_force_clause_failure
                BEFORE INSERT ON clauses
                WHEN NEW.section_number = '7.2'
                BEGIN
                    SELECT RAISE(ABORT, 'forced clause failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        try:
            with self.assertRaises(sqlite3.IntegrityError):
                adapter.save_clauses(
                    contract_id, self._canonical_clauses(payment_days=15)
                )
        finally:
            conn = db.get_conn()
            try:
                conn.execute("DROP TRIGGER IF EXISTS phase7_force_clause_failure")
                conn.commit()
            finally:
                conn.close()

        self.assertEqual(self._pointer(contract_id), pointer)
        self.assertEqual(adapter.get_nodes(contract_id), nodes_before)
        self.assertEqual(
            [row["clause_text"] for row in adapter.list_clauses(contract_id)],
            clauses_before,
        )
        self.assertEqual(adapter.get_graph_binding(contract_id)["state"],
                         adapter.GRAPH_STATE_CURRENT)

    def test_new_contract_version_invalidates_the_graph_without_deleting_it(self):
        contract_id = self._make_contract()
        build_id = self._pointer(contract_id)
        version_id = db.create_contract_version(contract_id)
        self.assertIsNotNone(version_id)
        self.assertIsNone(self._pointer(contract_id))

        binding = adapter.get_graph_binding(contract_id)
        self.assertEqual(binding["state"], adapter.GRAPH_STATE_STALE)
        self.assertEqual(binding["reason_code"], "graph_pointer_invalidated")
        self.assertEqual(binding["current_version_id"], version_id)
        # The old rows remain for audit, but are not readable as current.
        conn = db.get_conn()
        try:
            stored = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE build_id = ?", (build_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(stored, 0)
        self.assertEqual(adapter.get_nodes(contract_id), [])

    def test_stale_graph_returns_http_409_and_does_not_leak_clause_text(self):
        contract_id = self._make_contract()
        adapter.save_clauses(
            contract_id, self._canonical_clauses(payment_days=15)
        )

        payload = self._assert_safe_error(
            self.client.post(
                "/impact",
                json={
                    "contract_id": contract_id,
                    "clause_id": "7.2",
                    "old_value": 30,
                    "new_value": 15,
                },
            ),
            409,
            "graph_stale",
        )
        self.assertIn("stale", payload["detail"])
        self.assertNotIn("Customer shall pay", payload["detail"])
        self.assertNotIn("payment period", payload["detail"].lower())

        graph_payload = self.client.get("/graph/%d" % contract_id).json()
        self.assertFalse(graph_payload["analysis_ready"])
        self.assertEqual(graph_payload["binding"]["state"],
                         adapter.GRAPH_STATE_STALE)
        self.assertEqual(graph_payload["counts"], {"nodes": 0, "edges": 0})

    def test_missing_graph_returns_http_409(self):
        contract_id = self._make_contract(build=False)
        binding = adapter.get_graph_binding(contract_id)
        self.assertEqual(binding["state"], adapter.GRAPH_STATE_MISSING)
        self.assertEqual(binding["reason_code"], "graph_not_built")
        self.assertEqual(adapter.get_nodes(contract_id), [])

        payload = self._assert_safe_error(
            self.client.post(
                "/impact",
                json={
                    "contract_id": contract_id,
                    "clause_id": "7.2",
                    "old_value": 30,
                    "new_value": 15,
                },
            ),
            409,
            "graph_not_built",
        )
        self.assertIn("no published graph", payload["detail"].lower())

    def test_only_current_build_rows_are_readable(self):
        contract_id = self._make_contract()
        build_id = self._pointer(contract_id)
        nodes_before = adapter.get_nodes(contract_id)
        edges_before = adapter.get_edges(contract_id)

        conn = db.get_conn()
        try:
            clause_node = conn.execute(
                """SELECT node_id FROM nodes
                   WHERE contract_id = ? AND clause_reference = '7.2'""",
                (contract_id,),
            ).fetchone()[0]
            stale_node = conn.execute(
                """INSERT INTO nodes
                       (clause_reference, node_type, contract_id, detail,
                        build_id)
                   VALUES ('STALE', 'clause:other', ?, 'old build', ?)""",
                (contract_id, build_id - 1),
            ).lastrowid
            conn.execute(
                """INSERT INTO edges
                       (source_node, target_node, relationship_type,
                        contract_id, build_id)
                   VALUES (?, ?, 'depends_on', ?, ?)""",
                (clause_node, stale_node, contract_id, build_id - 1),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(adapter.get_nodes(contract_id), nodes_before)
        self.assertEqual(adapter.get_edges(contract_id), edges_before)
        self.assertNotIn("STALE",
                         {node["clause_reference"]
                          for node in adapter.get_nodes(contract_id)})
        self.assertNotIn("STALE",
                         {edge["src"] for edge in adapter.get_edges(contract_id)})

    def test_row_count_tampering_makes_the_graph_stale(self):
        contract_id = self._make_contract()
        build_id = self._pointer(contract_id)

        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO nodes
                       (clause_reference, node_type, contract_id, detail,
                        build_id)
                   VALUES ('TAMPERED', 'clause:other', ?, 'tampered', ?)""",
                (contract_id, build_id),
            )
            conn.commit()
        finally:
            conn.close()

        binding = adapter.get_graph_binding(contract_id)
        self.assertEqual(binding["state"], adapter.GRAPH_STATE_STALE)
        self.assertEqual(binding["reason_code"], "graph_row_count_mismatch")
        self.assertEqual(adapter.get_nodes(contract_id), [])
        self.assertEqual(adapter.get_edges(contract_id), [])

    def test_clause_hash_must_match_before_analysis(self):
        contract_id = self._make_contract()
        clauses = self.client.get("/clauses/%d" % contract_id).json()["clauses"]
        payment = next(item for item in clauses if item["clause_id"] == "7.2")
        self.assertEqual(payment["clause_sha256"],
                         hashlib.sha256(payment["text"].encode("utf-8")).hexdigest())
        self.assertEqual(payment["version_id"],
                         db.get_current_version_id(contract_id))

        request = {
            "contract_id": contract_id,
            "clause_id": "7.2",
            "old_value": 30,
            "new_value": 15,
            "expected_clause_sha256": payment["clause_sha256"],
        }
        response = self.client.post("/impact", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["analysis_binding"]["clause_sha256"],
                         payment["clause_sha256"])
        self.assertEqual(payload["analysis_binding"]["expected_clause_sha256"],
                         payment["clause_sha256"])

        request["expected_clause_sha256"] = "0" * 64
        mismatch = self._assert_safe_error(
            self.client.post("/impact", json=request),
            409,
            "clause_hash_mismatch",
        )
        self.assertNotIn(payment["clause_sha256"], mismatch["detail"])
        self.assertNotIn("Customer shall pay", mismatch["detail"])

    def test_legacy_clause_without_a_hash_fails_closed_when_hash_is_expected(self):
        contract_id = self._make_contract()
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE clauses SET clause_sha256 = NULL "
                "WHERE contract_id = ? AND section_number = '7.2'",
                (contract_id,),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.post(
            "/impact",
            json={
                "contract_id": contract_id,
                "clause_id": "7.2",
                "old_value": 30,
                "new_value": 15,
                "expected_clause_sha256": "0" * 64,
            },
        )
        self._assert_safe_error(response, 409, "clause_hash_unavailable")

        # Supplying no expected hash remains backward compatible.
        response = self.client.post(
            "/impact",
            json={
                "contract_id": contract_id,
                "clause_id": "7.2",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_malformed_expected_hash_is_rejected_by_the_schema(self):
        contract_id = self._make_contract()
        response = self.client.post(
            "/impact",
            json={
                "contract_id": contract_id,
                "clause_id": "7.2",
                "old_value": 30,
                "new_value": 15,
                "expected_clause_sha256": "not-a-hash",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)


class LegacyGraphTests(Phase7HardeningTestCase):
    """A read-only pre-binding database is usable, but explicitly partial."""

    def _create_legacy_database(self):
        path = os.path.join(self.tmp_dir, "legacy.db")
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE contracts (
                    contract_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    upload_timestamp TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    raw_text TEXT
                );
                CREATE TABLE clauses (
                    clause_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL,
                    section_number TEXT,
                    clause_text TEXT,
                    metadata TEXT
                );
                CREATE TABLE nodes (
                    node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clause_reference TEXT,
                    node_type TEXT,
                    contract_id INTEGER,
                    detail TEXT
                );
                CREATE TABLE edges (
                    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_node INTEGER NOT NULL,
                    target_node INTEGER NOT NULL,
                    relationship_type TEXT,
                    contract_id INTEGER
                );
                INSERT INTO contracts
                    (contract_id, filename, upload_timestamp,
                     processing_status, raw_text)
                VALUES
                    (1, 'legacy.pdf', '2026-01-01T00:00:00+00:00', 'extracted',
                     'Legacy contract text.');
                INSERT INTO clauses
                    (clause_id, contract_id, section_number, clause_text,
                     metadata)
                VALUES
                    (1, 1, '1',
                     '1. Payment Period. The Customer shall pay within 30 days.',
                     '{"heading":"Payment Period","type":"payment"}'),
                    (2, 1, '2',
                     '2. Cure Period. The Customer has 15 days from notice to cure.',
                     '{"heading":"Cure Period","type":"cure"}');
                INSERT INTO nodes
                    (node_id, clause_reference, node_type, contract_id, detail)
                VALUES
                    (10, '1', 'clause:payment', 1, 'Payment Period'),
                    (11, '1::payment', 'payment', 1, 'pay the invoice'),
                    (12, '2', 'clause:cure', 1, 'Cure Period'),
                    (13, '2::deadline', 'deadline', 1, 'Cure Period of 15 days');
                INSERT INTO edges
                    (edge_id, source_node, target_node, relationship_type,
                     contract_id)
                VALUES
                    (20, 10, 11, 'creates', 1),
                    (21, 12, 13, 'creates', 1);
                """
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_legacy_graph_is_readable_but_marked_partially_supported(self):
        path = self._create_legacy_database()
        token = db.set_database_config(
            db.DatabaseConfig(path, read_only=True)
        )
        self.addCleanup(db.reset_database_config, token)

        binding = adapter.get_graph_binding(1)
        self.assertEqual(binding["state"], adapter.GRAPH_STATE_LEGACY_UNBOUND)
        self.assertTrue(binding["legacy_unbound"])
        self.assertTrue(binding["analysis_ready"])
        self.assertEqual(binding["reason_code"],
                         "legacy_schema_without_graph_pointer")
        self.assertTrue(adapter.get_nodes(1))
        self.assertTrue(adapter.get_edges(1))

        report = impact.analyze_change(1, "1", 30, 15)
        self.assertNotIn("error", report)
        self.assertEqual(report["analysis_status"],
                         impact.ANALYSIS_STATUS_PARTIAL)
        self.assertEqual(report["analysis_binding"]["graph_state"],
                         adapter.GRAPH_STATE_LEGACY_UNBOUND)
        self.assertTrue(all(item["status"] == impact.ANALYSIS_STATUS_PARTIAL
                            for item in report["evidence"]))
        self.assertEqual(report["counterfactual"]["status"],
                         impact.ANALYSIS_STATUS_PARTIAL)
        self.assertIn("graph_state",
                      report["counterfactual"]["analysis_binding"])
        self.assertEqual(report["analysis_binding"]["clause_sha256"], None)
        self.assertEqual(report["analysis_binding"]["graph_build_hash"], None)

        # No read may migrate or rewrite the legacy database.
        conn = db.get_conn()
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            contract_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(contracts)")
            }
            clause_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(clauses)")
            }
        finally:
            conn.close()
        self.assertNotIn("graph_builds", tables)
        self.assertNotIn("contract_versions", tables)
        self.assertNotIn("current_graph_build_id", contract_columns)
        self.assertNotIn("clause_sha256", clause_columns)
        self.assertNotIn("version_id", clause_columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
