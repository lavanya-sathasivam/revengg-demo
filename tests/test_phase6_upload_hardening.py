"""Phase 6 upload and PDF-processing hardening tests.

Every database and upload directory is created under a temporary directory.
The project's ``demo.db``, ``app.db``, and real uploads are never opened or
modified by this suite.

Run:
    py -3.11 -m unittest discover -s tests -p "test_phase6_upload_hardening.py" -v
    py -3.11 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Never let a test reach a real LLM provider, whatever the local .env says.
os.environ["LLM_PROVIDER"] = "mock"

import pymupdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import adapter  # noqa: E402
import api_a  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import extract  # noqa: E402
from app_config import (  # noqa: E402
    DEFAULT_UPLOAD_MAX_BYTES,
    DEFAULT_UPLOAD_MAX_EXTRACTED_CHARS,
    DEFAULT_UPLOAD_MAX_PAGES,
    DEFAULT_UPLOAD_MAX_PROCESSING_SECONDS,
    UPLOAD_MAX_BYTES_ENV,
    UPLOAD_MAX_EXTRACTED_CHARS_ENV,
    UPLOAD_MAX_PAGES_ENV,
    UPLOAD_MAX_PROCESSING_SECONDS_ENV,
    ConfigurationError,
    load_settings,
)
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


def make_pdf_bytes(lines):
    return make_pdf_pages([lines])


def make_encrypted_pdf():
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "1. PAYMENT. This text must stay encrypted.")
        return doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw="user-secret",
            permissions=0,
        )
    finally:
        doc.close()


SECTIONS_PDF = make_pdf_bytes(
    [
        "SERVICES AGREEMENT",
        "1. PAYMENT. The Customer shall pay within thirty days.",
        "2. DEFAULT. The Customer is in default if it fails to pay under Clause 1.",
    ]
)

TWO_PAGE_PDF = make_pdf_pages(
    [
        ["1. PAYMENT. The Customer shall pay within thirty days."],
        ["2. DEFAULT. The Customer is in default if it fails to pay under Clause 1."],
    ]
)

ENCRYPTED_PDF = make_encrypted_pdf()
NO_TEXT_PDF = make_pdf_pages([[]])
MALFORMED_PDF = b"%PDF-1.4\nthis is not a valid PDF structure\n%%EOF"
# A valid PDF whose cross-reference table was deliberately damaged. PyMuPDF
# can reconstruct it, but a reconstructed document must not be accepted.
REPAIRED_PDF = SECTIONS_PDF.replace(b"startxref", b"startxrXX", 1)
NON_PDF_BYTES = b"this is definitely not a real pdf"


def write_bytes(path, data):
    Path(path).write_bytes(data)
    return str(path)


class UploadLimitConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase6_config_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.db_path = os.path.join(self.tmp_dir, "config.db")

    def _clean_limit_env(self):
        return mock.patch.dict(
            os.environ,
            {
                UPLOAD_MAX_BYTES_ENV: "",
                UPLOAD_MAX_PAGES_ENV: "",
                UPLOAD_MAX_EXTRACTED_CHARS_ENV: "",
                UPLOAD_MAX_PROCESSING_SECONDS_ENV: "",
            },
            clear=False,
        )

    def test_default_limits_are_documented_local_test_values(self):
        with self._clean_limit_env():
            settings = load_settings(mode="upload", database_path=self.db_path)
        self.assertEqual(settings.upload_max_bytes, DEFAULT_UPLOAD_MAX_BYTES)
        self.assertEqual(settings.upload_max_pages, DEFAULT_UPLOAD_MAX_PAGES)
        self.assertEqual(
            settings.upload_max_extracted_chars,
            DEFAULT_UPLOAD_MAX_EXTRACTED_CHARS,
        )
        self.assertEqual(
            settings.upload_max_processing_seconds,
            DEFAULT_UPLOAD_MAX_PROCESSING_SECONDS,
        )

    def test_environment_can_override_each_limit(self):
        with mock.patch.dict(
            os.environ,
            {
                UPLOAD_MAX_BYTES_ENV: "2048",
                UPLOAD_MAX_PAGES_ENV: "3",
                UPLOAD_MAX_EXTRACTED_CHARS_ENV: "500",
                UPLOAD_MAX_PROCESSING_SECONDS_ENV: "4",
            },
            clear=False,
        ):
            settings = load_settings(mode="upload", database_path=self.db_path)
        self.assertEqual(settings.upload_max_bytes, 2048)
        self.assertEqual(settings.upload_max_pages, 3)
        self.assertEqual(settings.upload_max_extracted_chars, 500)
        self.assertEqual(settings.upload_max_processing_seconds, 4)

    def test_invalid_and_non_positive_limits_fail_closed(self):
        for name in (
            UPLOAD_MAX_BYTES_ENV,
            UPLOAD_MAX_PAGES_ENV,
            UPLOAD_MAX_EXTRACTED_CHARS_ENV,
            UPLOAD_MAX_PROCESSING_SECONDS_ENV,
        ):
            for value in ("not-an-integer", "0", "-1"):
                with self.subTest(name=name, value=value):
                    with self._clean_limit_env():
                        with mock.patch.dict(
                            os.environ, {name: value}, clear=False
                        ):
                            with self.assertRaises(ConfigurationError):
                                load_settings(
                                    mode="upload",
                                    database_path=self.db_path,
                                )


class ExtractionUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase6_extract_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def _write(self, data, name="document.pdf"):
        return write_bytes(os.path.join(self.tmp_dir, name), data)

    def _assert_code(self, data, code, status_code=422):
        path = self._write(data)
        with self.assertRaises(extract.PDFValidationError) as ctx:
            extract.extract_text(path)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status_code, status_code)
        self.assertNotIn(str(path), str(ctx.exception))

    def test_valid_pdf_extracts_text(self):
        path = self._write(SECTIONS_PDF)
        text = extract.extract_text(path)
        self.assertIn("1. PAYMENT.", text)
        self.assertIn("2. DEFAULT.", text)

    def test_non_pdf_bytes_are_rejected(self):
        self._assert_code(NON_PDF_BYTES, "not_a_pdf")

    def test_malformed_pdf_is_rejected_without_parser_detail(self):
        self._assert_code(MALFORMED_PDF, "malformed_pdf")

    def test_encrypted_pdf_is_rejected(self):
        self._assert_code(ENCRYPTED_PDF, "encrypted_pdf")

    def test_repaired_pdf_is_rejected(self):
        self._assert_code(REPAIRED_PDF, "malformed_pdf")

    def test_pdf_without_extractable_text_is_rejected(self):
        self._assert_code(NO_TEXT_PDF, "no_extractable_text")

    def test_page_limit_is_enforced_before_page_text_is_read(self):
        path = self._write(TWO_PAGE_PDF)
        with self.assertRaises(extract.PDFPageLimitError) as ctx:
            extract.extract_text(path, max_pages=1)
        self.assertEqual(ctx.exception.status_code, 413)

    def test_text_limit_is_enforced(self):
        path = self._write(SECTIONS_PDF)
        with self.assertRaises(extract.PDFTextLimitError) as ctx:
            extract.extract_text(path, max_extracted_chars=20)
        self.assertEqual(ctx.exception.status_code, 413)

    def test_timeout_is_checked_before_the_parser_opens_the_file(self):
        path = self._write(SECTIONS_PDF)
        with mock.patch.object(
            extract, "_monotonic", side_effect=[0.0, 100.0]
        ):
            with self.assertRaises(extract.PDFProcessingTimeoutError):
                extract.extract_text(path, max_processing_seconds=30)

    def test_timeout_is_checked_between_pages(self):
        path = self._write(TWO_PAGE_PDF)
        with mock.patch.object(
            extract, "_monotonic", side_effect=[0.0, 0.0, 0.0, 0.0, 100.0]
        ):
            with self.assertRaises(extract.PDFProcessingTimeoutError):
                extract.extract_text(path, max_processing_seconds=30)

    def test_timeout_is_checked_after_the_last_page(self):
        path = self._write(SECTIONS_PDF)
        with mock.patch.object(
            extract, "_monotonic", side_effect=[0.0, 0.0, 0.0, 0.0, 100.0]
        ):
            with self.assertRaises(extract.PDFProcessingTimeoutError):
                extract.extract_text(path, max_processing_seconds=30)


class Phase6UploadTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase6_upload_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir, "uploads")
        os.makedirs(self.upload_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self._db_token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, self._db_token)
        db.init_db()

        self.firm_a = auth.create_firm("Phase 6 Alpha Firm")
        self.firm_b = auth.create_firm("Phase 6 Beta Firm")
        self.lawyer = auth.create_user("phase6:lawyer", "Phase 6 Lawyer")
        self.reviewer = auth.create_user("phase6:reviewer", "Phase 6 Reviewer")
        auth.add_membership(self.firm_a, self.lawyer, auth.ROLE_LAWYER)
        auth.add_membership(self.firm_a, self.reviewer, auth.ROLE_REVIEWER)
        self.token = auth.create_session(
            self.lawyer, self.firm_a, ttl_seconds=3600
        )
        self.reviewer_token = auth.create_session(
            self.reviewer, self.firm_a, ttl_seconds=3600
        )

        self.application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=self.upload_dir,
        )
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)
        self._login(self.client)

    def _login(self, client, token=None):
        client.cookies.clear()
        token = self.token if token is None else token
        if token:
            client.cookies.set(auth.SESSION_COOKIE_NAME, token)
        return client

    def _client_with_limits(self, *, token=None, **overrides):
        settings = replace(
            load_settings(
                mode="upload",
                database_path=self.db_path,
                upload_dir=self.upload_dir,
            ),
            **overrides,
        )
        application = create_app(settings=settings)
        client = TestClient(application)
        self.addCleanup(client.close)
        return self._login(client, token=token)

    def _upload(
        self,
        data,
        filename="contract.pdf",
        mime="application/pdf",
        *,
        client=None,
    ):
        client = client or self.client
        return client.post(
            "/upload",
            files={"file": (filename, data, mime)},
        )

    def _contracts(self):
        return db.list_contracts_for_firm(self.firm_a)

    def _versions(self, contract_id):
        return db.list_contract_versions(contract_id)

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

    def _metadata(self, event):
        return json.loads(event["metadata_json"])

    def _audit_json(self):
        return json.dumps(self._events(), sort_keys=True, default=str)

    def _files(self):
        root = Path(self.upload_dir)
        if not root.exists():
            return []
        return sorted(
            str(path.relative_to(root)).replace("\\", "/")
            for path in root.rglob("*")
            if path.is_file()
        )

    def _assert_no_stored_files(self):
        self.assertEqual(self._files(), [])

    def _assert_failed_version(self, contract_id, expected_code):
        contract = db.get_contract(contract_id)
        self.assertIsNotNone(contract)
        self.assertEqual(contract["processing_status"], "extraction_failed")
        self.assertIsNone(contract["raw_text"])
        self.assertEqual(db.list_clauses(contract_id), [])

        versions = self._versions(contract_id)
        self.assertEqual(len(versions), 1)
        version = versions[0]
        self.assertEqual(version["status"], db.VERSION_STATUS_FAILED)
        self.assertEqual(version["extraction_status"], db.EXTRACTION_STATUS_FAILED)
        self.assertIsNone(version["source_path"])
        self.assertTrue(version["source_sha256"])
        self.assertGreater(version["source_size_bytes"], 0)
        self.assertEqual(
            version["provenance_status"], db.PROVENANCE_VERIFIED
        )

        failure = self._events("contract.extraction_failed")[-1]
        self.assertEqual(failure["outcome"], "failure")
        self.assertEqual(failure["target_id"], str(contract_id))
        self.assertEqual(self._metadata(failure)["reason_code"], expected_code)
        return contract, version


class UploadRouteTests(Phase6UploadTestCase):
    def test_valid_upload_is_published_under_a_server_generated_firm_path(self):
        filename = "Client Services Agreement.pdf"
        response = self._upload(SECTIONS_PDF, filename)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "extracted")
        self.assertEqual(body["analysis_status"], "ready")

        contract_id = body["contract_id"]
        contract = db.get_contract(contract_id)
        self.assertEqual(contract["processing_status"], "extracted")

        version = self._versions(contract_id)[0]
        self.assertEqual(version["firm_id"], self.firm_a)
        self.assertEqual(version["source_filename"], "Client_Services_Agreement.pdf")
        self.assertEqual(version["source_sha256"], hashlib.sha256(SECTIONS_PDF).hexdigest())
        self.assertEqual(version["source_size_bytes"], len(SECTIONS_PDF))
        self.assertEqual(version["source_mime"], "application/pdf")
        self.assertEqual(version["provenance_status"], db.PROVENANCE_VERIFIED)
        self.assertEqual(version["extraction_status"], db.EXTRACTION_STATUS_SUCCEEDED)
        self.assertEqual(version["status"], db.VERSION_STATUS_PUBLISHED)

        source_path = Path(version["source_path"])
        self.assertTrue(source_path.is_file())
        relative = source_path.relative_to(Path(self.upload_dir).resolve())
        self.assertEqual(relative.parts[0], "firm-%d" % self.firm_a)
        self.assertRegex(relative.name, r"^[0-9a-f]{32}\.pdf$")
        self.assertNotIn("Client", relative.name)
        self.assertEqual(source_path.read_bytes(), SECTIONS_PDF)
        self.assertEqual(self._files(), [str(relative).replace("\\", "/")])

        builds = db.list_graph_builds(contract_id)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["status"], adapter.GRAPH_STATUS_PUBLISHED)
        self.assertTrue(adapter.get_nodes(contract_id))

        created = self._events("contract.created")
        self.assertEqual(len(created), 1)
        metadata = self._metadata(created[0])
        self.assertEqual(metadata["source_sha256"], version["source_sha256"])
        self.assertEqual(metadata["source_size_bytes"], len(SECTIONS_PDF))
        self.assertNotIn("Customer shall pay", self._audit_json())

    def test_wrong_extension_is_rejected_before_staging(self):
        response = self._upload(SECTIONS_PDF, "notes.txt")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()
        event = self._events("upload.rejected")[-1]
        self.assertEqual(self._metadata(event)["reason"], "unsupported_extension")
        self.assertEqual(self._metadata(event)["status_code"], 400)

    def test_empty_pdf_is_rejected_without_a_contract_row(self):
        response = self._upload(b"", "empty.pdf")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()
        event = self._events("upload.rejected")[-1]
        self.assertEqual(self._metadata(event)["reason"], "empty_file")

    def test_non_pdf_content_with_pdf_extension_creates_a_failed_version(self):
        response = self._upload(NON_PDF_BYTES, "misleading.pdf")
        self.assertEqual(response.status_code, 422, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        contract, version = self._assert_failed_version(
            contracts[0]["contract_id"], "not_a_pdf"
        )
        self.assertEqual(version["source_filename"], "misleading.pdf")
        self.assertEqual(
            version["source_sha256"], hashlib.sha256(NON_PDF_BYTES).hexdigest()
        )
        self.assertEqual(version["source_size_bytes"], len(NON_PDF_BYTES))
        self._assert_no_stored_files()
        self.assertNotIn("contract.created", [e["action"] for e in self._events()])
        self.assertNotIn("definitely not a real pdf", response.text)

    def test_malformed_pdf_is_rejected_with_a_safe_error(self):
        response = self._upload(MALFORMED_PDF, "malformed.pdf")
        self.assertEqual(response.status_code, 422, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        self._assert_failed_version(contracts[0]["contract_id"], "malformed_pdf")
        self._assert_no_stored_files()
        self.assertNotIn("FzError", response.text)
        self.assertNotIn("no objects found", response.text)

    def test_encrypted_pdf_is_rejected_and_bytes_are_deleted(self):
        response = self._upload(ENCRYPTED_PDF, "encrypted.pdf")
        self.assertEqual(response.status_code, 422, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        self._assert_failed_version(contracts[0]["contract_id"], "encrypted_pdf")
        self._assert_no_stored_files()

    def test_no_text_pdf_is_rejected(self):
        response = self._upload(NO_TEXT_PDF, "scanned.pdf")
        self.assertEqual(response.status_code, 422, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        self._assert_failed_version(
            contracts[0]["contract_id"], "no_extractable_text"
        )
        self._assert_no_stored_files()

    def test_actual_size_limit_is_enforced_and_partial_temp_is_removed(self):
        client = self._client_with_limits(upload_max_bytes=64)
        response = self._upload(
            b"%PDF-" + b"x" * 200, "oversized.pdf", client=client
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()
        event = self._events("upload.rejected")[-1]
        self.assertEqual(self._metadata(event)["reason"], "file_too_large")
        self.assertEqual(self._metadata(event)["status_code"], 413)

    def test_page_limit_is_enforced_and_failed_version_is_recorded(self):
        client = self._client_with_limits(upload_max_pages=1)
        response = self._upload(TWO_PAGE_PDF, "two-pages.pdf", client=client)
        self.assertEqual(response.status_code, 413, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        self._assert_failed_version(
            contracts[0]["contract_id"], "page_limit_exceeded"
        )
        self._assert_no_stored_files()

    def test_text_limit_is_enforced_and_failed_version_is_recorded(self):
        client = self._client_with_limits(upload_max_extracted_chars=20)
        response = self._upload(SECTIONS_PDF, "too-much-text.pdf", client=client)
        self.assertEqual(response.status_code, 413, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        self._assert_failed_version(
            contracts[0]["contract_id"], "text_limit_exceeded"
        )
        self._assert_no_stored_files()

    def test_processing_timeout_is_enforced_cooperatively(self):
        client = self._client_with_limits(upload_max_processing_seconds=30)
        with mock.patch.object(
            extract, "_monotonic", side_effect=[0.0, 0.0, 0.0, 0.0, 100.0]
        ):
            response = self._upload(SECTIONS_PDF, "slow.pdf", client=client)
        self.assertEqual(response.status_code, 422, response.text)
        contracts = self._contracts()
        self.assertEqual(len(contracts), 1)
        self._assert_failed_version(
            contracts[0]["contract_id"], "processing_timeout"
        )
        self._assert_no_stored_files()

    def test_traversal_filename_is_metadata_only(self):
        response = self._upload(
            SECTIONS_PDF, "..\\..\\..\\Client Secrets.pdf"
        )
        self.assertEqual(response.status_code, 200, response.text)
        contract_id = response.json()["contract_id"]
        contract = db.get_contract(contract_id)
        self.assertEqual(contract["filename"], "Client_Secrets.pdf")

        version = self._versions(contract_id)[0]
        self.assertEqual(version["source_filename"], "Client_Secrets.pdf")
        source_path = Path(version["source_path"]).resolve()
        root = Path(self.upload_dir).resolve()
        self.assertEqual(source_path.parent.parent, root)
        self.assertRegex(source_path.name, r"^[0-9a-f]{32}\.pdf$")
        self.assertEqual(len(self._files()), 1)

    def test_parser_exception_is_sanitized_and_leaves_no_partial_record(self):
        with mock.patch.object(
            api_a,
            "extract_text",
            side_effect=RuntimeError("secret parser path C:\\private\\contract"),
        ):
            response = self._upload(SECTIONS_PDF, "explodes.pdf")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(
            response.json()["detail"],
            "Unexpected error while processing the PDF.",
        )
        self.assertNotIn("secret", response.text)
        self.assertNotIn("private", response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()

        event = self._events("upload.processing_failed")[-1]
        metadata = self._metadata(event)
        self.assertEqual(metadata["reason"], "parser_exception")
        self.assertEqual(metadata["error_type"], "RuntimeError")
        self.assertNotIn("secret parser path", event["metadata_json"])

    def test_database_failure_removes_the_published_file(self):
        with mock.patch.object(
            api_a,
            "save_contract_with_trace",
            side_effect=RuntimeError("database detail must not leak"),
        ):
            response = self._upload(SECTIONS_PDF, "database-fails.pdf")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("database detail", response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()
        event = self._events("upload.processing_failed")[-1]
        self.assertEqual(
            self._metadata(event)["reason"], "contract_record_failed"
        )

    def test_failed_upload_does_not_change_the_last_known_good_graph(self):
        first = self._upload(SECTIONS_PDF, "good.pdf")
        self.assertEqual(first.status_code, 200, first.text)
        contract_id = first.json()["contract_id"]
        nodes_before = adapter.get_nodes(contract_id)
        edges_before = adapter.get_edges(contract_id)
        builds_before = db.list_graph_builds(contract_id)
        self.assertTrue(nodes_before)
        self.assertEqual(len(builds_before), 1)

        failed = self._upload(MALFORMED_PDF, "bad.pdf")
        self.assertEqual(failed.status_code, 422, failed.text)

        self.assertEqual(adapter.get_nodes(contract_id), nodes_before)
        self.assertEqual(adapter.get_edges(contract_id), edges_before)
        self.assertEqual(db.list_graph_builds(contract_id), builds_before)
        self.assertEqual(
            db.get_contract(contract_id)["processing_status"], "extracted"
        )

    def test_anonymous_upload_is_rejected_before_any_file_is_stored(self):
        client = self._client_with_limits(token=False)
        response = self._upload(SECTIONS_PDF, "anonymous.pdf", client=client)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()
        self.assertTrue(self._events("auth.denied"))

    def test_reviewer_upload_is_denied_and_audited(self):
        client = self._client_with_limits(token=self.reviewer_token)
        response = self._upload(SECTIONS_PDF, "reviewer.pdf", client=client)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self._contracts(), [])
        self._assert_no_stored_files()
        event = self._events("upload.denied")[-1]
        self.assertEqual(self._metadata(event)["reason"], "insufficient_role")
        self.assertEqual(self._metadata(event)["role"], auth.ROLE_REVIEWER)

    def test_demo_mode_never_writes_an_upload_or_creates_storage(self):
        demo_upload_dir = os.path.join(self.tmp_dir, "demo-uploads")
        application = create_app(
            mode="demo",
            database_path=self.db_path,
            upload_dir=demo_upload_dir,
        )
        client = TestClient(application)
        self.addCleanup(client.close)
        response = client.post(
            "/upload",
            files={"file": ("contract.pdf", SECTIONS_PDF, "application/pdf")},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("read-only", response.json()["detail"])
        self.assertFalse(os.path.exists(demo_upload_dir))

    def test_startup_removes_only_abandoned_staging_files(self):
        stale = Path(self.upload_dir) / ".upload-abandoned.part"
        stale.write_bytes(b"partial")
        non_matching = Path(self.upload_dir) / ".upload-abandoned.tmp"
        non_matching.write_bytes(b"keep")
        accepted_dir = Path(self.upload_dir) / ("firm-%d" % self.firm_a)
        accepted_dir.mkdir(parents=True, exist_ok=True)
        accepted = accepted_dir / "accepted.pdf"
        accepted.write_bytes(SECTIONS_PDF)

        application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=self.upload_dir,
        )
        with TestClient(application) as client:
            self.assertEqual(client.get("/health").status_code, 200)

        self.assertFalse(stale.exists())
        self.assertTrue(non_matching.exists())
        self.assertTrue(accepted.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
