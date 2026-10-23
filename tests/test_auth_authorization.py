"""Phase 4 authentication and firm-level authorization tests.

Every fixture is synthetic and every database is a throwaway file under a
temporary directory.  The project's demo.db, app.db, and uploads directory are
never opened or modified.

Run:
    py -3.11 -m unittest discover -s tests -p "test_auth_authorization.py" -v
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Never let a test reach a real LLM provider, whatever local .env says.
os.environ["LLM_PROVIDER"] = "mock"

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


FIRM_A_CLAUSES = [
    clause(
        "1",
        "1. Payment Period. The Customer shall pay each undisputed Invoice "
        "within 30 days. Alpha-firm payment wording.",
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
        "3. Confidentiality. Alpha-firm-only secret wording.",
        "Confidentiality",
        "confidentiality",
    ),
]

FIRM_B_CLAUSES = [
    clause(
        "1",
        "1. Payment Period. The Customer shall pay each undisputed Invoice "
        "within 45 days. Beta-firm payment wording.",
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
        "3. Confidentiality. Beta-firm-only secret wording.",
        "Confidentiality",
        "confidentiality",
    ),
]

DEMO_CLAUSES = [
    clause(
        "1",
        "1. Payment Period. The synthetic demo contract says 30 days. "
        "Demo-only wording.",
        "Payment Period",
        "payment",
    ),
    clause(
        "2",
        "2. Default. The synthetic demo contract has a default clause.",
        "Default",
        "default",
    ),
]


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="auth_scope_")
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
        self.firm_c = auth.create_firm("Gamma Firm")

        self.alice = self._user("alice", "Alice Administrator")
        self.bob = self._user("bob", "Bob Beta Lawyer")
        self.carol = self._user("carol", "Carol Viewer")
        self.dave = self._user("dave", "Dave Reviewer")
        self.erin = self._user("erin", "Erin Paralegal")
        self.frank = self._user("frank", "Frank Multi Firm")
        self.nina = self._user("nina", "Nina No Membership")

        auth.add_membership(self.firm_a, self.alice, auth.ROLE_ADMINISTRATOR)
        auth.add_membership(self.firm_b, self.bob, auth.ROLE_LAWYER)
        auth.add_membership(self.firm_a, self.carol, auth.ROLE_VIEWER)
        auth.add_membership(self.firm_a, self.dave, auth.ROLE_REVIEWER)
        auth.add_membership(self.firm_a, self.erin, auth.ROLE_PARALEGAL)
        auth.add_membership(self.firm_a, self.frank, auth.ROLE_LAWYER)
        auth.add_membership(self.firm_b, self.frank, auth.ROLE_VIEWER)

        self.contract_a = self._make_contract("alpha.pdf", self.firm_a, FIRM_A_CLAUSES)
        self.contract_a2 = self._make_contract(
            "alpha-two.pdf", self.firm_a, FIRM_A_CLAUSES
        )
        self.contract_b = self._make_contract("beta.pdf", self.firm_b, FIRM_B_CLAUSES)
        self.demo_contract = self._make_contract(
            "demo.pdf", None, DEMO_CLAUSES
        )

        self.token_alice = self._session(self.alice, self.firm_a)
        self.token_bob = self._session(self.bob, self.firm_b)
        self.token_carol = self._session(self.carol, self.firm_a)
        self.token_dave = self._session(self.dave, self.firm_a)
        self.token_erin = self._session(self.erin, self.firm_a)
        self.token_frank = self._session(self.frank, self.firm_a)
        self.token_nina = self._session(self.nina, None)

        self.application = create_app(
            mode="upload",
            database_path=self.db_path,
            upload_dir=self.upload_dir,
        )
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)

    def _user(self, subject, display_name):
        return auth.create_user("test:%s" % subject, display_name)

    def _session(self, user_id, firm_id):
        return auth.create_session(user_id, firm_id, ttl_seconds=3600)

    def _make_contract(self, filename, firm_id, clauses):
        raw_text = "\n\n".join(item["clause_text"] for item in clauses)
        contract_id = adapter.create_contract(
            filename, raw_text, status="extracted", firm_id=firm_id
        )
        adapter.save_clauses(contract_id, clauses)
        graph.build_graph(contract_id, use_llm=False)
        return contract_id

    def _login(self, token):
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, token)

    def _logout_client(self):
        self.client.cookies.clear()

    def _impact(self, contract_id, clause_id="1", old=30, new=15, **extra):
        body = {
            "contract_id": contract_id,
            "clause_id": clause_id,
            "old_value": old,
            "new_value": new,
        }
        body.update(extra)
        return self.client.post("/impact", json=body)

    def _assert_foreign_contract_is_404(self, user_token, contract_id):
        self._login(user_token)
        for path in (
            "/contracts/%d" % contract_id,
            "/contracts/%d/clauses" % contract_id,
            "/graph/%d" % contract_id,
            "/clauses/%d" % contract_id,
            "/demo/%d/suggest" % contract_id,
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn("secret wording", response.text.lower())


class UnauthenticatedAccessTests(AuthTestCase):
    def test_protected_endpoints_reject_anonymous_requests(self):
        self._logout_client()
        cases = (
            ("GET", "/contracts", None),
            ("GET", "/contracts/%d" % self.contract_a, None),
            ("GET", "/contracts/%d/clauses" % self.contract_a, None),
            (
                "GET",
                "/contracts/%d/clauses/1/explanation" % self.contract_a,
                None,
            ),
            ("GET", "/graph/%d" % self.contract_a, None),
            ("GET", "/clauses/%d" % self.contract_a, None),
            (
                "POST",
                "/impact",
                {
                    "contract_id": self.contract_a,
                    "clause_id": "1",
                    "old_value": 30,
                    "new_value": 15,
                },
            ),
            ("GET", "/compare/%d/%d" % (self.contract_a, self.contract_a2), None),
            ("GET", "/demo/%d/suggest" % self.contract_a, None),
            ("GET", "/auth/me", None),
            ("POST", "/auth/firm", {"firm_id": self.firm_a}),
            ("POST", "/auth/logout", None),
        )
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                if method == "GET":
                    response = self.client.get(path)
                else:
                    response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 401, response.text)

    def test_upload_rejects_anonymous_requests(self):
        self._logout_client()
        response = self.client.post(
            "/upload",
            files={"file": ("contract.pdf", UPLOAD_PDF, "application/pdf")},
        )
        self.assertEqual(response.status_code, 401)

    def test_health_and_html_shells_are_public(self):
        self._logout_client()
        self.assertEqual(self.client.get("/health").status_code, 200)
        for path in ("/", "/impact-analysis", "/explain", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


class AuthorizedAccessTests(AuthTestCase):
    def test_firm_member_reads_only_its_own_contracts(self):
        self._login(self.token_alice)

        listing = self.client.get("/contracts")
        self.assertEqual(listing.status_code, 200, listing.text)
        ids = [row["contract_id"] for row in listing.json()]
        self.assertIn(self.contract_a, ids)
        self.assertIn(self.contract_a2, ids)
        self.assertNotIn(self.contract_b, ids)
        self.assertNotIn(self.demo_contract, ids)

        detail = self.client.get("/contracts/%d" % self.contract_a)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Alpha-firm payment wording", detail.json()["raw_text"])
        self.assertNotIn("Beta-firm payment wording", detail.json()["raw_text"])

        for path in (
            "/contracts/%d/clauses" % self.contract_a,
            "/contracts/%d/clauses/1/explanation" % self.contract_a,
            "/graph/%d" % self.contract_a,
            "/clauses/%d" % self.contract_a,
            "/demo/%d/suggest" % self.contract_a,
            "/compare/%d/%d" % (self.contract_a, self.contract_a2),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)

        impact = self._impact(self.contract_a, "1", 30, 15)
        self.assertEqual(impact.status_code, 200, impact.text)
        self.assertEqual(impact.json()["changed_clause"]["sec_no"], "1")

    def test_upload_is_assigned_to_the_session_firm_not_the_client(self):
        self._login(self.token_alice)
        response = self.client.post(
            "/upload",
            files={"file": ("contract.pdf", UPLOAD_PDF, "application/pdf")},
            data={"firm_id": str(self.firm_b)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        contract_id = response.json()["contract_id"]

        self.assertIsNotNone(db.get_contract_for_firm(contract_id, self.firm_a))
        self.assertIsNone(db.get_contract_for_firm(contract_id, self.firm_b))

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT firm_id FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["firm_id"], self.firm_a)

    def test_all_roles_can_read(self):
        for token in (
            self.token_alice,
            self.token_frank,
            self.token_carol,
            self.token_dave,
            self.token_erin,
        ):
            with self.subTest(token=token[:6]):
                self._login(token)
                self.assertEqual(self.client.get("/contracts").status_code, 200)
                self.assertEqual(
                    self.client.get("/contracts/%d" % self.contract_a).status_code,
                    200,
                )


class CrossFirmAccessTests(AuthTestCase):
    def test_foreign_contract_and_nested_routes_are_404(self):
        self._assert_foreign_contract_is_404(self.token_alice, self.contract_b)
        self._assert_foreign_contract_is_404(self.token_bob, self.contract_a)

    def test_foreign_contract_cannot_be_analyzed_or_compared(self):
        self._login(self.token_alice)
        impact = self._impact(self.contract_b, "1", 45, 15)
        self.assertEqual(impact.status_code, 404, impact.text)

        comparison = self.client.get(
            "/compare/%d/%d" % (self.contract_a, self.contract_b)
        )
        self.assertEqual(comparison.status_code, 404, comparison.text)

        reverse = self.client.get(
            "/compare/%d/%d" % (self.contract_b, self.contract_a)
        )
        self.assertEqual(reverse.status_code, 404, reverse.text)

    def test_foreign_clause_number_does_not_resolve_on_own_contract(self):
        self._login(self.token_alice)
        # Both contracts have section 1, but A's value is 30, not B's 45.
        mismatch = self._impact(self.contract_a, "1", 45, 15)
        self.assertEqual(mismatch.status_code, 400, mismatch.text)
        self.assertIn("does not match", mismatch.json()["detail"])

        valid = self._impact(self.contract_a, "1", 30, 15)
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertIn(
            "Alpha-firm payment wording",
            valid.json()["changed_clause"]["text"],
        )

    def test_client_supplied_firm_id_does_not_widen_scope(self):
        self._login(self.token_alice)
        listing = self.client.get("/contracts?firm_id=%d" % self.firm_b)
        self.assertEqual(listing.status_code, 200)
        ids = [row["contract_id"] for row in listing.json()]
        self.assertNotIn(self.contract_b, ids)
        self.assertIn(self.contract_a, ids)

        impact = self._impact(
            self.contract_b, "1", 45, 15, firm_id=self.firm_b
        )
        self.assertEqual(impact.status_code, 404, impact.text)

    def test_demo_contract_is_not_visible_to_firm_members(self):
        self._login(self.token_alice)
        self.assertEqual(
            self.client.get("/contracts/%d" % self.demo_contract).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/graph/%d" % self.demo_contract).status_code,
            404,
        )


class RoleRestrictionTests(AuthTestCase):
    def test_viewer_and_reviewer_cannot_upload(self):
        for token in (self.token_carol, self.token_dave):
            with self.subTest(token=token[:6]):
                self._login(token)
                response = self.client.post(
                    "/upload",
                    files={
                        "file": ("contract.pdf", UPLOAD_PDF, "application/pdf")
                    },
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertIn("role", response.json()["detail"].lower())

    def test_upload_roles_can_upload(self):
        for token in (self.token_alice, self.token_frank, self.token_erin):
            with self.subTest(token=token[:6]):
                self._login(token)
                response = self.client.post(
                    "/upload",
                    files={
                        "file": ("contract.pdf", UPLOAD_PDF, "application/pdf")
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                contract_id = response.json()["contract_id"]
                stored = db.get_contract_for_firm(contract_id, self.firm_a)
                self.assertIsNotNone(stored)
                self.assertEqual(stored["contract_id"], contract_id)

    def test_me_reports_role_and_upload_permission(self):
        self._login(self.token_carol)
        viewer = self.client.get("/auth/me").json()
        self.assertEqual(viewer["active_firm"]["role"], auth.ROLE_VIEWER)
        self.assertFalse(viewer["permissions"]["can_upload"])

        self._login(self.token_alice)
        admin = self.client.get("/auth/me").json()
        self.assertEqual(admin["active_firm"]["role"], auth.ROLE_ADMINISTRATOR)
        self.assertTrue(admin["permissions"]["can_upload"])


class FirmSwitchTests(AuthTestCase):
    def test_member_can_switch_between_authorized_firms(self):
        self._login(self.token_frank)
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_a).status_code, 200
        )
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_b).status_code, 404
        )

        switched = self.client.post(
            "/auth/firm", json={"firm_id": self.firm_b}
        )
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(switched.json()["active_firm"]["firm_id"], self.firm_b)
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_b).status_code, 200
        )
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_a).status_code, 404
        )

    def test_switching_to_a_non_member_firm_is_denied_and_keeps_scope(self):
        self._login(self.token_frank)
        self.client.post("/auth/firm", json={"firm_id": self.firm_b})

        denied = self.client.post("/auth/firm", json={"firm_id": self.firm_c})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIn("do not have access", denied.json()["detail"])

        me = self.client.get("/auth/me").json()
        self.assertEqual(me["active_firm"]["firm_id"], self.firm_b)
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_b).status_code, 200
        )
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_a).status_code, 404
        )

    def test_multi_firm_session_without_active_firm_requires_explicit_switch(self):
        token = auth.create_session(self.frank, None, ttl_seconds=3600)
        self._login(token)

        me = self.client.get("/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertIsNone(me.json()["active_firm"])
        self.assertEqual(len(me.json()["memberships"]), 2)
        self.assertEqual(self.client.get("/contracts").status_code, 403)

        switched = self.client.post("/auth/firm", json={"firm_id": self.firm_a})
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(self.client.get("/contracts").status_code, 200)

    def test_user_without_membership_cannot_access_or_switch(self):
        self._login(self.token_nina)
        me = self.client.get("/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["memberships"], [])
        self.assertIsNone(me.json()["active_firm"])
        self.assertEqual(self.client.get("/contracts").status_code, 403)

        denied = self.client.post("/auth/firm", json={"firm_id": self.firm_a})
        self.assertEqual(denied.status_code, 403)
        self.assertIsNone(self.client.get("/auth/me").json()["active_firm"])

    def test_removed_membership_is_revalidated_on_every_request(self):
        self._login(self.token_frank)
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_a).status_code, 200
        )

        conn = db.get_conn()
        try:
            conn.execute(
                "DELETE FROM firm_memberships WHERE user_id = ? AND firm_id = ?",
                (self.frank, self.firm_a),
            )
            conn.commit()
        finally:
            conn.close()

        # The old session no longer authorizes firm A.
        self.assertEqual(self.client.get("/contracts").status_code, 403)
        self.assertIsNone(self.client.get("/auth/me").json()["active_firm"])

        switched = self.client.post("/auth/firm", json={"firm_id": self.firm_b})
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(
            self.client.get("/contracts/%d" % self.contract_b).status_code, 200
        )


class SessionLifecycleTests(AuthTestCase):
    def test_invalid_and_revoked_sessions_are_rejected(self):
        self._login("not-a-real-session-token")
        self.assertEqual(self.client.get("/contracts").status_code, 401)

        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ?",
                (
                    "2026-01-01T00:00:00+00:00",
                    hashlib.sha256(
                        self.token_alice.encode("utf-8")
                    ).hexdigest(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self._login(self.token_alice)
        self.assertEqual(self.client.get("/contracts").status_code, 401)

    def test_expired_session_is_rejected(self):
        token = auth.create_session(self.alice, self.firm_a, ttl_seconds=60)
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                (
                    "2000-01-01T00:00:00+00:00",
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self._login(token)
        self.assertEqual(self.client.get("/contracts").status_code, 401)

    def test_logout_revokes_the_session(self):
        self._login(self.token_alice)
        logout = self.client.post("/auth/logout")
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertEqual(self.client.get("/contracts").status_code, 401)


class DemoIsolationTests(AuthTestCase):
    def _demo_client(self):
        return TestClient(
            create_app(mode="demo", database_path=self.db_path)
        )

    def test_demo_mode_sees_only_null_firm_rows_and_stays_read_only(self):
        client = self._demo_client()
        self.addCleanup(client.close)

        listing = client.get("/contracts")
        self.assertEqual(listing.status_code, 200)
        ids = [row["contract_id"] for row in listing.json()]
        self.assertEqual(ids, [self.demo_contract])

        self.assertEqual(
            client.get("/contracts/%d" % self.demo_contract).status_code, 200
        )
        self.assertEqual(
            client.get("/contracts/%d" % self.contract_a).status_code, 404
        )
        self.assertEqual(
            client.get("/graph/%d" % self.contract_a).status_code, 404
        )

        impact = client.post(
            "/impact",
            json={
                "contract_id": self.demo_contract,
                "clause_id": "1",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(impact.status_code, 200, impact.text)
        foreign = client.post(
            "/impact",
            json={
                "contract_id": self.contract_a,
                "clause_id": "1",
                "old_value": 30,
                "new_value": 15,
            },
        )
        self.assertEqual(foreign.status_code, 404)

        upload = client.post(
            "/upload",
            files={"file": ("contract.pdf", UPLOAD_PDF, "application/pdf")},
        )
        self.assertEqual(upload.status_code, 403)

    def test_demo_mode_ignores_an_authenticated_firm_cookie(self):
        client = self._demo_client()
        self.addCleanup(client.close)
        client.cookies.set(auth.SESSION_COOKIE_NAME, self.token_alice)

        listing = client.get("/contracts")
        self.assertEqual(listing.status_code, 200)
        ids = [row["contract_id"] for row in listing.json()]
        self.assertEqual(ids, [self.demo_contract])
        self.assertNotIn(self.contract_a, ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
