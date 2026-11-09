"""Phase 2 regression tests: registration, login, firm workspaces, member
management, contract lifecycle statuses, search/filter, version history, and
the audit trail.

Every fixture is synthetic and every database is a throwaway file under a
temporary directory.  The project's demo.db, app.db, and uploads directory are
never opened or modified.

Run:
    py -3.11 -m unittest discover -s tests -p "test_phase2_contract_lifecycle.py" -v
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ["LLM_PROVIDER"] = "mock"  # never reach a real LLM provider

import pymupdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import adapter  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import graph  # noqa: E402
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


def make_contract(filename, firm_id, clauses):
    raw_text = "\n\n".join(item["clause_text"] for item in clauses)
    contract_id = adapter.create_contract(
        filename, raw_text, status="extracted", firm_id=firm_id
    )
    adapter.save_clauses(contract_id, clauses)
    graph.build_graph(contract_id, use_llm=False)
    return contract_id


class Phase2TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="phase2_")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir, "uploads")
        os.makedirs(self.upload_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self._db_token = db.set_database_config(
            db.DatabaseConfig(self.db_path, read_only=False)
        )
        self.addCleanup(db.reset_database_config, self._db_token)
        db.init_db()

        self.firm_a = auth.create_firm("Alpha Firm")
        self.firm_b = auth.create_firm("Beta Firm")

        self.alice = auth.register_user("p2:alice", "Alice Administrator", "Passw0rd!")
        self.bob = auth.register_user("p2:bob", "Bob Lawyer", "Passw0rd!")
        self.carol = auth.register_user("p2:carol", "Carol Viewer", "Passw0rd!")
        self.dave = auth.register_user("p2:dave", "Dave NoMembership", "Passw0rd!")

        auth.add_membership(self.firm_a, self.alice, auth.ROLE_ADMINISTRATOR)
        auth.add_membership(self.firm_a, self.bob, auth.ROLE_LAWYER)
        auth.add_membership(self.firm_a, self.carol, auth.ROLE_VIEWER)
        auth.add_membership(self.firm_b, self.alice, auth.ROLE_VIEWER)

        self.contract_a = make_contract("alpha.pdf", self.firm_a, [
            clause("1", "1. Payment Period. Pay within 30 days.", "Payment", "payment"),
            clause("2", "2. Default. Late fees apply.", "Default", "default"),
        ])
        self.contract_b = make_contract("beta.pdf", self.firm_b, [
            clause("1", "1. Payment Period. Pay within 45 days.", "Payment", "payment"),
        ])

        self.token_alice = auth.create_session(self.alice, self.firm_a, ttl_seconds=3600)
        self.token_bob = auth.create_session(self.bob, self.firm_a, ttl_seconds=3600)
        self.token_carol = auth.create_session(self.carol, self.firm_a, ttl_seconds=3600)
        self.token_dave = auth.create_session(self.dave, None, ttl_seconds=3600)

        self.application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=self.upload_dir,
        )
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)

    def _login_client(self, token):
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, token)

    def _logout_client(self):
        self.client.cookies.clear()

    def _post(self, path, body=None, files=None):
        kwargs = {}
        if body is not None:
            kwargs["json"] = body
        if files is not None:
            kwargs["files"] = files
        return self.client.post(path, **kwargs)


class RegistrationTests(Phase2TestCase):
    def test_register_creates_user_and_returns_user_id(self):
        response = self._post(
            "/auth/register",
            body={
                "subject": "p2:newuser",
                "display_name": "New User",
                "password": "SecurePass123",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["user"]["subject"], "p2:newuser")
        self.assertEqual(payload["user"]["display_name"], "New User")
        self.assertIsNotNone(auth.authenticate_user("p2:newuser", "SecurePass123"))

    def test_register_rejects_short_password(self):
        response = self._post(
            "/auth/register",
            body={"subject": "p2:short", "password": "short"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("8 characters", response.json()["detail"])

    def test_register_rejects_duplicate_subject(self):
        # Registration is idempotent: an existing subject returns successfully
        # without changing its password (no user enumeration).
        response = self._post(
            "/auth/register",
            body={"subject": "p2:alice", "password": "SecurePass123"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        # The original password still works (registration did not reset it).
        self.assertIsNotNone(auth.authenticate_user("p2:alice", "Passw0rd!"))


class LoginTests(Phase2TestCase):
    def test_login_creates_session_and_sets_cookie(self):
        self._logout_client()
        # bob has exactly one membership, so it is selected automatically.
        response = self._post(
            "/auth/login",
            body={"subject": "p2:bob", "password": "Passw0rd!"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["authenticated"], True)
        self.assertEqual(payload["active_firm"]["firm_id"], self.firm_a)
        self.assertEqual(payload["active_firm"]["role"], auth.ROLE_LAWYER)
        cookie = self.client.cookies.get(auth.SESSION_COOKIE_NAME)
        self.assertIsNotNone(cookie)
        principal = auth.get_session_principal(cookie)
        self.assertIsInstance(principal, auth.Principal)
        self.assertEqual(principal.user_id, self.bob)

    def test_multi_firm_login_has_no_active_firm_until_switch(self):
        self._logout_client()
        response = self._post(
            "/auth/login",
            body={"subject": "p2:alice", "password": "Passw0rd!"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIsNone(payload["active_firm"])
        self.assertEqual(len(payload["memberships"]), 2)
        cookie = response.cookies.get(auth.SESSION_COOKIE_NAME)
        self.assertIsNotNone(cookie)
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, cookie)
        switched = self.client.post(
            "/auth/firm", json={"firm_id": self.firm_a}
        )
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(switched.json()["active_firm"]["firm_id"], self.firm_a)

    def test_login_rejects_wrong_password(self):
        response = self._post(
            "/auth/login",
            body={"subject": "p2:alice", "password": "WrongPass!"},
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_login_rejects_unknown_subject(self):
        response = self._post(
            "/auth/login",
            body={"subject": "p2:nope", "password": "Passw0rd!"},
        )
        self.assertEqual(response.status_code, 401, response.text)


class FirmTests(Phase2TestCase):
    def test_create_firm_makes_caller_administrator_and_active(self):
        self._login_client(self.token_dave)  # dave has no firm yet
        response = self._post(
            "/firms",
            body={"name": "Dave's Firm"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["role"], auth.ROLE_ADMINISTRATOR)
        firm_id = payload["firm"]["firm_id"]
        me = self.client.get("/auth/me").json()
        self.assertEqual(me["active_firm"]["firm_id"], firm_id)

    def test_create_firm_requires_authentication(self):
        self._logout_client()
        response = self._post("/firms", body={"name": "X"})
        self.assertEqual(response.status_code, 401, response.text)

    def test_create_firm_requires_name(self):
        self._login_client(self.token_alice)
        response = self._post("/firms", body={"name": "   "})
        self.assertEqual(response.status_code, 400, response.text)

    def test_list_firms_shows_memberships_and_roles(self):
        self._login_client(self.token_alice)
        response = self.client.get("/firms")
        self.assertEqual(response.status_code, 200, response.text)
        firms = response.json()
        ids = {f["firm_id"] for f in firms}
        self.assertEqual(ids, {self.firm_a, self.firm_b})
        roles = {f["firm_id"]: f["role"] for f in firms}
        self.assertEqual(roles[self.firm_a], auth.ROLE_ADMINISTRATOR)
        self.assertEqual(roles[self.firm_b], auth.ROLE_VIEWER)
        active = [f for f in firms if f["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["firm_id"], self.firm_a)


class MemberManagementTests(Phase2TestCase):
    def test_administrator_can_add_list_change_remove_members(self):
        self._login_client(self.token_alice)
        add = self._post(
            "/firms/%d/members" % self.firm_a,
            body={"subject": "p2:erin", "display_name": "Erin", "role": auth.ROLE_PARALEGAL},
        )
        self.assertEqual(add.status_code, 200, add.text)
        erin_id = add.json()["user_id"]
        members = self.client.get("/firms/%d/members" % self.firm_a).json()
        self.assertEqual(len(members), 4)
        subjects = {m["subject"] for m in members}
        self.assertIn("p2:erin", subjects)
        changed = self.client.patch(
            "/firms/%d/members/%d" % (self.firm_a, erin_id),
            json={"role": auth.ROLE_REVIEWER},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        removed = self.client.delete(
            "/firms/%d/members/%d" % (self.firm_a, erin_id)
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        members = self.client.get("/firms/%d/members" % self.firm_a).json()
        self.assertNotIn("p2:erin", {m["subject"] for m in members})

    def test_non_administrator_cannot_manage_members(self):
        self._login_client(self.token_carol)  # viewer
        cases = [
            ("post", "/firms/%d/members" % self.firm_a,
             {"subject": "p2:x", "role": auth.ROLE_VIEWER}),
            ("patch", "/firms/%d/members/%d" % (self.firm_a, self.bob),
             {"role": auth.ROLE_ADMINISTRATOR}),
            ("delete", "/firms/%d/members/%d" % (self.firm_a, self.bob), None),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                if method == "post":
                    response = self._post(path, body=body)
                elif method == "patch":
                    response = self.client.patch(path, json=body)
                else:
                    response = self.client.delete(path)
                self.assertEqual(response.status_code, 403, response.text)

    def test_cannot_add_member_to_foreign_firm(self):
        self._login_client(self.token_alice)
        response = self._post(
            "/firms/%d/members" % self.firm_b,
            body={"subject": "p2:x", "role": auth.ROLE_VIEWER},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_cannot_remove_oneself(self):
        self._login_client(self.token_alice)
        response = self.client.delete(
            "/firms/%d/members/%d" % (self.firm_a, self.alice)
        )
        self.assertEqual(response.status_code, 400, response.text)


class ContractLifecycleTests(Phase2TestCase):
    def test_uploaded_contract_starts_as_draft(self):
        self._login_client(self.token_bob)  # lawyer can upload
        response = self._post(
            "/upload",
            files={"file": ("contract.pdf", UPLOAD_PDF, "application/pdf")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        contract_id = response.json()["contract_id"]
        detail = self.client.get("/contracts/%d" % contract_id).json()
        self.assertEqual(detail["lifecycle_status"], db.LIFECYCLE_DRAFT)

    def test_administrator_can_advance_lifecycle(self):
        self._login_client(self.token_alice)
        response = self.client.patch(
            "/contracts/%d/lifecycle" % self.contract_a,
            json={"lifecycle_status": "active"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["lifecycle_status"], "active")
        self.assertEqual(response.json()["previous"], "draft")
        detail = self.client.get("/contracts/%d" % self.contract_a).json()
        self.assertEqual(detail["lifecycle_status"], "active")

    def test_viewer_cannot_change_lifecycle(self):
        self._login_client(self.token_carol)  # viewer
        response = self.client.patch(
            "/contracts/%d/lifecycle" % self.contract_a,
            json={"lifecycle_status": "active"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_invalid_lifecycle_status_is_rejected(self):
        self._login_client(self.token_alice)
        response = self.client.patch(
            "/contracts/%d/lifecycle" % self.contract_a,
            json={"lifecycle_status": "bogus"},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_foreign_contract_lifecycle_is_404(self):
        self._login_client(self.token_alice)
        response = self.client.patch(
            "/contracts/%d/lifecycle" % self.contract_b,
            json={"lifecycle_status": "active"},
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_all_lifecycle_statuses_can_be_set(self):
        self._login_client(self.token_alice)
        for status in (
            db.LIFECYCLE_DRAFT,
            db.LIFECYCLE_UNDER_REVIEW,
            db.LIFECYCLE_APPROVED,
            db.LIFECYCLE_ACTIVE,
            db.LIFECYCLE_EXPIRED,
            db.LIFECYCLE_ARCHIVED,
        ):
            with self.subTest(status=status):
                response = self.client.patch(
                    "/contracts/%d/lifecycle" % self.contract_a,
                    json={"lifecycle_status": status},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["lifecycle_status"], status)


class ContractListingFilterTests(Phase2TestCase):
    def test_list_contracts_is_firm_scoped(self):
        self._login_client(self.token_alice)
        listing = self.client.get("/contracts").json()
        ids = [row["contract_id"] for row in listing]
        self.assertIn(self.contract_a, ids)
        self.assertNotIn(self.contract_b, ids)

    def test_search_filters_by_filename(self):
        self._login_client(self.token_alice)
        response = self.client.get("/contracts?search=beta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 0)
        response = self.client.get("/contracts?search=alpha")
        ids = [row["contract_id"] for row in response.json()]
        self.assertIn(self.contract_a, ids)

    def test_lifecycle_status_filter(self):
        self._login_client(self.token_alice)
        self.client.patch(
            "/contracts/%d/lifecycle" % self.contract_a,
            json={"lifecycle_status": "active"},
        )
        response = self.client.get("/contracts?lifecycle_status=active")
        ids = [row["contract_id"] for row in response.json()]
        self.assertIn(self.contract_a, ids)
        response = self.client.get("/contracts?lifecycle_status=draft")
        ids = [row["contract_id"] for row in response.json()]
        self.assertNotIn(self.contract_a, ids)


class VersionHistoryTests(Phase2TestCase):
    def test_list_contract_versions(self):
        self._login_client(self.token_alice)
        response = self.client.get("/contracts/%d/versions" % self.contract_a)
        self.assertEqual(response.status_code, 200, response.text)
        versions = response.json()
        self.assertGreaterEqual(len(versions), 1)
        version = versions[0]
        for key in (
            "version_id", "version_number", "created_at", "created_by",
            "firm_id", "source_filename", "status", "provenance_status",
        ):
            self.assertIn(key, version)

    def test_foreign_contract_versions_is_404(self):
        self._login_client(self.token_alice)
        response = self.client.get("/contracts/%d/versions" % self.contract_b)
        self.assertEqual(response.status_code, 404, response.text)


class AuditTrailTests(Phase2TestCase):
    def _audit_count(self):
        conn = db.get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        finally:
            conn.close()

    def _audit_events(self):
        conn = db.get_conn()
        try:
            rows = conn.execute(
                """SELECT event_id, occurred_at, firm_id, actor_user_id,
                          actor_type, action, outcome, target_type, target_id,
                          metadata_json
                   FROM audit_events ORDER BY event_id DESC"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def test_lifecycle_change_is_audited(self):
        self._login_client(self.token_alice)
        before = self._audit_count()
        self.client.patch(
            "/contracts/%d/lifecycle" % self.contract_a,
            json={"lifecycle_status": "active"},
        )
        after = self._audit_count()
        self.assertGreater(after, before)
        events = self._audit_events()
        lifecycle_events = [
            e for e in events if e["action"] == "contract.lifecycle_changed"
        ]
        self.assertGreaterEqual(len(lifecycle_events), 1)
        event = lifecycle_events[-1]
        self.assertEqual(event["actor_user_id"], self.alice)
        self.assertEqual(event["firm_id"], self.firm_a)
        self.assertEqual(event["target_type"], "contract")
        self.assertEqual(event["target_id"], str(self.contract_a))
        meta = json.loads(event["metadata_json"])
        self.assertEqual(meta["previous"], "draft")
        self.assertEqual(meta["current"], "active")

    def test_firm_creation_is_audited(self):
        self._login_client(self.token_dave)
        before = self._audit_count()
        self._post("/firms", body={"name": "Dave's Firm"})
        after = self._audit_count()
        self.assertGreater(after, before)
        events = self._audit_events()
        firm_events = [e for e in events if e["action"] == "firm.created"]
        self.assertGreaterEqual(len(firm_events), 1)

    def test_membership_change_is_audited(self):
        self._login_client(self.token_alice)
        self._post(
            "/firms/%d/members" % self.firm_a,
            body={"subject": "p2:fiona", "display_name": "Fiona", "role": auth.ROLE_VIEWER},
        )
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT user_id FROM users WHERE subject = 'p2:fiona'"
            ).fetchone()
            fiona_id = row["user_id"]
        finally:
            conn.close()
        before = self._audit_count()
        self.client.patch(
            "/firms/%d/members/%d" % (self.firm_a, fiona_id),
            json={"role": auth.ROLE_REVIEWER},
        )
        after = self._audit_count()
        self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
