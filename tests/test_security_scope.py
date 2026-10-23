"""Phase 3 regression tests: contract and clause data-access boundaries.

Every fixture is synthetic and every database is a throwaway file under a
temporary directory. The project's demo.db, app.db, and uploads directory are
never opened or modified.

Run:
    py -3.11 -m unittest discover -s tests -p "test_security_scope.py" -v
    py -3.11 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# The graph module imports llm.py, which loads .env on import. A test must never
# reach a real provider, whatever the local environment says.
os.environ["LLM_PROVIDER"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

import adapter  # noqa: E402
import db  # noqa: E402
import graph  # noqa: E402
import impact  # noqa: E402
from application import create_app  # noqa: E402


def clause(section_number, text, heading, clause_type, level=1):
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


CONTRACT_A_CLAUSES = [
    clause(
        "1",
        "1. Payment Period. The Customer shall pay each undisputed Invoice "
        "within 30 days. Alpha contract payment wording.",
        "Payment Period",
        "payment",
    ),
    clause(
        "2",
        "2. Default. If the Customer fails to pay under Clause 1, the Customer "
        "is in default.",
        "Default",
        "default",
    ),
    clause(
        "3",
        "3. Cure Period. The Customer has 15 days from notice to cure that "
        "failure.",
        "Cure Period",
        "cure",
    ),
    clause(
        "9",
        "9. Service Levels. The Supplier shall achieve 99% availability.",
        "Service Levels",
        "sla",
    ),
    clause(
        "10",
        "10. Confidentiality. Each Party shall keep confidential information "
        "secret. Alpha confidentiality wording.",
        "Confidentiality",
        "confidentiality",
    ),
]

CONTRACT_B_CLAUSES = [
    clause(
        "1",
        "1. Payment Period. The Customer shall pay each undisputed Invoice "
        "within 45 days. Beta contract payment wording.",
        "Payment Period",
        "payment",
    ),
    clause(
        "2",
        "2. Default. If the Customer fails to pay under Clause 1, the Customer "
        "is in default.",
        "Default",
        "default",
    ),
    clause(
        "9",
        "9. Service Levels. The Supplier shall achieve 99.5% availability.",
        "Service Levels",
        "sla",
    ),
    clause(
        "10",
        "10. Confidentiality. Each Party shall keep confidential information "
        "secret. Beta confidentiality wording.",
        "Confidentiality",
        "confidentiality",
    ),
    clause(
        "B-ONLY",
        "B-ONLY Secret Clause. The beta-only term states 77 days.",
        "Beta Only",
        "other",
    ),
]


class SecurityScopeTestCase(unittest.TestCase):
    """Two synthetic contracts that deliberately share section numbers."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="security_scope_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self._db_token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, self._db_token)
        db.init_db()

        self.contract_a = self._make_contract(
            "alpha.pdf", "Alpha synthetic contract", CONTRACT_A_CLAUSES
        )
        self.contract_b = self._make_contract(
            "beta.pdf", "Beta synthetic contract", CONTRACT_B_CLAUSES
        )

        self.application = create_app(mode="demo", database_path=self.db_path)
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)

    def _make_contract(self, filename, title, clauses):
        raw_text = "%s\n\n%s" % (
            title,
            "\n\n".join(item["clause_text"] for item in clauses),
        )
        contract_id = adapter.create_contract(
            filename, raw_text, status="extracted"
        )
        adapter.save_clauses(contract_id, clauses)
        graph.build_graph(contract_id, use_llm=False)
        return contract_id

    def _impact(self, contract_id, clause_id, old_value, new_value):
        return self.client.post(
            "/impact",
            json={
                "contract_id": contract_id,
                "clause_id": clause_id,
                "old_value": old_value,
                "new_value": new_value,
            },
        )

    def _insert_foreign_dependency(self):
        """Corrupt the graph with an edge from B's node into A's clause.

        The edge row claims contract A, so checking only edges.contract_id
        would admit it. The endpoint node belongs to contract B, which must
        make the row unusable to A.
        """
        conn = db.get_conn()
        try:
            foreign_node = conn.execute(
                """INSERT INTO nodes (clause_reference, node_type,
                                      contract_id, detail)
                   VALUES (?, ?, ?, ?)""",
                ("FOREIGN-SECRET", "clause:other", self.contract_b,
                 "Foreign clause detail that must not leak"),
            ).lastrowid
            target_node = conn.execute(
                """SELECT node_id FROM nodes
                   WHERE contract_id = ? AND clause_reference = '1'
                     AND node_type LIKE 'clause:%'""",
                (self.contract_a,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO edges (source_node, target_node,
                                      relationship_type, contract_id)
                   VALUES (?, ?, ?, ?)""",
                (foreign_node, target_node, "depends_on", self.contract_a),
            )
            conn.commit()
            return foreign_node
        finally:
            conn.close()


class ImpactInputIntegrityTests(SecurityScopeTestCase):
    def test_unknown_contract_returns_404(self):
        response = self._impact(9999, "1", 30, 15)
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["detail"])

    def test_unknown_clause_returns_400_without_contract_text(self):
        response = self._impact(self.contract_a, "99.9", 1, 2)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("not found", detail)
        self.assertNotIn("Alpha contract payment wording", detail)

    def test_old_value_must_match_the_stored_clause(self):
        response = self._impact(self.contract_a, "1", 999, 15)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("does not match the stored value", detail)
        self.assertIn("30 days", detail)

        report = impact.analyze_change(self.contract_a, "1", 999, 15)
        self.assertEqual(report["error_code"], "stale_old_value")
        self.assertNotIn("changed_clause", report)

    def test_old_value_is_read_from_the_requested_contract(self):
        # Clause 1 exists in both contracts with different stored periods.
        # Contract A currently says 30 days, so B's 45 must be rejected.
        mismatch = self._impact(self.contract_a, "1", 45, 14)
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("does not match", mismatch.json()["detail"])

        valid = self._impact(self.contract_a, "1", 30, 14)
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(
            valid.json()["changed_clause"]["sec_no"], "1"
        )
        self.assertEqual(
            valid.json()["counterfactual"]["old_value"], 30
        )

    def test_valid_payment_and_sla_requests_still_work(self):
        payment = self._impact(self.contract_a, "1", 30, 15)
        self.assertEqual(payment.status_code, 200, payment.text)
        payload = payment.json()
        self.assertIn("2", [hit["sec_no"] for hit in payload["direct"]])
        self.assertIn("Alpha contract payment wording",
                      payload["changed_clause"]["text"])

        sla = self._impact(self.contract_a, "9", 99, 98)
        self.assertEqual(sla.status_code, 200, sla.text)
        metrics = [row["metric"] for row in sla.json()["counterfactual"]["rows"]]
        self.assertTrue(any("Service level availability" in m for m in metrics))

    def test_a_clause_with_no_stored_number_keeps_unmodelled_behaviour(self):
        response = self._impact(self.contract_a, "10", 1, 2)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["counterfactual"]["rows"], [])
        self.assertTrue(payload["counterfactual"]["notes"])

    def test_negative_values_are_rejected(self):
        response = self._impact(self.contract_a, "1", 30, -5)
        self.assertEqual(response.status_code, 400)
        self.assertIn("non-negative", response.json()["detail"])

    def test_missing_change_values_are_rejected(self):
        # The HTTP schema rejects omitted fields before the engine is reached.
        omitted = self.client.post(
            "/impact",
            json={
                "contract_id": self.contract_a,
                "clause_id": "1",
                "new_value": 15,
            },
        )
        self.assertEqual(omitted.status_code, 422)

        # The engine also guards callers that bypass the HTTP schema.
        report = impact.analyze_change(self.contract_a, "1", None, 15)
        self.assertEqual(report["error_code"], "missing_change_value")
        self.assertNotIn("changed_clause", report)


class NumericIntegrityTests(SecurityScopeTestCase):
    def test_decimal_payment_values_are_compared_exactly(self):
        report = impact.analyze_change(
            self.contract_a, "1", Decimal("30.0"), Decimal("15.0")
        )
        self.assertNotIn("error", report)
        first = report["counterfactual"]["rows"][0]
        self.assertEqual(first["old"], "30 days")
        self.assertEqual(first["new"], "15 days")

        mismatch = impact.analyze_change(
            self.contract_a, "1",
            Decimal("30.000000000000001"), Decimal("15"),
        )
        self.assertEqual(mismatch["error_code"], "stale_old_value")

    def test_decimal_sla_values_work_over_http_and_directly(self):
        direct = impact.analyze_change(
            self.contract_b, "9", Decimal("99.50"), Decimal("98.25")
        )
        self.assertNotIn("error", direct)
        self.assertEqual(direct["counterfactual"]["rows"][0]["old"], "99.5%")
        self.assertEqual(direct["counterfactual"]["rows"][0]["new"], "98.25%")

        http = self._impact(self.contract_b, "9", 99.5, 98.25)
        self.assertEqual(http.status_code, 200, http.text)
        self.assertEqual(http.json()["counterfactual"]["rows"][0]["old"],
                         "99.5%")

    def test_non_finite_direct_engine_values_never_reach_counterfactual(self):
        invalid = (
            float("nan"), float("inf"), float("-inf"),
            Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
            Decimal("sNaN"),
        )
        for value in invalid:
            with self.subTest(value=repr(value)):
                report = impact.analyze_change(
                    self.contract_a, "1", value, 15
                )
                self.assertEqual(report["error_code"],
                                 "invalid_change_value")
                self.assertNotIn("changed_clause", report)
                self.assertNotIn("counterfactual", report)

        report = impact.analyze_change(
            self.contract_a, "1", 30, float("nan")
        )
        self.assertEqual(report["error_code"], "invalid_change_value")

        with self.assertRaises(ValueError):
            impact.counterfactual(
                self.contract_a, "1", float("nan"), 15
            )

    def test_invalid_numeric_types_are_rejected_by_engine(self):
        for value in ("30", True, [30], {"value": 30}):
            with self.subTest(value=repr(value)):
                report = impact.analyze_change(
                    self.contract_a, "1", value, 15
                )
                self.assertEqual(report["error_code"],
                                 "invalid_change_value")
                self.assertNotIn("counterfactual", report)

        missing = impact.analyze_change(self.contract_a, "1", None, 15)
        self.assertEqual(missing["error_code"], "missing_change_value")

    def test_http_rejects_non_finite_and_invalid_numeric_values(self):
        # JSON itself cannot represent NaN/infinity, but Python's json module
        # can emit those tokens. The request must be rejected without tripping
        # over serialisation of the original non-finite value.
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                response = self.client.post(
                    "/impact",
                    content=(
                        '{"contract_id": %d, "clause_id": "1", '
                        '"old_value": %s, "new_value": 15}'
                        % (self.contract_a, token)
                    ),
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(response.status_code, 422, response.text)

        for value in ("NaN", "Infinity", "-Infinity",
                      "not-a-number", None, True):
            with self.subTest(value=repr(value)):
                response = self._impact(self.contract_a, "1", value, 15)
                self.assertEqual(response.status_code, 422, response.text)


class UnitConventionTests(SecurityScopeTestCase):
    def test_payment_validation_uses_only_the_operative_day_value(self):
        contract_id = self._make_contract(
            "mixed-payment.pdf", "Mixed payment contract",
            [clause(
                "1",
                "1. Payment Period. The Customer shall pay each undisputed "
                "Invoice within 30 days. Late amounts accrue interest of "
                "2% per month.",
                "Payment Period",
                "payment",
            )],
        )
        valid = impact.analyze_change(contract_id, "1", 30, 15)
        self.assertNotIn("error", valid)

        wrong_unit = impact.analyze_change(contract_id, "1", 2, 1)
        self.assertEqual(wrong_unit["error_code"], "stale_old_value")
        self.assertIn("30 days", wrong_unit["error"])

    def test_payment_period_in_months_is_rejected_without_conversion(self):
        contract_id = self._make_contract(
            "monthly-payment.pdf", "Monthly payment contract",
            [clause(
                "1",
                "1. Payment Period. The Customer shall pay each undisputed "
                "Invoice within 2 months.",
                "Payment Period",
                "payment",
            )],
        )
        for old_value in (2, 60):
            with self.subTest(old_value=old_value):
                report = impact.analyze_change(
                    contract_id, "1", old_value, 1
                )
                self.assertEqual(report["error_code"], "unsupported_unit")
                self.assertIn("months", report["error"])
                self.assertIn("days", report["error"])

        response = self._impact(contract_id, "1", 2, 1)
        self.assertEqual(response.status_code, 400)
        self.assertIn("No unit conversion", response.json()["detail"])

    def test_sla_validation_uses_only_the_stored_percentage(self):
        contract_id = self._make_contract(
            "mixed-sla.pdf", "Mixed SLA contract",
            [clause(
                "9",
                "9. Service Levels. The Supplier shall achieve 99.5% "
                "availability. A response is due within 30 days.",
                "Service Levels",
                "sla",
            )],
        )
        valid = impact.analyze_change(
            contract_id, "9", Decimal("99.50"), 98
        )
        self.assertNotIn("error", valid)

        wrong_unit = impact.analyze_change(contract_id, "9", 30, 20)
        self.assertEqual(wrong_unit["error_code"], "stale_old_value")
        self.assertIn("99.5%", wrong_unit["error"])

    def test_sla_without_a_percentage_is_rejected_not_reinterpreted(self):
        contract_id = self._make_contract(
            "day-based-sla.pdf", "Day based SLA contract",
            [clause(
                "9",
                "9. Service Levels. A response is due within 30 days.",
                "Service Levels",
                "sla",
            )],
        )
        report = impact.analyze_change(contract_id, "9", 30, 20)
        self.assertEqual(report["error_code"], "unsupported_unit")
        self.assertIn("days", report["error"])
        self.assertIn("%", report["error"])

    def test_unmodelled_type_keeps_number_only_ambiguity(self):
        report = impact.analyze_change(
            self.contract_a, "10", Decimal("1.5"), Decimal("2.5")
        )
        self.assertNotIn("error", report)
        self.assertEqual(report["counterfactual"]["rows"], [])


class CrossContractAccessTests(SecurityScopeTestCase):
    def test_clause_list_is_scoped_to_the_requested_contract(self):
        alpha = self.client.get("/clauses/%d" % self.contract_a)
        beta = self.client.get("/clauses/%d" % self.contract_b)
        self.assertEqual(alpha.status_code, 200)
        self.assertEqual(beta.status_code, 200)

        alpha_text = json.dumps(alpha.json())
        beta_text = json.dumps(beta.json())
        self.assertIn("Alpha contract payment wording", alpha_text)
        self.assertNotIn("Beta contract payment wording", alpha_text)
        self.assertIn("Beta contract payment wording", beta_text)
        self.assertNotIn("Alpha contract payment wording", beta_text)

        alpha_sections = {c["clause_id"] for c in alpha.json()["clauses"]}
        self.assertNotIn("B-ONLY", alpha_sections)

    def test_stored_clause_routes_are_scoped_to_the_requested_contract(self):
        alpha = self.client.get(
            "/contracts/%d/clauses" % self.contract_a
        ).json()
        beta = self.client.get(
            "/contracts/%d/clauses" % self.contract_b
        ).json()

        alpha_text = json.dumps(alpha)
        beta_text = json.dumps(beta)
        self.assertIn("Alpha contract payment wording", alpha_text)
        self.assertNotIn("Beta contract payment wording", alpha_text)
        self.assertIn("Beta contract payment wording", beta_text)
        self.assertNotIn("Alpha contract payment wording", beta_text)

    def test_foreign_only_clause_cannot_be_analyzed_or_explained(self):
        analyzed = self._impact(self.contract_a, "B-ONLY", 77, 14)
        self.assertEqual(analyzed.status_code, 400)
        self.assertIn("not found", analyzed.json()["detail"])
        self.assertNotIn("Secret Clause", analyzed.json()["detail"])

        explained = self.client.get(
            "/contracts/%d/clauses/B-ONLY/explanation" % self.contract_a
        )
        self.assertEqual(explained.status_code, 404)

    def test_contract_detail_stays_within_the_requested_contract(self):
        alpha = self.client.get("/contracts/%d" % self.contract_a)
        beta = self.client.get("/contracts/%d" % self.contract_b)
        self.assertEqual(alpha.status_code, 200)
        self.assertEqual(beta.status_code, 200)
        self.assertIn("Alpha contract payment wording", alpha.json()["raw_text"])
        self.assertNotIn("Beta contract payment wording", alpha.json()["raw_text"])
        self.assertIn("Beta contract payment wording", beta.json()["raw_text"])
        self.assertNotIn("Alpha contract payment wording", beta.json()["raw_text"])

    def test_contract_list_does_not_expose_raw_text(self):
        listing = self.client.get("/contracts").json()
        self.assertEqual(len(listing), 2)
        for row in listing:
            self.assertNotIn("raw_text", row)


class GraphAndEvidenceScopeTests(SecurityScopeTestCase):
    def test_foreign_endpoint_edge_is_not_returned_by_adapter(self):
        foreign_node = self._insert_foreign_dependency()

        conn = db.get_conn()
        try:
            stored = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE contract_id = ?",
                (self.contract_a,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(stored, 1)

        edges = adapter.get_edges(self.contract_a)
        self.assertNotIn(
            "FOREIGN-SECRET", {edge["src"] for edge in edges}
        )
        self.assertTrue(all(edge["src"] != "FOREIGN-SECRET" for edge in edges))
        self.assertIsInstance(foreign_node, int)

    def test_graph_references_share_stored_section_canonicalization(self):
        # Dotted sub-clause numbers and element refs ('7.2::payment') must
        # canonicalise back to the exact stored section keys. This synthetic
        # check found no mismatch, so production code was not changed.
        contract_id = self._make_contract(
            "canonical.pdf", "Canonicalisation contract",
            [
                clause(
                    "7.2", "7.2 Payment Period. Pay within 30 days.",
                    "Payment Period", "payment",
                ),
                clause(
                    "7.3",
                    "7.3 Late Payment. If payment under Clause 7.2 is late, "
                    "Clause 7.2 applies.",
                    "Late Payment", "other",
                ),
            ],
        )
        sections = {
            row["section_number"]
            for row in adapter.list_clauses(contract_id)
        }
        self.assertEqual(sections, {"7.2", "7.3"})
        edges = adapter.get_edges(contract_id)
        self.assertIn(
            ("7.3", "7.2", "references"),
            {(e["src"], e["dst"], e["rel"]) for e in edges},
        )
        for edge in edges:
            for reference in (edge["src"], edge["dst"]):
                self.assertIn(
                    impact._reference_section(reference), sections
                )

    def test_graph_endpoint_does_not_expose_cross_contract_nodes_or_edges(self):
        self._insert_foreign_dependency()
        response = self.client.get("/graph/%d" % self.contract_a)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["nodes"])
        self.assertTrue(all(node["contract_id"] == self.contract_a
                            for node in payload["nodes"]))
        blob = json.dumps(payload)
        self.assertNotIn("FOREIGN-SECRET", blob)
        self.assertNotIn("Foreign clause detail", blob)

    def test_impact_traversal_and_evidence_stay_in_contract_scope(self):
        self._insert_foreign_dependency()
        response = self._impact(self.contract_a, "1", 30, 15)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()

        stored_sections = {
            row["section_number"]
            for row in adapter.list_clauses(self.contract_a)
        }

        for hit in report["direct"] + report["indirect"]:
            self.assertIn(hit["sec_no"], stored_sections)
        for evidence in report["evidence"]:
            self.assertIn(evidence["id"], stored_sections)
        for bucket in ("obligations", "conditions", "consequences", "deadlines",
                       "rights", "definitions"):
            for item in report[bucket]:
                self.assertIn(item["clause"], stored_sections)

        blob = json.dumps(report)
        self.assertNotIn("FOREIGN-SECRET", blob)
        self.assertNotIn("Foreign clause detail", blob)


class UnknownGraphAndComparisonTests(SecurityScopeTestCase):
    def test_unknown_contract_ids_return_404_on_read_endpoints(self):
        for path in (
            "/graph/9999",
            "/clauses/9999",
            "/demo/9999/suggest",
            "/compare/9999/%d" % self.contract_a,
            "/compare/%d/9999" % self.contract_a,
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404, response.text)

    def test_direct_engine_unknown_contract_is_an_error_not_a_report(self):
        report = impact.analyze_change(9999, "1", 30, 15)
        self.assertEqual(report["error_code"], "unknown_contract")
        self.assertEqual(report["http_status"], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
