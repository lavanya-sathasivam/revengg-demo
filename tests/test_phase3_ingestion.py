"""Phase 3 reliable PDF ingestion tests.

Covers page-aware extraction, conservative normalization, clause hierarchy
and classification, source provenance, duplicate suppression, firm isolation,
processing logs, partial failures, and safe retries.

Every database and upload directory is created under a temporary directory;
the project's ``app.db``, ``demo.db`` and real uploads are never touched.

Run:
    py -3.11 -m unittest discover -s tests -p "test_phase3_ingestion.py" -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Never let a test reach a real LLM provider.
os.environ["LLM_PROVIDER"] = "mock"

import pymupdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import adapter  # noqa: E402
import api_a  # noqa: E402
import auth  # noqa: E402
import clauses as clause_tools  # noqa: E402
import db  # noqa: E402
import extract  # noqa: E402
import normalize  # noqa: E402
from application import create_app  # noqa: E402


def make_pdf_pages(page_lines):
    """Build a real PDF with one page per list of text lines."""
    doc = pymupdf.open()
    try:
        for lines in page_lines:
            page = doc.new_page()
            y = 72
            for line in lines:
                page.insert_text((72, y), line)
                y += 18
        return doc.tobytes()
    finally:
        doc.close()


TWO_PAGE_CONTRACT = make_pdf_pages(
    [
        [
            "SERVICES AGREEMENT",
            "1. SERVICES. The Supplier shall provide the services.",
            "2. PAYMENT. The Customer shall pay within thirty days.",
        ],
        [
            "3. TERMINATION. Either party may terminate for material breach.",
            "4. CONFIDENTIALITY. Each party shall keep confidential "
            "information secret.",
            "5. GOVERNING LAW. This Agreement is governed by the laws of India.",
            "6. OBLIGATIONS. Each party shall perform its stated duties.",
            "7. LIMITATION OF LIABILITY. Liability terms follow.",
            "7.2 Liability Cap. The Supplier shall be liable for direct damages.",
            "7.3 Exclusions. In no event shall aggregate liability exceed the "
            "fees paid.",
            "8. INDEMNITY. The Supplier shall indemnify the Customer.",
        ],
    ]
)


class IngestionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase3_ingest_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir, "uploads")
        os.makedirs(self.upload_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self._db_token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, self._db_token)
        db.init_db()

        self.firm_a = auth.create_firm("Phase 3 Alpha Firm")
        self.firm_b = auth.create_firm("Phase 3 Beta Firm")
        self.lawyer = auth.create_user("phase3:lawyer", "Phase 3 Lawyer")
        self.other_lawyer = auth.create_user("phase3:other", "Other Firm Lawyer")
        auth.add_membership(self.firm_a, self.lawyer, auth.ROLE_LAWYER)
        auth.add_membership(self.firm_b, self.other_lawyer, auth.ROLE_LAWYER)
        self.token = auth.create_session(
            self.lawyer, self.firm_a, ttl_seconds=3600
        )
        self.other_token = auth.create_session(
            self.other_lawyer, self.firm_b, ttl_seconds=3600
        )

        self.application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=self.upload_dir,
        )
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)
        self._login(self.client, self.token)

    def _login(self, client, token):
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE_NAME, token)
        return client

    def _upload(self, data, filename="contract.pdf", *, client=None):
        client = client or self.client
        return client.post(
            "/upload",
            files={"file": (filename, data, "application/pdf")},
        )

    def _clauses(self, contract_id):
        return db.list_clauses(contract_id)

    def _stages(self, contract_id):
        return db.list_processing_log(contract_id)

    def _events(self, action=None):
        conn = db.get_conn()
        try:
            sql = "SELECT * FROM audit_events"
            params = ()
            if action is not None:
                sql += " WHERE action = ?"
                params = (action,)
            sql += " ORDER BY event_id ASC"
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


class PageExtractionTests(IngestionTestCase):
    def test_extract_pages_preserves_page_boundaries_and_offsets(self):
        path = os.path.join(self.tmp_dir, "two-pages.pdf")
        Path(path).write_bytes(TWO_PAGE_CONTRACT)

        result = extract.extract_pages(path)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual([p["page_number"] for p in result["pages"]], [1, 2])

        text = result["text"]
        first, second = result["pages"]
        self.assertIn("1. SERVICES.", text[first["char_start"]:first["char_end"]])
        self.assertIn("2. PAYMENT.", text[first["char_start"]:first["char_end"]])
        self.assertNotIn("3. TERMINATION.", text[first["char_start"]:first["char_end"]])
        self.assertIn(
            "3. TERMINATION.", text[second["char_start"]:second["char_end"]]
        )
        self.assertEqual(first["char_start"], 0)
        self.assertLessEqual(first["char_end"], second["char_start"])
        self.assertEqual(second["char_end"], len(text))

        # The historical string API must return exactly the joined text.
        self.assertEqual(extract.extract_text(path), text)

    def test_uploaded_clauses_have_page_offsets_and_hierarchy(self):
        response = self._upload(TWO_PAGE_CONTRACT, "two-pages.pdf")
        self.assertEqual(response.status_code, 200, response.text)
        contract_id = response.json()["contract_id"]
        contract = db.get_contract(contract_id)
        raw_text = contract["raw_text"]
        clauses = self._clauses(contract_id)
        self.assertTrue(clauses)

        by_section = {row["section_number"]: row for row in clauses}
        for sec in ("1", "2", "3", "4", "5", "6", "7", "7.2", "7.3", "8"):
            self.assertIn(sec, by_section)

        for row in clauses:
            intro = "clause %s" % row["section_number"]
            self.assertIsNotNone(row["page_number"], intro)
            self.assertGreaterEqual(row["page_number"], 1, intro)
            self.assertIsNotNone(row["char_start"], intro)
            self.assertIsNotNone(row["char_end"], intro)
            self.assertLess(row["char_start"], row["char_end"], intro)
            self.assertLessEqual(row["char_end"], len(raw_text), intro)
            self.assertEqual(
                raw_text[row["char_start"]:row["char_end"]],
                row["clause_text"],
                intro,
            )
            self.assertTrue(row["normalized_text"], intro)

        self.assertEqual(by_section["1"]["page_number"], 1)
        self.assertEqual(by_section["2"]["page_number"], 1)
        self.assertEqual(by_section["3"]["page_number"], 2)
        self.assertEqual(by_section["8"]["page_number"], 2)

        parent = by_section["7"]
        child = by_section["7.2"]
        self.assertIsNotNone(child["parent_id"])
        self.assertEqual(child["parent_id"], parent["clause_id"])
        self.assertIsNone(parent["parent_id"])

    def test_clause_classification_keeps_required_categories(self):
        response = self._upload(TWO_PAGE_CONTRACT, "classified.pdf")
        self.assertEqual(response.status_code, 200, response.text)
        contract_id = response.json()["contract_id"]
        rows = {row["section_number"]: row for row in self._clauses(contract_id)}

        def ctype(sec):
            return (rows[sec]["metadata"] or {}).get("type")

        self.assertEqual(ctype("2"), "payment")
        self.assertEqual(ctype("3"), "termination")
        self.assertEqual(ctype("4"), "confidentiality")
        self.assertEqual(ctype("5"), "governing_law")
        self.assertEqual(ctype("6"), "obligations")
        self.assertEqual(ctype("7"), "limitation")
        self.assertEqual(ctype("7.2"), "liability")
        self.assertEqual(ctype("8"), "indemnity")

    def test_processing_log_records_each_successful_stage(self):
        response = self._upload(TWO_PAGE_CONTRACT, "stages.pdf")
        self.assertEqual(response.status_code, 200, response.text)
        contract_id = response.json()["contract_id"]

        stages = self._stages(contract_id)
        statuses = {(row["stage"], row["status"]) for row in stages}
        for stage in (
            db.PROCESSING_STAGE_EXTRACT,
            db.PROCESSING_STAGE_SEGMENT,
            db.PROCESSING_STAGE_CLASSIFY,
            db.PROCESSING_STAGE_PERSIST,
            db.PROCESSING_STAGE_GRAPH,
        ):
            self.assertIn((stage, db.PROCESSING_STATUS_SUCCEEDED), statuses)
        self.assertTrue(all(row["started_at"] for row in stages))
        self.assertTrue(all(row["finished_at"] for row in stages))

        # The API exposes the same operational history without contract text.
        api = self.client.get("/contracts/%d/processing" % contract_id)
        self.assertEqual(api.status_code, 200, api.text)
        payload = api.json()
        self.assertEqual(payload["processing_status"], "extracted")
        self.assertFalse(payload["retryable"])
        self.assertEqual(len(payload["stages"]), len(stages))
        self.assertNotIn("raw", json.dumps(payload).lower())


class NormalizationTests(unittest.TestCase):
    def test_normalization_is_conservative_and_preserves_structure(self):
        raw = (
            "1. PAYMENT.\r\n"
            "\r\n"
            "\r\n"
            "The Customer   shall pay within thirty days.\f\n"
            "2. TERM.\n"
            "The term is one year."
        )
        normalized = normalize.normalize_text(raw)
        self.assertNotIn("\r", normalized)
        self.assertNotIn("\f", normalized)
        self.assertIn("1. PAYMENT.", normalized)
        self.assertIn("The Customer shall pay within thirty days.", normalized)
        self.assertIn("2. TERM.", normalized)
        self.assertLess(normalized.index("1. PAYMENT."), normalized.index("2. TERM."))
        # No words are added, removed or reordered.
        self.assertEqual(normalized.split(), raw.replace("\f", " ").split())

    def test_normalize_section_number_has_one_meaning(self):
        self.assertEqual(normalize.normalize_section_number("Section 3."), "3")
        self.assertEqual(normalize.normalize_section_number(" 3 "), "3")
        self.assertEqual(clause_tools.sec_sort_key("2.10"),
                         clause_tools.sec_sort_key("2.10"))


class DuplicateAndIsolationTests(IngestionTestCase):
    def test_duplicate_upload_returns_existing_contract_and_keeps_one_clause_set(self):
        first = self._upload(TWO_PAGE_CONTRACT, "original.pdf")
        self.assertEqual(first.status_code, 200, first.text)
        contract_id = first.json()["contract_id"]
        clauses_before = self._clauses(contract_id)
        stages_before = self._stages(contract_id)

        second = self._upload(TWO_PAGE_CONTRACT, "renamed-copy.pdf")
        self.assertEqual(second.status_code, 200, second.text)
        body = second.json()
        self.assertTrue(body["duplicate"])
        self.assertEqual(body["contract_id"], contract_id)

        self.assertEqual(len(db.list_contracts_for_firm(self.firm_a)), 1)
        self.assertEqual(len(self._clauses(contract_id)), len(clauses_before))
        self.assertEqual(len(self._stages(contract_id)), len(stages_before))
        self.assertEqual(len(db.list_contract_versions(contract_id)), 1)
        self.assertEqual(len(self._events("upload.duplicate")), 1)

    def test_duplicate_scope_is_per_firm_and_reads_are_isolated(self):
        first = self._upload(TWO_PAGE_CONTRACT, "alpha.pdf")
        self.assertEqual(first.status_code, 200, first.text)
        alpha_id = first.json()["contract_id"]

        other_client = TestClient(self.application)
        self.addCleanup(other_client.close)
        self._login(other_client, self.other_token)

        # Same bytes in another firm are a new document, not a duplicate.
        beta = self._upload(TWO_PAGE_CONTRACT, "beta.pdf", client=other_client)
        self.assertEqual(beta.status_code, 200, beta.text)
        beta_id = beta.json()["contract_id"]
        self.assertNotEqual(alpha_id, beta_id)

        # A duplicate inside firm B resolves to firm B's document.
        beta_again = self._upload(
            TWO_PAGE_CONTRACT, "beta-copy.pdf", client=other_client
        )
        self.assertEqual(beta_again.status_code, 200, beta_again.text)
        self.assertEqual(beta_again.json()["contract_id"], beta_id)
        self.assertTrue(beta_again.json()["duplicate"])

        alpha_list = self.client.get("/contracts")
        beta_list = other_client.get("/contracts")
        self.assertEqual(
            [row["contract_id"] for row in alpha_list.json()], [alpha_id]
        )
        self.assertEqual(
            [row["contract_id"] for row in beta_list.json()], [beta_id]
        )
        self.assertEqual(self.client.get("/contracts/%d" % beta_id).status_code, 404)
        self.assertEqual(
            other_client.get("/contracts/%d" % alpha_id).status_code, 404
        )
        self.assertEqual(
            other_client.get("/contracts/%d/clauses" % alpha_id).status_code, 404
        )


class RetryTests(IngestionTestCase):
    def test_clause_persistence_failure_is_retryable_without_duplicate_clauses(self):
        with mock.patch.object(
            api_a, "save_clauses", side_effect=RuntimeError("simulated save failure")
        ):
            response = self._upload(TWO_PAGE_CONTRACT, "clause-fails.pdf")
        self.assertEqual(response.status_code, 500, response.text)

        contract = db.list_contracts_for_firm(self.firm_a)[0]
        contract_id = contract["contract_id"]
        self.assertEqual(contract["processing_status"], "clause_persistence_failed")
        self.assertEqual(self._clauses(contract_id), [])
        versions = db.list_contract_versions(contract_id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["status"], db.VERSION_STATUS_FAILED)

        failed_stages = self._stages(contract_id)
        self.assertIn(
            (db.PROCESSING_STAGE_PERSIST, db.PROCESSING_STATUS_FAILED, 1),
            {(row["stage"], row["status"], row["attempt_no"]) for row in failed_stages},
        )

        retry = self.client.post("/contracts/%d/retry" % contract_id)
        self.assertEqual(retry.status_code, 200, retry.text)
        body = retry.json()
        self.assertEqual(body["status"], "extracted")
        self.assertGreater(body["clauses_saved"], 0)

        self.assertEqual(
            db.get_contract(contract_id)["processing_status"], "extracted"
        )
        live_clauses = self._clauses(contract_id)
        self.assertEqual(len(live_clauses), body["clauses_saved"])
        self.assertEqual(
            len({row["section_number"] for row in live_clauses}),
            len(live_clauses),
        )

        versions = db.list_contract_versions(contract_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["status"], db.VERSION_STATUS_FAILED)
        self.assertEqual(versions[1]["status"], db.VERSION_STATUS_PUBLISHED)
        self.assertTrue(
            all(row["version_id"] == versions[1]["version_id"] for row in live_clauses)
        )

        stages = self._stages(contract_id)
        persist = [row for row in stages if row["stage"] == db.PROCESSING_STAGE_PERSIST]
        self.assertEqual(
            [(row["attempt_no"], row["status"]) for row in persist],
            [(1, "failed"), (2, "succeeded")],
        )

        # A second retry is rejected rather than creating another version.
        again = self.client.post("/contracts/%d/retry" % contract_id)
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(len(db.list_contract_versions(contract_id)), 2)

    def test_graph_failure_is_retryable_without_touching_clauses(self):
        with mock.patch.object(
            adapter, "replace_graph", side_effect=RuntimeError("graph down")
        ):
            response = self._upload(TWO_PAGE_CONTRACT, "graph-fails.pdf")
        self.assertEqual(response.status_code, 500, response.text)

        contract = db.list_contracts_for_firm(self.firm_a)[0]
        contract_id = contract["contract_id"]
        self.assertEqual(contract["processing_status"], "graph_build_failed")
        clauses_before = self._clauses(contract_id)
        self.assertTrue(clauses_before)
        version_count_before = len(db.list_contract_versions(contract_id))

        retry = self.client.post("/contracts/%d/retry" % contract_id)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["status"], "extracted")
        self.assertEqual(
            db.get_contract(contract_id)["processing_status"], "extracted"
        )
        self.assertEqual(len(self._clauses(contract_id)), len(clauses_before))
        self.assertEqual(
            len(db.list_contract_versions(contract_id)), version_count_before
        )

        graph_stages = [
            row
            for row in self._stages(contract_id)
            if row["stage"] == db.PROCESSING_STAGE_GRAPH
        ]
        self.assertEqual(
            [(row["attempt_no"], row["status"]) for row in graph_stages],
            [(1, "failed"), (2, "succeeded")],
        )

    def test_extraction_failure_is_not_retryable_without_source_bytes(self):
        malformed = b"%PDF-1.4\nthis is not a valid PDF structure\n%%EOF"
        response = self._upload(malformed, "broken.pdf")
        self.assertEqual(response.status_code, 422, response.text)
        contract = db.list_contracts_for_firm(self.firm_a)[0]
        contract_id = contract["contract_id"]
        self.assertEqual(contract["processing_status"], "extraction_failed")

        retry = self.client.post("/contracts/%d/retry" % contract_id)
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertIn("re-upload", retry.json()["detail"].lower())

        extract_stages = [
            row
            for row in self._stages(contract_id)
            if row["stage"] == db.PROCESSING_STAGE_EXTRACT
        ]
        self.assertEqual(len(extract_stages), 1)
        self.assertEqual(extract_stages[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
