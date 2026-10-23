"""Canonical factory, route-manifest, and database-mode tests.

All databases here are throwaway files under a temporary directory. The
project's demo.db and app.db are never opened or modified.
"""

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pymupdf  # noqa: E402

import api_a  # noqa: E402
import api_auth  # noqa: E402
import api_b  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402
from app_config import (  # noqa: E402
    AppSettings,
    ConfigurationError,
    DEMO_DB_ENV,
    MODE_ENV,
    MODE_UPLOAD,
    UPLOAD_DB_ENV,
    load_settings,
)
from application import create_app, route_manifest  # noqa: E402

APP_DB = os.path.join(ROOT, "app.db")


def make_pdf_bytes(lines):
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
        "3. TERM. This Agreement lasts one year.",
    ]
)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def init_temp_database(path):
    with db.database_context(path):
        db.init_db()


class TempDirectoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="app_factory_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp_dir, name)


class RouteManifestTests(TempDirectoryTestCase):
    def test_app_and_server_entrypoints_expose_the_same_manifest(self):
        import app as app_entry
        import server as server_entry

        self.assertEqual(
            route_manifest(app_entry.app),
            route_manifest(server_entry.app),
        )

    def test_every_router_route_is_mounted_exactly_once(self):
        import app as app_entry

        manifest = route_manifest(app_entry.app)
        counts = Counter(manifest)
        for path, methods in manifest:
            self.assertEqual(
                counts[(path, methods)],
                1,
                f"{path} {methods} is mounted more than once",
            )

        for router in (api_auth.router, api_a.router, api_b.router):
            for route in router.routes:
                methods = tuple(sorted(route.methods))
                self.assertIn((route.path, methods), manifest)
                self.assertEqual(counts[(route.path, methods)], 1)

        self.assertEqual(counts[("/", ("GET",))], 1)

    def test_homepage_is_the_contract_intelligence_overview(self):
        from fastapi.testclient import TestClient
        import app as app_entry

        response = TestClient(app_entry.app).get("/")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("Contract Intelligence", body)
        self.assertIn('href="/impact-analysis"', body)
        self.assertIn('href="/explain"', body)
        self.assertIn("read-only", body)
        self.assertNotIn('id="upload-form"', body)

    def test_import_and_route_registration_do_not_create_app_db(self):
        before = os.path.exists(APP_DB)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import app, server; "
                    "from application import route_manifest; "
                    "assert route_manifest(app.app) == route_manifest(server.app); "
                    "print('ok')"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertEqual(os.path.exists(APP_DB), before)

    def test_building_and_inspecting_routes_does_not_create_a_database(self):
        target = self.path("never-created.db")
        application = create_app(mode="demo", database_path=target)
        self.assertTrue(route_manifest(application))
        self.assertFalse(os.path.exists(target))


class DemoReadOnlyTests(TempDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.demo_path = self.path("demo-fixture.db")
        init_temp_database(self.demo_path)

    def test_upload_is_rejected_without_changing_the_demo_database(self):
        from fastapi.testclient import TestClient

        before = file_hash(self.demo_path)
        application = create_app(mode="demo", database_path=self.demo_path)

        with TestClient(application) as client:
            response = client.post(
                "/upload",
                files={"file": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("read-only", response.json()["detail"])
        self.assertEqual(file_hash(self.demo_path), before)
        self.assertFalse(os.path.exists(self.path("uploads")))

    def test_read_only_connection_rejects_a_write(self):
        before = file_hash(self.demo_path)
        with db.database_context(self.demo_path, read_only=True):
            conn = db.get_conn()
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute(
                        """INSERT INTO contracts
                           (filename, upload_timestamp, processing_status, raw_text)
                           VALUES (?, ?, ?, ?)""",
                        ("x.pdf", "2026-01-01T00:00:00", "uploaded", "text"),
                    )
                    conn.commit()
            finally:
                conn.close()
        self.assertEqual(file_hash(self.demo_path), before)


class DemoReadRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.mkdtemp(prefix="app_factory_seed_")
        cls.demo_path = os.path.join(cls.tmp_dir, "seeded-demo.db")
        original_get_db_path = db.get_db_path
        try:
            seed.seed(db_path=cls.demo_path)
        finally:
            # seed() still uses the legacy assignment internally; restore it
            # so this test cannot leak a process-wide path to other tests.
            db.get_db_path = original_get_db_path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(create_app(mode="demo", database_path=self.demo_path))

    def test_existing_impact_route_is_accessible(self):
        response = self._client().post(
            "/impact",
            json={
                "contract_id": 1,
                "clause_id": "7.2",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["changed_clause"]["sec_no"], "7.2")

    def test_existing_explanation_route_is_accessible(self):
        response = self._client().get("/contracts/1/clauses/7.2/explanation")
        self.assertEqual(response.status_code, 200)
        self.assertIn("disclaimer", response.json())

    def test_existing_contract_and_clause_routes_are_accessible(self):
        client = self._client()
        self.assertEqual(client.get("/contracts").status_code, 200)
        self.assertEqual(client.get("/contracts/1").status_code, 200)
        self.assertEqual(client.get("/contracts/1/clauses").status_code, 200)
        self.assertEqual(client.get("/clauses/1").status_code, 200)

    def test_reads_do_not_change_the_seeded_database(self):
        before = file_hash(self.demo_path)
        client = self._client()
        client.get("/contracts")
        client.get("/clauses/1")
        client.post(
            "/impact",
            json={
                "contract_id": 1,
                "clause_id": "7.2",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(file_hash(self.demo_path), before)


class UploadModeConfigurationTests(TempDirectoryTestCase):
    def test_upload_mode_requires_an_explicit_database_path(self):
        env = {MODE_ENV: MODE_UPLOAD, UPLOAD_DB_ENV: ""}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(ConfigurationError):
                load_settings()
            with self.assertRaises(ConfigurationError):
                create_app(mode="upload")

    def test_upload_mode_rejects_the_synthetic_demo_database(self):
        demo_path = self.path("demo-fixture.db")
        init_temp_database(demo_path)
        with mock.patch.dict(os.environ, {DEMO_DB_ENV: demo_path}, clear=False):
            with self.assertRaises(ConfigurationError):
                load_settings(mode="upload", database_path=demo_path)

    def test_explicit_database_path_selects_upload_mode(self):
        upload_db = self.path("uploads.db")
        settings = load_settings(database_path=upload_db)
        self.assertEqual(settings.mode, "upload")
        self.assertFalse(settings.database_read_only)
        self.assertTrue(settings.uploads_enabled)

    def test_upload_mode_writes_only_to_its_own_database_and_storage(self):
        from fastapi.testclient import TestClient

        demo_path = self.path("demo-fixture.db")
        init_temp_database(demo_path)
        demo_hash = file_hash(demo_path)

        upload_db = self.path("local-uploads.db")
        upload_dir = self.path("local-uploads")
        with db.database_context(upload_db):
            db.init_db()
            firm_id = auth.create_firm("Upload Test Firm")
            user_id = auth.create_user("upload-test-user")
            auth.add_membership(firm_id, user_id, auth.ROLE_LAWYER)
            token = auth.create_session(user_id, firm_id, ttl_seconds=3600)

        application = create_app(
            mode="upload",
            database_path=upload_db,
            upload_dir=upload_dir,
        )

        with TestClient(application) as client:
            client.cookies.set(auth.SESSION_COOKIE_NAME, token)
            response = client.post(
                "/upload",
                files={"file": ("contract.pdf", SECTIONS_PDF, "application/pdf")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "extracted")
        self.assertTrue(os.path.exists(upload_db))
        self.assertTrue(os.listdir(upload_dir))
        self.assertEqual(file_hash(demo_path), demo_hash)

        with db.database_context(upload_db):
            contracts = db.list_contracts()
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0]["contract_id"], payload["contract_id"])

    def test_app_state_exposes_the_selected_mode(self):
        target = self.path("configured.db")
        application = create_app(
            mode="demo",
            database_path=target,
        )
        self.assertIsInstance(application.state.settings, AppSettings)
        self.assertEqual(application.state.settings.mode, "demo")
        self.assertTrue(application.state.settings.database_read_only)
        self.assertFalse(application.state.settings.uploads_enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
