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

    def test_06_builder_requires_authentication(self):
        self.assertEqual(self.client.get("/api/builder/starters").status_code, 401)
        self.assertEqual(self.client.post("/api/builder/create", json={
            "description": "Review the records I provide."}).status_code, 401)

    def test_07_reviewing_a_brief_does_not_create_an_agent(self):
        before = self.client.get("/api/agents", headers=self.headers).json()["agents"]
        response = self.client.post("/api/builder/draft", headers=self.headers,
                                    json={"description": "Review invoices and cite every price difference."})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["draft"]["status"], "stopped")
        after = self.client.get("/api/agents", headers=self.headers).json()["agents"]
        self.assertEqual(len(before), len(after))

    def test_08_plain_text_creation_preserves_instructions_and_stays_stopped(self):
        text = "Review invoices and cite every price difference. Draft requests for my approval."
        response = self.client.post("/api/builder/create", headers=self.headers, json={
            "name": "Plain text checker", "description": text, "starter_id": "invoice-check"})
        self.assertEqual(response.status_code, 200, response.text)
        agent = response.json()["agent"]
        self.assertEqual(agent["status"], "stopped")
        self.assertEqual(agent["endpoint"]["type"], "embedded")
        self.assertEqual(agent["config"]["standing_instruction"], text)
        self.assertEqual(agent["config"]["system_prompt"], text)
        self.assertEqual(agent["config"]["tools"], [])
        self.assertEqual(agent["config"]["data_sources"], [])
        self.assertTrue(agent["config"]["behavior"]["confirm_before_action"])
        self.assertEqual(agent["config"]["builder"]["setup_status"], "needs_connections")
        self.assertIn(text, cortex.build_system_prompt(agent))
        versions = self.client.get(f"/api/agents/{agent['id']}/versions", headers=self.headers)
        self.assertEqual(versions.status_code, 200)
        self.assertEqual(len(versions.json()["versions"]), 1)

    def test_09_preview_uses_current_text_without_tools_or_agent_writes(self):
        before = self.client.get("/api/agents", headers=self.headers).json()["agents"]
        result = {"ok": True, "final_text": "NW-104 differs by $7.", "input_tokens": 25, "output_tokens": 10}
        with patch.object(cortex, "get_key", return_value="test-only"), \
             patch.object(cortex.providers_mod, "run_tool_loop", return_value=result) as run:
            response = self.client.post("/api/builder/preview", headers=self.headers, json={
                "description": "Compare the supplied prices and cite each difference.",
                "sample_input": "NW-104: billed $147; agreed $140."})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["output"], result["final_text"])
        self.assertTrue(response.json()["preview"])
        self.assertEqual(run.call_args.kwargs["tools"], [])
        self.assertEqual(run.call_args.kwargs["max_iterations"], 1)
        self.assertEqual(run.call_args.kwargs["user_message"], "NW-104: billed $147; agreed $140.")
        self.assertIn("Compare the supplied prices", run.call_args.kwargs["system"])
        after = self.client.get("/api/agents", headers=self.headers).json()["agents"]
        self.assertEqual(len(before), len(after))

    def test_10_preview_without_a_model_explains_setup(self):
        with patch.object(cortex, "get_key", return_value=""), \
             patch.object(cortex.providers_mod, "run_tool_loop") as run:
            response = self.client.post("/api/builder/preview", headers=self.headers, json={
                "description": "Review the records I provide.", "sample_input": "Example record."})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Settings", response.json()["detail"])
        run.assert_not_called()

    def test_11_invalid_briefs_are_rejected(self):
        for body, status in [({"description": " " * 20}, 400),
                             ({"description": "Review the records I provide.", "starter_id": "unknown"}, 400),
                             ({"description": "short"}, 422)]:
            response = self.client.post("/api/builder/create", headers=self.headers, json=body)
            self.assertEqual(response.status_code, status, response.text)

    def test_12_model_failure_does_not_return_a_successful_preview(self):
        with patch.object(cortex, "get_key", return_value="test-only"), \
             patch.object(cortex.providers_mod, "run_tool_loop", return_value={"ok": False, "error": "bad key"}):
            response = self.client.post("/api/builder/preview", headers=self.headers, json={
                "description": "Review the records I provide.", "sample_input": "Example record."})
        self.assertEqual(response.status_code, 502)

