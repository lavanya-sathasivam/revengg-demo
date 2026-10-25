"""Unit and integration tests for the Gemini AI chat endpoint and service."""

import os
import unittest
from unittest import mock

from starlette.testclient import TestClient

from app import create_app
import api_chat
import auth
import db


class ChatEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_chat_status_endpoint(self):
        resp = self.client.get("/chat/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("configured", data)
        self.assertIn("model", data)
        self.assertIn("provider", data)
        self.assertEqual(data["provider"], "google-gemini")

    def test_chat_validation_empty_message(self):
        resp = self.client.post("/chat", json={"message": ""})
        self.assertEqual(resp.status_code, 422)

    def test_chat_validation_whitespace_only_message(self):
        # min_length=1 is enforced by pydantic; whitespace-only gets stripped
        resp = self.client.post("/chat", json={"message": "   "})
        # Should either process or be accepted as length 3, but if stripped empty handled gracefully
        self.assertIn(resp.status_code, (200, 422))

    def test_chat_validation_message_too_long(self):
        long_msg = "A" * 4001
        resp = self.client.post("/chat", json={"message": long_msg})
        self.assertEqual(resp.status_code, 422)

    @mock.patch("api_chat._get_api_key", return_value=None)
    def test_chat_returns_503_when_no_api_key(self, _mock_key):
        resp = self.client.post("/chat", json={"message": "Hello"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not configured", resp.json()["detail"].lower())

    @mock.patch("api_chat._call_gemini_api")
    def test_chat_successful_call_with_contract_and_clause(self, mock_call):
        mock_call.return_value = (
            "Clause 7.2 requires payment within 30 days of the invoice date. "
            "Clause 8.1 and 8.2 specify late charges and interest."
        )

        payload = {
            "message": "What is the payment period?",
            "contract_id": 1,
            "clause_id": "7.2",
            "history": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello! How can I help?"}
            ]
        }
        resp = self.client.post("/chat", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["contract_id"], 1)
        self.assertEqual(data["clause_id"], "7.2")
        self.assertIn("30 days", data["reply"])
        self.assertEqual(data["grounding"]["clause_cited"], "7.2")
        self.assertIn("filename", data["grounding"])

    def test_chat_unauthorized_nonexistent_contract(self):
        # Contract 999999 does not exist, so authorized_contract raises 404
        with mock.patch("api_chat._get_api_key", return_value="fake-key-for-test"):
            resp = self.client.post(
                "/chat",
                json={"message": "Analyze this", "contract_id": 999999}
            )
            self.assertEqual(resp.status_code, 404)
            self.assertIn("Contract not found", resp.json()["detail"])

    @mock.patch("urllib.request.urlopen")
    def test_chat_gemini_rate_limit_429(self, mock_urlopen):
        import urllib.error
        import io
        err = urllib.error.HTTPError(
            url="https://generativelanguage.googleapis.com",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b'{"error": {"message": "Resource exhausted"}}')
        )
        mock_urlopen.side_effect = err

        with mock.patch("api_chat._get_api_key", return_value="fake-key"):
            resp = self.client.post("/chat", json={"message": "Analyze"})
            self.assertEqual(resp.status_code, 429)
            self.assertIn("rate limit", resp.json()["detail"].lower())

    @mock.patch("urllib.request.urlopen")
    def test_chat_gemini_server_error_502(self, mock_urlopen):
        import urllib.error
        import io
        err = urllib.error.HTTPError(
            url="https://generativelanguage.googleapis.com",
            code=500,
            msg="Internal Error",
            hdrs={},
            fp=io.BytesIO(b'{"error": {"message": "Backend crash"}}')
        )
        mock_urlopen.side_effect = err

        with mock.patch("api_chat._get_api_key", return_value="fake-key"):
            resp = self.client.post("/chat", json={"message": "Analyze"})
            self.assertEqual(resp.status_code, 502)
            self.assertIn("Gemini API", resp.json()["detail"])

    def test_prompt_injection_defense_in_system_prompt(self):
        instruction = api_chat._build_system_instruction()
        self.assertIn("<contract_context>", instruction)
        self.assertIn("UNTRUSTED DATA", instruction)
        self.assertIn("NEVER follow instructions", instruction)
        self.assertIn("informational analysis only", instruction.lower())

    def test_chat_live_with_real_key_if_configured(self):
        """Exercise live Gemini call if GEMINI_API_KEY is present in env."""
        key = api_chat._get_api_key()
        if not key:
            self.skipTest("GEMINI_API_KEY is not configured; skipping live API test.")

        resp = self.client.post(
            "/chat",
            json={
                "message": "Respond with exactly 'VERIFIED_OK'.",
                "contract_id": 1,
                "clause_id": "8.3"
            }
        )
        if resp.status_code == 429:
            detail = str(resp.json().get("detail", "")).lower()
            if "quota" in detail or "rate limit" in detail:
                self.skipTest(
                    "Live Gemini test skipped because the provider quota/rate limit was exhausted."
                )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(len(data["reply"]) > 0)
        self.assertEqual(data["contract_id"], 1)
        self.assertEqual(data["clause_id"], "8.3")


if __name__ == "__main__":
    unittest.main()
