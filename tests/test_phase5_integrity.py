"""Phase 5 database integrity, provenance, versioning, graph, and audit tests.

Every database and upload directory is created under a temporary directory.
The project's ``demo.db``, ``app.db``, and real uploads are never opened or
modified by this suite.

Run:
    py -3.11 -m unittest discover -s tests -p "test_phase5_integrity.py" -v
    py -3.11 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Never let a test reach a real LLM provider, whatever the local .env says.
os.environ["LLM_PROVIDER"] = "mock"

import pymupdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import adapter  # noqa: E402
import audit  # noqa: E402
import auth  # noqa: E402
import clauses as clause_tools  # noqa: E402
import db  # noqa: E402
import graph  # noqa: E402
import seed  # noqa: E402
from application import create_app  # noqa: E402


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


UPLOAD_PDF = make_pdf_bytes(
    [
        "SERVICES AGREEMENT",
        "1. PAYMENT. The Customer shall pay within thirty days.",
        "2. DEFAULT. The Customer is in default if it fails to pay under Clause 1.",
    ]
)


def clause(section_number, text, heading, clause_type="other"):
    return {
        "section_number": section_number,
        "clause_text": text,
        "metadata": {
            "heading": heading,
            "type": clause_type,
            "level": 1,
            "source": "sub_clause",
        },
    }


CLAUSES_V1 = [
    clause(
        "1",
        "1. Payment Period. The Customer shall pay each invoice within 30 days.",
        "Payment Period",
        "payment",
    ),
    clause(
        "2",
        "2. Default. The Customer is in default if it fails to pay under Clause 1.",
        "Default",
        "default",
    ),
]

CLAUSES_V2 = [
    clause(
        "1",
        "1. Payment Period. The Customer shall pay each invoice within 15 days.",
        "Payment Period",
        "payment",
    ),
    clause(
        "2",
        "2. Default. The Customer is in default if it fails to pay under Clause 1.",
        "Default",
        "default",
    ),
]


class Phase5TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase5_integrity_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir, "uploads")
        os.makedirs(self.upload_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self._db_token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, self._db_token)
        db.init_db()

    def _make_contract(
        self,
        clauses=None,
        *,
        filename="contract.pdf",
        firm_id=None,
        provenance=db.PROVENANCE_UNVERIFIED,
        build_graph=True,
    ):
        clauses = clauses or CLAUSES_V1
        raw_text = "\n\n".join(item["clause_text"] for item in clauses)
        contract_id = adapter.create_contract(
            filename,
            raw_text,
            status="extracted",
            firm_id=firm_id,
            provenance_status=provenance,
        )
        adapter.save_clauses(contract_id, clauses)
        if build_graph:
            graph.build_graph(contract_id, use_llm=False)
        return contract_id

    def _current_graph_build_id(self, contract_id):
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT current_graph_build_id FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            return row["current_graph_build_id"] if row else None
        finally:
            conn.close()

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

    def _audit_json(self):
        return json.dumps(self._events(), sort_keys=True, default=str)


class LegacyDatabaseTests(unittest.TestCase):
    """A pre-Phase-5 database must stay readable without an implicit migration."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase5_legacy_")
        self.db_path = os.path.join(self.tmp_dir, "legacy.db")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        conn = sqlite3.connect(self.db_path)
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
                    (contract_id, filename, upload_timestamp, processing_status,
                     raw_text)
                VALUES
                    (1, 'legacy.pdf', '2026-01-01T00:00:00+00:00', 'extracted',
                     'Legacy contract text.');
                INSERT INTO clauses
                    (clause_id, contract_id, section_number, clause_text, metadata)
                VALUES
                    (1, 1, '1', 'Legacy payment clause.', '{"heading":"Payment"}'),
                    (2, 1, '2', 'Legacy default clause.', '{"heading":"Default"}');
                INSERT INTO nodes
                    (node_id, clause_reference, node_type, contract_id, detail)
                VALUES
                    (10, '1', 'clause:payment', 1, 'Payment'),
                    (11, '1::payment', 'payment', 1, 'pay invoice'),
                    (12, '2', 'clause:default', 1, 'Default');
                INSERT INTO edges
                    (edge_id, source_node, target_node, relationship_type, contract_id)
                VALUES
                    (20, 10, 11, 'creates', 1),
                    (21, 12, 10, 'depends_on', 1);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _read_only(self):
        token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=True)
        )
        self.addCleanup(db.reset_database_config, token)

    def test_legacy_reads_are_labelled_and_do_not_run_ddl(self):
        self._read_only()

        contracts = db.list_contracts()
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0]["provenance_status"], db.PROVENANCE_LEGACY)
        self.assertIsNone(contracts[0]["current_version_id"])

        contract = db.get_contract(1)
        self.assertEqual(contract["provenance_status"], db.PROVENANCE_LEGACY)
        self.assertIsNone(db.get_current_version_id(1))
        self.assertIsNone(db.get_contract_for_firm(1, 99))

        provenance = db.get_contract_provenance(1)
        self.assertEqual(provenance["provenance_status"], db.PROVENANCE_LEGACY)
        self.assertTrue(provenance["legacy_unclassified"])
        self.assertIsNone(provenance["version"])
        self.assertIsNone(provenance["source"])

        self.assertEqual(db.list_contract_versions(1), [])
        self.assertEqual(db.list_clause_history(1), [])
        self.assertEqual(db.list_graph_builds(1), [])
        self.assertEqual(
            [row["clause_reference"] for row in adapter.get_nodes(1)],
            ["1", "1::payment", "2"],
        )
        self.assertEqual(
            adapter.get_edges(1),
            [
                {"src": "1", "dst": "1::payment", "rel": "creates"},
                {"src": "2", "dst": "1", "rel": "depends_on"},
            ],
        )

        # No read path may create a table or add a column in read-only mode.
        conn = db.get_conn()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(contracts)")
            }
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("ALTER TABLE contracts ADD COLUMN forbidden TEXT")
        finally:
            conn.close()
        self.assertNotIn("contract_versions", tables)
        self.assertNotIn("audit_events", tables)
        self.assertNotIn("firm_id", columns)
        self.assertNotIn("current_version_id", columns)
        self.assertNotIn("provenance_status", columns)

    def test_legacy_without_node_contract_scope_fails_closed(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                DROP TABLE edges;
                DROP TABLE nodes;
                CREATE TABLE nodes (
                    node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clause_reference TEXT,
                    node_type TEXT,
                    detail TEXT
                );
                CREATE TABLE edges (
                    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_node INTEGER NOT NULL,
                    target_node INTEGER NOT NULL,
                    relationship_type TEXT
                );
                INSERT INTO nodes (node_id, clause_reference, node_type, detail)
                VALUES (1, '1', 'clause:payment', 'Payment');
                INSERT INTO edges
                    (edge_id, source_node, target_node, relationship_type)
                VALUES (1, 1, 1, 'references');
                """
            )
            conn.commit()
        finally:
            conn.close()
        self._read_only()

        self.assertEqual(adapter.get_nodes(1), [])
        self.assertEqual(adapter.get_edges(1), [])

    def test_writable_migration_is_additive_and_keeps_legacy_provenance(self):
        token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, token)

        db.init_db()

        conn = db.get_conn()
        try:
            contract_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(contracts)")
            }
            clause_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(clauses)")
            }
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            contract = dict(
                conn.execute("SELECT * FROM contracts WHERE contract_id = 1")
                .fetchone()
            )
            clause_count = conn.execute("SELECT COUNT(*) FROM clauses").fetchone()[0]
        finally:
            conn.close()

        for column in (
            "firm_id",
            "current_version_id",
            "current_graph_build_id",
            "provenance_status",
        ):
            self.assertIn(column, contract_columns)
        self.assertIn("version_id", clause_columns)
        self.assertIn("clause_sha256", clause_columns)
        for table in (
            "contract_versions",
            "clause_history",
            "graph_builds",
            "audit_events",
        ):
            self.assertIn(table, tables)

        self.assertEqual(contract["filename"], "legacy.pdf")
        self.assertEqual(contract["provenance_status"], db.PROVENANCE_LEGACY)
        self.assertIsNone(contract["current_version_id"])
        self.assertEqual(clause_count, 2)
        self.assertEqual(db.list_contract_versions(1), [])
        self.assertEqual(db.get_contract_provenance(1)["provenance_status"],
                         db.PROVENANCE_LEGACY)


class VersionAndProvenanceTests(Phase5TestCase):
    def test_published_version_is_immutable_and_replacement_archives_history(self):
        contract_id = self._make_contract(build_graph=False)
        versions = db.list_contract_versions(contract_id)
        self.assertEqual(len(versions), 1)
        first = versions[0]
        self.assertEqual(first["version_number"], 1)
        self.assertEqual(first["status"], db.VERSION_STATUS_PUBLISHED)
        self.assertEqual(first["provenance_status"], db.PROVENANCE_UNVERIFIED)
        self.assertEqual(first["source_kind"], "programmatic")
        self.assertEqual(first["source_filename"], "contract.pdf")
        self.assertEqual(
            first["extracted_text_sha256"],
            db.sha256_text(
                "\n\n".join(item["clause_text"] for item in CLAUSES_V1)
            ),
        )

        with self.assertRaises(ValueError):
            adapter.save_clauses(contract_id, CLAUSES_V2, version_id=first["version_id"])
        self.assertEqual(
            [row["clause_text"] for row in db.list_clauses(contract_id)],
            [row["clause_text"] for row in CLAUSES_V1],
        )
        self.assertEqual(db.list_clause_history(contract_id), [])

        adapter.save_clauses(contract_id, CLAUSES_V2)

        versions = db.list_contract_versions(contract_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["version_id"], first["version_id"])
        self.assertEqual(versions[0]["status"], db.VERSION_STATUS_SUPERSEDED)
        self.assertEqual(versions[1]["version_number"], 2)
        self.assertEqual(versions[1]["status"], db.VERSION_STATUS_PUBLISHED)
        self.assertEqual(
            db.get_current_version_id(contract_id), versions[1]["version_id"]
        )

        history = db.list_clause_history(contract_id, first["version_id"])
        self.assertEqual(len(history), len(CLAUSES_V1))
        self.assertEqual(
            [row["clause_text"] for row in history],
            [row["clause_text"] for row in CLAUSES_V1],
        )
        self.assertTrue(all(row["archive_reason"] == "version_replaced" for row in history))
        self.assertTrue(all(row["clause_sha256"] for row in history))
        self.assertEqual(
            [row["clause_text"] for row in db.list_clauses(contract_id)],
            [row["clause_text"] for row in CLAUSES_V2],
        )

    def test_version_firm_and_contract_boundaries_are_enforced(self):
        firm_a = auth.create_firm("Firm A")
        firm_b = auth.create_firm("Firm B")
        contract_a = self._make_contract(firm_id=firm_a, build_graph=False)
        contract_b = self._make_contract(
            filename="other.pdf", firm_id=firm_b, build_graph=False
        )
        version_b = db.get_current_version_id(contract_b)
        self.assertIsNotNone(version_b)

        with self.assertRaises(ValueError):
            db.create_contract_version(contract_a, firm_id=firm_b)
        with self.assertRaises(ValueError):
            adapter.save_clauses(contract_a, CLAUSES_V2, version_id=version_b)

        self.assertEqual(len(db.list_contract_versions(contract_a)), 1)
        self.assertEqual(
            [row["clause_text"] for row in db.list_clauses(contract_a)],
            [row["clause_text"] for row in CLAUSES_V1],
        )

    def test_provenance_records_source_metadata_without_duplicating_text(self):
        contract_id = adapter.create_contract(
            "source.pdf",
            "Exact source text",
            status="extracted",
            provenance_status=db.PROVENANCE_SYNTHETIC,
        )
        version_id = db.create_contract_version(
            contract_id,
            source_filename="source.pdf",
            source_path="uploads/source.pdf",
            source_sha256="a" * 64,
            source_size_bytes=1234,
            source_mime="application/pdf",
            extracted_text_sha256=db.sha256_text("Exact source text"),
            clause_splitter_version=clause_tools.SPLITTER_VERSION,
            provenance_status=db.PROVENANCE_SYNTHETIC,
            source_kind="synthetic",
            status=db.VERSION_STATUS_DRAFT,
        )

        provenance = db.get_contract_provenance(contract_id)
        self.assertEqual(provenance["provenance_status"], db.PROVENANCE_SYNTHETIC)
        self.assertFalse(provenance["legacy_unclassified"])
        self.assertEqual(provenance["current_version_id"], version_id)
        self.assertEqual(provenance["source"]["sha256"], "a" * 64)
        self.assertEqual(provenance["source"]["size_bytes"], 1234)
        self.assertEqual(provenance["source"]["mime"], "application/pdf")
        self.assertEqual(provenance["source"]["source_kind"], "synthetic")
        self.assertNotIn("Exact source text", json.dumps(provenance["source"]))

    def test_contract_with_versions_cannot_be_deleted_directly(self):
        contract_id = self._make_contract()
        conn = db.get_conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM contracts WHERE contract_id = ?", (contract_id,)
                )
            conn.rollback()
        finally:
            conn.close()
        self.assertIsNotNone(db.get_contract(contract_id))
        self.assertTrue(db.list_contract_versions(contract_id))


class AtomicGraphTests(Phase5TestCase):
    def test_failed_rebuild_rolls_back_and_records_failure(self):
        contract_id = self._make_contract()
        build_id_before = self._current_graph_build_id(contract_id)
        nodes_before = adapter.get_nodes(contract_id)
        edges_before = adapter.get_edges(contract_id)
        builds_before = db.list_graph_builds(contract_id)
        self.assertTrue(nodes_before)
        self.assertTrue(edges_before)
        self.assertEqual(len(builds_before), 1)
        self.assertEqual(builds_before[0]["status"], adapter.GRAPH_STATUS_PUBLISHED)

        conn = db.get_conn()
        try:
            conn.execute(
                """
                CREATE TRIGGER phase5_force_graph_failure
                BEFORE INSERT ON nodes
                WHEN NEW.clause_reference = '2'
                BEGIN
                    SELECT RAISE(ABORT, 'forced graph failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        try:
            with self.assertRaises(sqlite3.IntegrityError):
                graph.build_graph(contract_id, use_llm=False)
        finally:
            conn = db.get_conn()
            try:
                conn.execute("DROP TRIGGER IF EXISTS phase5_force_graph_failure")
                conn.commit()
            finally:
                conn.close()

        self.assertEqual(self._current_graph_build_id(contract_id), build_id_before)
        self.assertEqual(adapter.get_nodes(contract_id), nodes_before)
        self.assertEqual(adapter.get_edges(contract_id), edges_before)

        builds_after = db.list_graph_builds(contract_id)
        self.assertEqual(len(builds_after), 2)
        self.assertEqual(builds_after[0]["status"], adapter.GRAPH_STATUS_PUBLISHED)
        failed = builds_after[1]
        self.assertEqual(failed["status"], adapter.GRAPH_STATUS_FAILED)
        self.assertEqual(failed["contract_id"], contract_id)
        self.assertEqual(failed["version_id"], db.get_current_version_id(contract_id))
        self.assertEqual(failed["error_type"], "IntegrityError")
        self.assertIn("forced graph failure", failed["error_message"])
        self.assertIsNone(failed["build_hash"])

    def test_invalid_graph_is_rejected_before_publication(self):
        contract_id = self._make_contract()
        nodes_before = adapter.get_nodes(contract_id)
        edges_before = adapter.get_edges(contract_id)
        builds_before = db.list_graph_builds(contract_id)

        with self.assertRaises(ValueError):
            adapter.replace_graph(
                contract_id,
                [{"ref": "1", "node_type": "clause:payment"}],
                [{"source": 0, "target": 99, "relationship_type": "depends_on"}],
            )

        self.assertEqual(adapter.get_nodes(contract_id), nodes_before)
        self.assertEqual(adapter.get_edges(contract_id), edges_before)
        self.assertEqual(db.list_graph_builds(contract_id), builds_before)

    def test_graph_version_must_belong_to_the_contract(self):
        contract_id = self._make_contract()
        other_id = self._make_contract(filename="other.pdf")
        other_version = db.get_current_version_id(other_id)
        builds_before = db.list_graph_builds(contract_id)
        nodes_before = adapter.get_nodes(contract_id)

        with self.assertRaises(ValueError):
            adapter.replace_graph(contract_id, [], [], version_id=other_version)

        self.assertEqual(db.list_graph_builds(contract_id), builds_before)
        self.assertEqual(adapter.get_nodes(contract_id), nodes_before)

    def test_graph_hash_is_stable_and_content_sensitive(self):
        nodes = [
            {"ref": "1", "node_type": "clause:payment", "detail": "Payment"},
            {"ref": "1::payment", "node_type": "payment", "detail": "pay"},
        ]
        edges = [
            {"source": 0, "target": 1, "relationship_type": "creates"},
        ]
        first = adapter.compute_graph_hash(nodes, edges)
        second = adapter.compute_graph_hash(
            [dict(node) for node in nodes], [dict(edge) for edge in edges]
        )
        self.assertEqual(first, second)
        changed = [dict(node) for node in nodes]
        changed[0]["detail"] = "Changed"
        self.assertNotEqual(first, adapter.compute_graph_hash(changed, edges))


class AuditTrailTests(Phase5TestCase):
    def test_sanitizer_removes_secrets_and_truncates_long_values(self):
        payload = {
            "clause_text": "CLIENT CONFIDENTIAL CLAUSE TEXT",
            "nested": {"token": "raw-token-value", "safe": "visible"},
            "authorization": "Bearer abc.def.ghi",
            "note": "Bearer abc.def.ghi",
            "long_value": "x" * 600,
            "count": 3,
            "filename": "contract.pdf",
        }
        sanitized = audit.sanitize_metadata(payload)
        serialized = json.dumps(sanitized, sort_keys=True)

        self.assertNotIn("CLIENT CONFIDENTIAL", serialized)
        self.assertNotIn("raw-token-value", serialized)
        self.assertNotIn("abc.def.ghi", serialized)
        self.assertEqual(sanitized["clause_text"], "[redacted]")
        self.assertEqual(sanitized["nested"]["token"], "[redacted]")
        self.assertEqual(sanitized["nested"]["safe"], "visible")
        self.assertEqual(sanitized["authorization"], "[redacted]")
        self.assertEqual(sanitized["note"], "[redacted]")
        self.assertTrue(sanitized["long_value"].endswith("...[truncated]"))
        self.assertEqual(sanitized["count"], 3)
        self.assertEqual(sanitized["filename"], "contract.pdf")

    def test_audit_events_are_append_only_and_store_sanitized_metadata(self):
        event_id = audit.record_event(
            "phase5.test",
            "success",
            actor_type="user",
            metadata={
                "clause_text": "SECRET CLAUSE",
                "token": "SECRET-TOKEN",
                "safe": "kept",
            },
        )

        conn = db.get_conn()
        try:
            row = dict(
                conn.execute(
                    "SELECT * FROM audit_events WHERE event_id = ?", (event_id,)
                ).fetchone()
            )
            stored = json.loads(row["metadata_json"])
            self.assertEqual(stored["clause_text"], "[redacted]")
            self.assertEqual(stored["token"], "[redacted]")
            self.assertEqual(stored["safe"], "kept")
            self.assertNotIn("SECRET", row["metadata_json"])

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE audit_events SET outcome = 'tampered' WHERE event_id = ?",
                    (event_id,),
                )
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM audit_events WHERE event_id = ?", (event_id,)
                )
            conn.rollback()

            persisted = conn.execute(
                "SELECT outcome FROM audit_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            self.assertEqual(persisted["outcome"], "success")
        finally:
            conn.close()


class UploadAndAuthorizationAuditTests(Phase5TestCase):
    def setUp(self):
        super().setUp()
        self.firm_a = auth.create_firm("Alpha Firm")
        self.firm_b = auth.create_firm("Beta Firm")
        self.lawyer = auth.create_user("phase5:lawyer", "Lawyer")
        self.reviewer = auth.create_user("phase5:reviewer", "Reviewer")
        auth.add_membership(self.firm_a, self.lawyer, auth.ROLE_LAWYER)
        auth.add_membership(self.firm_a, self.reviewer, auth.ROLE_REVIEWER)
        self.token_lawyer = auth.create_session(
            self.lawyer, self.firm_a, ttl_seconds=3600
        )
        self.token_reviewer = auth.create_session(
            self.reviewer, self.firm_a, ttl_seconds=3600
        )
        self.foreign_contract = self._make_contract(
            filename="foreign.pdf",
            firm_id=self.firm_b,
            provenance=db.PROVENANCE_VERIFIED,
        )

        self.application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=self.upload_dir,
        )
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)

    def _login(self, token):
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, token)

    def test_upload_records_source_provenance_and_lifecycle_audit(self):
        self._login(self.token_lawyer)
        response = self.client.post(
            "/upload",
            files={"file": ("source.pdf", UPLOAD_PDF, "application/pdf")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        contract_id = body["contract_id"]
        self.assertGreater(body["clauses_saved"], 0)

        contract = db.get_contract(contract_id)
        version_id = contract["current_version_id"]
        self.assertIsNotNone(version_id)
        self.assertEqual(contract["provenance_status"], db.PROVENANCE_VERIFIED)

        version = db.get_contract_version(version_id)
        self.assertEqual(version["source_filename"], "source.pdf")
        self.assertEqual(version["source_mime"], "application/pdf")
        self.assertEqual(version["source_size_bytes"], len(UPLOAD_PDF))
        self.assertEqual(
            version["source_sha256"], hashlib.sha256(UPLOAD_PDF).hexdigest()
        )
        self.assertEqual(version["provenance_status"], db.PROVENANCE_VERIFIED)
        self.assertEqual(version["source_kind"], "upload")
        self.assertEqual(version["extraction_status"], db.EXTRACTION_STATUS_SUCCEEDED)
        self.assertEqual(
            version["clause_splitter_version"], clause_tools.SPLITTER_VERSION
        )
        self.assertEqual(version["status"], db.VERSION_STATUS_PUBLISHED)
        self.assertTrue(os.path.exists(version["source_path"]))
        with open(version["source_path"], "rb") as handle:
            self.assertEqual(handle.read(), UPLOAD_PDF)

        conn = db.get_conn()
        try:
            clause_rows = conn.execute(
                """SELECT version_id, clause_sha256 FROM clauses
                   WHERE contract_id = ?""",
                (contract_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(clause_rows)
        self.assertTrue(all(row["version_id"] == version_id for row in clause_rows))
        self.assertTrue(all(row["clause_sha256"] for row in clause_rows))

        builds = db.list_graph_builds(contract_id)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["status"], adapter.GRAPH_STATUS_PUBLISHED)
        self.assertEqual(builds[0]["version_id"], version_id)
        self.assertEqual(
            self._current_graph_build_id(contract_id), builds[0]["build_id"]
        )
        self.assertTrue(builds[0]["build_hash"])

        events = self._events()
        actions = [event["action"] for event in events]
        for action in (
            "contract.created",
            "clauses.persisted",
            "graph.build_published",
        ):
            self.assertIn(action, actions)
        created = next(event for event in events if event["action"] == "contract.created")
        created_meta = json.loads(created["metadata_json"])
        self.assertEqual(created_meta["source_sha256"], hashlib.sha256(UPLOAD_PDF).hexdigest())
        self.assertEqual(created_meta["source_size_bytes"], len(UPLOAD_PDF))
        self.assertEqual(created_meta["source_mime"], "application/pdf")
        self.assertEqual(created["actor_user_id"], self.lawyer)
        self.assertEqual(created["firm_id"], self.firm_a)

        serialized = self._audit_json()
        self.assertNotIn("The Customer shall pay", serialized)
        self.assertNotIn(self.token_lawyer, serialized)
        self.assertNotIn(self.token_reviewer, serialized)

    def test_anonymous_and_role_denials_are_audited(self):
        self.client.cookies.clear()
        response = self.client.get("/contracts")
        self.assertEqual(response.status_code, 401, response.text)
        denied = self._events("auth.denied")
        self.assertTrue(denied)
        self.assertEqual(denied[-1]["target_type"], "route")
        self.assertEqual(denied[-1]["target_id"], "/contracts")
        self.assertEqual(denied[-1]["actor_type"], "anonymous")
        self.assertEqual(
            json.loads(denied[-1]["metadata_json"])["reason"], "not_authenticated"
        )

        self._login(self.token_reviewer)
        response = self.client.post(
            "/upload",
            files={"file": ("blocked.pdf", UPLOAD_PDF, "application/pdf")},
        )
        self.assertEqual(response.status_code, 403, response.text)
        upload_denied = self._events("upload.denied")
        self.assertTrue(upload_denied)
        event = upload_denied[-1]
        self.assertEqual(event["outcome"], "denied")
        self.assertEqual(event["actor_user_id"], self.reviewer)
        self.assertEqual(event["firm_id"], self.firm_a)
        self.assertEqual(json.loads(event["metadata_json"])["reason"], "insufficient_role")
        self.assertEqual(json.loads(event["metadata_json"])["role"], auth.ROLE_REVIEWER)

    def test_foreign_contract_denial_is_audited(self):
        self._login(self.token_lawyer)
        response = self.client.get("/contracts/%d" % self.foreign_contract)
        self.assertEqual(response.status_code, 404, response.text)

        denied = self._events("contract.access_denied")
        self.assertTrue(denied)
        event = denied[-1]
        self.assertEqual(event["actor_user_id"], self.lawyer)
        self.assertEqual(event["firm_id"], self.firm_a)
        self.assertEqual(event["target_type"], "contract")
        self.assertEqual(event["target_id"], str(self.foreign_contract))
        self.assertEqual(
            json.loads(event["metadata_json"])["reason"], "not_found_or_foreign"
        )


class SeedResetTests(Phase5TestCase):
    def test_reset_clears_contract_data_but_preserves_audit_history(self):
        contract_id = self._make_contract()
        first_event = audit.record_event(
            "phase5.before_reset", metadata={"contract_id": contract_id}
        )
        self.assertIsNotNone(first_event)

        conn = db.get_conn()
        try:
            seed.reset_demo_tables(conn)
            conn.commit()
            for table in (
                "edges",
                "nodes",
                "clauses",
                "clause_history",
                "graph_builds",
                "contract_versions",
                "contracts",
                "traces",
            ):
                count = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                self.assertEqual(count, 0, table)
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0]
            self.assertGreaterEqual(audit_count, 1)
            persisted = conn.execute(
                "SELECT event_id FROM audit_events WHERE event_id = ?",
                (first_event,),
            ).fetchone()
            self.assertIsNotNone(persisted)
        finally:
            conn.close()

        second_event = audit.record_event("phase5.after_reset")
        self.assertGreater(second_event, first_event)


if __name__ == "__main__":
    unittest.main()
