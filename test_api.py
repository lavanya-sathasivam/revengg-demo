"""Focused API tests for the clause-integrated upload/retrieval flow.

Notes on the test harness:
- httpx is not installed, so fastapi.testclient (Starlette TestClient) cannot
  be used and we must not add dependencies. Instead the route handler
  functions are called directly with real Starlette Request / UploadFile
  objects. This exercises all route logic (validation, extraction, split,
  persistence, error handling) without a live server.
- The database and the uploads directory are redirected to temporary paths,
  so the project's real app.db and uploads/ are never touched.

Run:  python -m unittest test_api -v
"""

import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import pymupdf
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.requests import Request

import app
import adapter
import api_a
import api_b
import auth
import db
import graph
from application import create_app


def make_request(content_length=None, application=None):
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": headers}
    if application is not None:
        scope["app"] = application
    return Request(scope)


def make_upload(data: bytes, filename: str) -> UploadFile:
    return UploadFile(file=BytesIO(data), filename=filename)


def make_pdf_bytes(lines):
    """Build a real one-page PDF containing the given text lines."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line)
        y += 18
    data = doc.tobytes()
    doc.close()
    return data


SECTIONS_PDF = make_pdf_bytes(
    [
        "SERVICES AGREEMENT",
        "1. SERVICES. Consultant shall provide services.",
        "2. PAYMENTS. Company shall pay within thirty days.",
        "3.",
        "TERM. This Agreement lasts one year.",
        "4. TERMINATION. Either party may terminate.",
    ]
)

NO_SECTIONS_PDF = make_pdf_bytes(
    ["THIS DOCUMENT HAS NO NUMBERED SECTIONS AT ALL"]
)

HIERARCHICAL_PDF = make_pdf_bytes(
    [
        "1. FIRST. First section.",
        "2. SECOND. Second section.",
        "3. THIRD. Third section.",
        "4. FOURTH. Fourth section.",
        "5. FIFTH. Fifth section.",
        "6. SIXTH. Sixth section.",
        "7. PAYMENT. Parent text before the first nested clause.",
        "7.2 Payment Period. Pay within thirty days.",
        "7.3 Late Fee. Pay interest on overdue sums.",
    ]
)

DEPENDENCY_PDF = make_pdf_bytes(
    [
        "1. PAYMENT. Payment terms follow.",
        "1.1 Payment Period. Customer shall pay within thirty days.",
        "2. FEES. Fee terms follow.",
        "2.1 Late Fee. Customer shall pay a late fee on overdue amounts.",
        "3. DEFAULT. Customer is in default if it fails to pay under Clause 1.1 "
        "or Clause 2.1.",
    ]
)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="api_test_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir, "uploads")
        os.makedirs(self.upload_dir)

        # Explicit, test-scoped configuration. Nothing is monkeypatched on
        # db.get_db_path and the project's app.db/demo.db are never used.
        self._db_token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.addCleanup(db.reset_database_config, self._db_token)
        db.init_db()

        # Every upload-mode request is authenticated.  These are synthetic
        # local fixtures; no password or production credential exists.
        self.firm_id = auth.create_firm("API Test Firm")
        self.user_id = auth.create_user("api-test-user", "API Test User")
        auth.add_membership(self.firm_id, self.user_id, auth.ROLE_LAWYER)
        self.session_token = auth.create_session(
            self.user_id, self.firm_id, ttl_seconds=3600
        )
        self.principal = auth.get_session_principal(self.session_token)
        self.assertIsNotNone(self.principal)

        self.application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=Path(self.upload_dir),
        )
        self.client = TestClient(self.application)
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, self.session_token)

    def _upload(self, data: bytes, filename: str = "contract.pdf"):
        response = self.client.post(
            "/upload",
            files={"file": (filename, data, "application/pdf")},
        )
        payload = response.json()
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise HTTPException(status_code=response.status_code, detail=detail)
        return payload

    def _count_contracts(self):
        conn = db.get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        finally:
            conn.close()


class HealthTests(ApiTestCase):
    def test_health(self):
        self.assertEqual(app.health(), {"status": "ok"})


class UploadFlowTests(ApiTestCase):
    def test_successful_upload_persists_contract_and_clauses(self):
        response = self._upload(SECTIONS_PDF)
        self.assertEqual(response["status"], "extracted")
        self.assertEqual(response["analysis_status"], "ready")
        self.assertEqual(response["clauses_saved"], 4)
        self.assertEqual(response["clause_split_status"], "ok")
        self.assertEqual(response["graph"]["clauses"], 4)
        self.assertIn("analysis is ready", response["message"])

        contract = db.get_contract(response["contract_id"])
        self.assertIsNotNone(contract)
        self.assertEqual(contract["processing_status"], "extracted")
        self.assertTrue(contract["raw_text"])

        clauses = db.list_clauses(response["contract_id"])
        self.assertEqual(
            [c["section_number"] for c in clauses], ["1", "2", "3", "4"]
        )
        self.assertTrue(all(c["metadata"].get("type") for c in clauses))

    def test_upload_persists_nested_clauses_and_preserves_source_text(self):
        response = self._upload(HIERARCHICAL_PDF)
        clauses = db.list_clauses(response["contract_id"])
        by_number = {clause["section_number"]: clause for clause in clauses}

        self.assertIn("7", by_number)
        self.assertIn("7.2", by_number)
        self.assertIn("7.3", by_number)
        self.assertIn("Parent text before the first nested clause.",
                      by_number["7"]["clause_text"])
        self.assertIn("7.2 Payment Period.", by_number["7.2"]["clause_text"])
        self.assertIn("Pay within thirty days.", by_number["7.2"]["clause_text"])
        self.assertEqual(by_number["7.2"]["metadata"]["heading"],
                         "Payment Period")
        self.assertEqual(by_number["7.2"]["metadata"]["level"], 1)
        self.assertEqual(by_number["7.2"]["metadata"]["type"], "payment")

        returned = self.client.get(
            "/contracts/%d/clauses" % response["contract_id"]
        ).json()
        returned_by_number = {
            clause["section_number"]: clause for clause in returned["clauses"]
        }
        self.assertIn("7.2", returned_by_number["7.2"]["clause_text"])

    def test_uploaded_contract_builds_graph_and_supports_impact_analysis_offline(self):
        with mock.patch.object(graph.llm, "ask_json", side_effect=AssertionError(
                "upload graph construction must not call an LLM")) as ask_json:
            response = self._upload(DEPENDENCY_PDF)

        cid = response["contract_id"]
        self.assertEqual(response["analysis_status"], "ready")
        self.assertGreater(response["graph"]["nodes"], 0)
        ask_json.assert_not_called()

        rows = {c["section_number"]: c for c in db.list_clauses(cid)}
        self.assertEqual(rows["1.1"]["metadata"]["type"], "payment")
        self.assertEqual(rows["2.1"]["metadata"]["type"], "fee")
        self.assertEqual(rows["3"]["metadata"]["type"], "default")

        edges = {(edge["src"], edge["dst"], edge["rel"])
                 for edge in adapter.get_edges(cid)}
        self.assertIn(("3", "1.1", "depends_on"), edges)
        self.assertIn(("3", "2.1", "depends_on"), edges)

        analysis = self.client.post(
            "/impact",
            json={
                "contract_id": cid,
                "clause_id": "1.1",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(analysis.status_code, 200, analysis.text)
        report = analysis.json()
        self.assertEqual(report["changed_clause"]["sec_no"], "1.1")
        self.assertIn("3", [hit["sec_no"] for hit in report["direct"]])
        self.assertTrue(report["evidence"])

    def test_rebuilding_uploaded_graph_does_not_duplicate_nodes_or_edges(self):
        cid = self._upload(DEPENDENCY_PDF)["contract_id"]
        before = graph.get_graph(cid)

        stats = graph.build_graph(cid, use_llm=False)
        after = graph.get_graph(cid)

        self.assertEqual(len(after["nodes"]), len(before["nodes"]))
        self.assertEqual(len(after["edges"]), len(before["edges"]))
        self.assertEqual(stats["nodes"], len(before["nodes"]))
        self.assertEqual(stats["edges"], len(before["edges"]))

    def test_overlapping_clause_ids_keep_uploaded_graphs_isolated(self):
        first = self._upload(DEPENDENCY_PDF)["contract_id"]
        first_nodes = {node["clause_reference"] for node in adapter.get_nodes(first)}
        first_edges = adapter.get_edges(first)
        second = self._upload(DEPENDENCY_PDF)["contract_id"]

        self.assertNotEqual(first, second)
        self.assertEqual(
            {node["clause_reference"] for node in adapter.get_nodes(first)},
            first_nodes,
        )
        self.assertEqual(adapter.get_edges(first), first_edges)
        second_nodes = adapter.get_nodes(second)
        self.assertTrue(first_nodes & {node["clause_reference"] for node in second_nodes})
        self.assertTrue(all(node["contract_id"] == second for node in second_nodes))

        conn = db.get_conn()
        try:
            rows = conn.execute(
                """SELECT e.contract_id, s.contract_id, t.contract_id
                   FROM edges e JOIN nodes s ON s.node_id = e.source_node
                   JOIN nodes t ON t.node_id = e.target_node
                   WHERE e.contract_id IN (?, ?)""",
                (first, second),
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        for edge_contract, source_contract, target_contract in rows:
            self.assertEqual(edge_contract, source_contract)
            self.assertEqual(edge_contract, target_contract)

    def test_graph_build_failure_leaves_no_partial_graph_and_records_failure(self):
        # Force failure at the atomic publication boundary, after clauses are
        # saved but before any node or edge can become visible.
        with mock.patch.object(
            adapter,
            "replace_graph",
            side_effect=RuntimeError("simulated graph failure"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                self._upload(DEPENDENCY_PDF)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("text and clauses were saved", ctx.exception.detail)
        self.assertIn("analysis could not be completed", ctx.exception.detail)

        contract = db.list_contracts()[0]
        cid = contract["contract_id"]
        self.assertEqual(contract["processing_status"], "graph_build_failed")
        self.assertTrue(db.list_clauses(cid))
        self.assertEqual(adapter.get_nodes(cid), [])
        self.assertEqual(adapter.get_edges(cid), [])

        builds = db.list_graph_builds(cid)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["status"], "failed")
        self.assertEqual(builds[0]["error_type"], "RuntimeError")

        analysis_error = self.client.post(
            "/impact",
            json={
                "contract_id": cid,
                "clause_id": "1.1",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(analysis_error.status_code, 409)
        self.assertIn("Impact analysis is unavailable",
                      analysis_error.json()["detail"])

        conn = db.get_conn()
        try:
            trace = conn.execute(
                """SELECT action, details FROM traces
                   WHERE contract_id = ? ORDER BY trace_id DESC LIMIT 1""",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(trace["action"], "graph_construction")
        self.assertIn("graph construction failed", trace["details"])

    def test_clauses_endpoint_returns_sections_in_order(self):
        upload = self._upload(SECTIONS_PDF)
        body = self.client.get(
            "/contracts/%d/clauses" % upload["contract_id"]
        ).json()
        self.assertEqual(body["contract_id"], upload["contract_id"])
        self.assertEqual(
            [c["section_number"] for c in body["clauses"]], ["1", "2", "3", "4"]
        )
        for clause in body["clauses"]:
            self.assertIn(clause["section_number"], clause["clause_text"])

    def test_no_detected_sections_is_honest(self):
        response = self._upload(NO_SECTIONS_PDF)
        self.assertEqual(response["status"], "extracted")
        self.assertEqual(response["clauses_saved"], 0)
        self.assertEqual(response["clause_split_status"], "no_clauses_detected")
        self.assertEqual(response["analysis_status"], "unavailable")
        self.assertIn("analysis is unavailable", response["message"])

        # No clauses invented; endpoint returns an empty list, not an error.
        self.assertEqual(db.list_clauses(response["contract_id"]), [])
        body = self.client.get(
            "/contracts/%d/clauses" % response["contract_id"]
        ).json()
        self.assertEqual(body["clauses"], [])

    def test_corrupt_pdf_returns_422_and_saves_no_clauses(self):
        with self.assertRaises(HTTPException) as ctx:
            self._upload(b"this is definitely not a real pdf", "broken.pdf")
        self.assertEqual(ctx.exception.status_code, 422)

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT contract_id, processing_status FROM contracts"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["processing_status"], "extraction_failed")
        self.assertEqual(db.list_clauses(row["contract_id"]), [])

    def test_non_pdf_upload_returns_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._upload(b"hello", "notes.txt")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self._count_contracts(), 0)

    def test_oversized_content_length_returns_413(self):
        request = make_request(
            content_length=app.MAX_UPLOAD_SIZE + 5_000,
            application=self.application,
        )
        request.state.principal = self.principal
        with self.assertRaises(HTTPException) as ctx:
            app.upload_contract(
                request,
                make_upload(b"%PDF-1.4", "big.pdf"),
            )
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(self._count_contracts(), 0)

    def test_clause_persistence_failure_does_not_claim_extracted(self):
        with mock.patch.object(api_a, "save_clauses", side_effect=RuntimeError("boom")):
            with self.assertRaises(HTTPException) as ctx:
                self._upload(SECTIONS_PDF)
        self.assertEqual(ctx.exception.status_code, 500)

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT contract_id, processing_status FROM contracts"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["processing_status"], "clause_persistence_failed")
        self.assertEqual(db.list_clauses(row["contract_id"]), [])


class RetrievalTests(ApiTestCase):
    def test_unknown_contract_clauses_returns_404(self):
        response = self.client.get("/contracts/9999/clauses")
        self.assertEqual(response.status_code, 404)

    def test_unknown_contract_detail_returns_404(self):
        response = self.client.get("/contracts/9999")
        self.assertEqual(response.status_code, 404)

    def test_contract_list_and_detail_still_work(self):
        upload = self._upload(SECTIONS_PDF)
        contract_id = upload["contract_id"]

        listing = self.client.get("/contracts").json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["contract_id"], contract_id)
        self.assertNotIn("raw_text", listing[0])

        detail = self.client.get("/contracts/%d" % contract_id).json()
        self.assertEqual(detail["contract_id"], contract_id)
        self.assertIn("raw_text", detail)

    def test_routes_registered_without_conflict(self):
        routes = {
            (r.path, tuple(sorted(r.methods)))
            for r in app.app.routes
            if getattr(r, "methods", None)
        }
        self.assertIn(("/contracts/{contract_id}", ("GET",)), routes)
        self.assertIn(("/contracts/{contract_id}/clauses", ("GET",)), routes)
        self.assertIn(("/contracts", ("GET",)), routes)
        self.assertIn(("/upload", ("POST",)), routes)
        self.assertIn(("/health", ("GET",)), routes)


class ClauseExplanationTests(ApiTestCase):
    """Tests for the per-clause explanation endpoint."""

    EXPLANATION_KEYS = {
        "input",
        "summary",
        "key_points",
        "relevant_authorities",
        "matched_evidence",
        "possible_legal_relevance",
        "uncertainties_and_missing_information",
        "reasoning_trace",
        "disclaimer",
        "meta",
    }

    def test_explanation_for_existing_section(self):
        upload = self._upload(SECTIONS_PDF)
        response = self.client.get(
            "/contracts/%d/clauses/2/explanation" % upload["contract_id"]
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()

        self.assertIsInstance(result, dict)
        self.assertEqual(set(result.keys()), self.EXPLANATION_KEYS)
        self.assertIsInstance(result["relevant_authorities"], list)
        self.assertIn("This is not legal advice", result["disclaimer"])

    def test_unknown_contract_returns_404(self):
        response = self.client.get("/contracts/9999/clauses/1/explanation")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Contract not found")

    def test_unknown_section_returns_404(self):
        upload = self._upload(SECTIONS_PDF)
        response = self.client.get(
            "/contracts/%d/clauses/99/explanation" % upload["contract_id"]
        )
        self.assertEqual(response.status_code, 404)

    def test_section_matching_is_exact(self):
        upload = self._upload(SECTIONS_PDF)
        contract_id = upload["contract_id"]

        # Replace the detected clauses with a single section 30.
        db.save_clauses(
            contract_id,
            [{"section_number": "30", "clause_text": "30. THIRTY. Section thirty text."}],
        )

        # "3" must not match "30".
        missing = self.client.get(
            "/contracts/%d/clauses/3/explanation" % contract_id
        )
        self.assertEqual(missing.status_code, 404)

        result = self.client.get(
            "/contracts/%d/clauses/30/explanation" % contract_id
        ).json()
        self.assertIn("Section thirty text", result["input"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
