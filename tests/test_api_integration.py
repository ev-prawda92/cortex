import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

_temp_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(_temp_dir.name) / "api-test.db")
os.environ["CORTEX_AUTHZ_FAIL_CLOSED"] = "true"
os.environ["SEED_SAMPLE_AGENTS"] = "true"

from fastapi.testclient import TestClient
import cortex


class AuthorizationApiIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(cortex.app)
        cls.client = cls.client_context.__enter__()
        signup = cls.client.post("/api/auth/signup", json={
            "name": "Test Admin", "email": "admin@example.test",
            "password": "a sufficiently long test password",
        })
        cls.headers = {"Authorization": "Bearer " + signup.json()["token"]}
        cls.agent_id = cls.client.get("/api/agents", headers=cls.headers).json()["agents"][0]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        _temp_dir.cleanup()

    def _execution(self, approval_id=None):
        payload = {
            "agent_id": self.agent_id,
            "integration": "webhook",
            "action": "send",
            "params": {"case_id": "case-1"},
            "environment": "production",
            "data_scope": "assigned",
            "evidence": [{"type": "case_record", "current": True}],
        }
        if approval_id:
            payload["approval_id"] = approval_id
        return self.client.post("/api/integrations/execute", headers=self.headers, json=payload)

    def test_01_unconfigured_agent_fails_closed(self):
        response = self._execution()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["executed"])
        self.assertEqual(result["authorization"]["decision"], "BLOCK")

    def test_02_review_approval_and_replay_prevention(self):
        profile = {
            "status": "active", "default_decision": "BLOCK",
            "credentials": [{"name": "verified", "status": "valid",
                             "expires_at": "2027-09-01T00:00:00Z"}],
            "privileges": [{
                "action": "integration.webhook.send", "effect": "allow",
                "environments": ["production"], "data_scopes": ["assigned"],
                "target_systems": ["webhook"], "required_credentials": ["verified"],
                "min_evidence_items": 1, "required_evidence_types": ["case_record"],
                "requires_human_review": True,
            }],
        }
        configured = self.client.put(
            f"/api/agents/{self.agent_id}/authority", headers=self.headers, json=profile)
        self.assertEqual(configured.status_code, 200, configured.text)

        held = self._execution().json()
        self.assertEqual(held["authorization"]["decision"], "HUMAN_REVIEW")
        approval_id = held["authorization"]["approval_id"]
        approved = self.client.post(
            f"/api/approvals/{approval_id}", headers=self.headers,
            json={"decision": "approved", "note": "reviewed"})
        self.assertEqual(approved.status_code, 200, approved.text)

        with patch.object(cortex.integration_manager, "execute", return_value={"ok": True, "data": {}}) as execute:
            allowed = self._execution(approval_id).json()
            self.assertTrue(allowed["ok"])
            self.assertEqual(allowed["authorization"]["decision"], "ALLOW")
            execute.assert_called_once()

        with patch.object(cortex.integration_manager, "execute", return_value={"ok": True}) as execute:
            replay = self._execution(approval_id).json()
            self.assertEqual(replay["authorization"]["decision"], "HUMAN_REVIEW")
            execute.assert_not_called()

    def test_03_health_and_security_headers(self):
        live = self.client.get("/health/live")
        ready = self.client.get("/health/ready")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(live.headers["x-content-type-options"], "nosniff")
        self.assertEqual(live.headers["x-frame-options"], "DENY")

    def test_04_private_api_requires_authentication(self):
        response = self.client.get("/api/agents")
        self.assertEqual(response.status_code, 401)

    def test_05_agent_owner_isolation(self):
        created = self.client.post(
            "/api/agents/register", headers=self.headers,
            json={"name": "Admin Private Agent"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        private_id = created.json()["agent_id"]

        signup = self.client.post("/api/auth/signup", json={
            "name": "Other User", "email": "other@example.test",
            "password": "another sufficiently long password",
        })
        other_headers = {"Authorization": "Bearer " + signup.json()["token"]}
        listed = self.client.get("/api/agents", headers=other_headers).json()["agents"]
        self.assertNotIn(private_id, {agent["id"] for agent in listed})
        denied = self.client.get(f"/api/agents/{private_id}", headers=other_headers)
        self.assertEqual(denied.status_code, 403)
