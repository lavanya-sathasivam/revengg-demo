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
from starlette.requests import Request

import app
import db


def make_request(content_length=None):
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": headers}
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


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="api_test_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir, "uploads")
        os.makedirs(self.upload_dir)

        self._orig_get_db_path = db.get_db_path
        self._orig_upload_dir = app.UPLOAD_DIR
        db.get_db_path = lambda: self.db_path
        app.UPLOAD_DIR = Path(self.upload_dir)

        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.addCleanup(self._restore)
        db.init_db()

    def _restore(self):
        db.get_db_path = self._orig_get_db_path
        app.UPLOAD_DIR = self._orig_upload_dir

    def _upload(self, data: bytes, filename: str = "contract.pdf"):
        return app.upload_contract(make_request(), make_upload(data, filename))

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
        self.assertEqual(response["clauses_saved"], 4)
        self.assertEqual(response["clause_split_status"], "ok")

        contract = db.get_contract(response["contract_id"])
        self.assertIsNotNone(contract)
        self.assertEqual(contract["processing_status"], "extracted")
        self.assertTrue(contract["raw_text"])

        clauses = db.list_clauses(response["contract_id"])
        self.assertEqual(
            [c["section_number"] for c in clauses], ["1", "2", "3", "4"]
        )

    def test_clauses_endpoint_returns_sections_in_order(self):
        upload = self._upload(SECTIONS_PDF)
        body = app.get_contract_clauses_route(upload["contract_id"])
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

        # No clauses invented; endpoint returns an empty list, not an error.
        self.assertEqual(db.list_clauses(response["contract_id"]), [])
        body = app.get_contract_clauses_route(response["contract_id"])
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
        with self.assertRaises(HTTPException) as ctx:
            app.upload_contract(
                make_request(content_length=app.MAX_UPLOAD_SIZE + 5_000),
                make_upload(b"%PDF-1.4", "big.pdf"),
            )
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(self._count_contracts(), 0)

    def test_clause_persistence_failure_does_not_claim_extracted(self):
        with mock.patch.object(app, "save_clauses", side_effect=RuntimeError("boom")):
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
        with self.assertRaises(HTTPException) as ctx:
            app.get_contract_clauses_route(9999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_contract_detail_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            app.get_contract_route(9999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_contract_list_and_detail_still_work(self):
        upload = self._upload(SECTIONS_PDF)
        contract_id = upload["contract_id"]

        listing = app.list_contracts_route()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["contract_id"], contract_id)
        self.assertNotIn("raw_text", listing[0])

        detail = app.get_contract_route(contract_id)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
