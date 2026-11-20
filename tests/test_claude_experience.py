"""Local, offline contract tests for the Cortex Experience Claude Code bridge."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx

import cortex_experience_client as client_mod
import cortex_experience_mcp as bridge
from scripts import setup_claude_code as installer


class ExperienceClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def handler(req):
            self.requests.append(req)
            path = req.url.path
            if path.endswith("/summary"):
                return httpx.Response(200, json={"memory_count": 1})
            if path.endswith("/memories") and req.method == "GET":
                return httpx.Response(200, json={
                    "total": 1,
                    "data": [{"id": "m1", "title": "Python API conventions",
                              "content": "Use Pydantic models", "status": "verified"}],
                })
            if path.endswith("/memories") and req.method == "POST":
                return httpx.Response(201, json={
                    "id": "m1", "status": "draft", **json.loads(req.content)
                })
            if path.endswith("/content"):
                return httpx.Response(200, json={
                    "id": "m1", "status": "draft", "training_opt_in": False,
                })
            if path.endswith("/memories/m1") and req.method == "PATCH":
                return httpx.Response(200, json={
                    "id": "m1", "status": "verified", "training_opt_in": False,
                })
            if path.endswith("/performance"):
                return httpx.Response(200, json={
                    "verdict": {"direction": "no_clear_change"},
                })
            if path.endswith("/datasets/preview"):
                return httpx.Response(200, json={"eligible": 0})
            return httpx.Response(404)

        self.client = client_mod.CortexExperienceClient(
            base_url="http://127.0.0.1:3000", api_key="ctx_test",
            transport=httpx.MockTransport(handler),
        )

    def test_connection_and_bearer_authentication(self):
        self.assertEqual(self.client.status()["memory_count"], 1)
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer ctx_test")
        self.assertEqual(self.requests[0].url.path, "/api/v1/experience/summary")

    def test_verified_only_recall_by_default(self):
        self.client.recall("Python API conventions", limit=100)
        self.assertEqual(self.requests[-1].url.params["status"], "verified")
        self.assertEqual(self.requests[-1].url.params["limit"], "20")
        self.client.recall("Python", verified_only=False)
        self.assertNotIn("status", self.requests[-1].url.params)

    def test_writes_default_to_draft_and_no_training(self):
        saved = self.client.remember(
            title="Typed APIs", content="Use Pydantic models.",
            kind="preference", tags=["python"],
        )
        self.assertEqual(saved["status"], "draft")
        self.assertFalse(json.loads(self.requests[-1].content)["training_opt_in"])
        self.assertFalse(self.client.review(
            "m1", verification_note="Reviewed against working implementation."
        )["training_opt_in"])
        self.assertEqual(self.client.edit(
            "m1", title="Typed APIs", content="Use typed APIs.",
            kind="preference", tags=["python"],
        )["status"], "draft")

    def test_other_read_methods(self):
        self.assertEqual(self.client.list_memories()["total"], 1)
        self.assertIn("verdict", self.client.performance("agent1"))
        self.assertEqual(self.client.dataset_preview()["eligible"], 0)

    def test_rejects_invalid_http_url_and_missing_api_key(self):
        with self.assertRaises(client_mod.CortexConnectionError):
            client_mod.normalize_base_url("http://example.com:3000")
        with self.assertRaises(client_mod.CortexConnectionError):
            client_mod.normalize_base_url("http://alice:pass@localhost:3000")
        with patch.object(client_mod, "load_credentials", return_value={}), patch.dict(
            os.environ, {"CORTEX_API_KEY": ""}, clear=False
        ):
            with self.assertRaises(client_mod.CortexConnectionError):
                client_mod.CortexExperienceClient(base_url="http://127.0.0.1:3000")

    def test_server_errors_do_not_leak_body(self):
        def deny(req):
            return httpx.Response(403, json={"detail": "internal secret in server"})
        self.client.transport = httpx.MockTransport(deny)
        with self.assertRaises(client_mod.CortexConnectionError) as err:
            self.client.status()
        self.assertNotIn("internal secret", str(err.exception))


class SetupTests(unittest.TestCase):
    def test_creds_file_owner_only_and_no_repo_file(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "credentials.json"
            with patch.object(installer, "credential_path", return_value=file):
                result = installer.save_credentials("http://localhost:3000", "ctx_test")
            self.assertEqual(result, file)
            self.assertEqual(json.loads(file.read_text())["api_key"], "ctx_test")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            with patch.object(client_mod, "credential_path", return_value=file):
                self.assertEqual(client_mod.load_credentials()["url"],
                                 "http://localhost:3000")

    def test_installer_register_command_user_scope(self):
        def command(args, **kwargs):
            if args[:3] == ["claude", "mcp", "get"]:
                return types.SimpleNamespace(returncode=1)
            return types.SimpleNamespace(returncode=0)
        with patch.object(installer.shutil, "which", return_value="/usr/local/bin/claude"), patch.object(
            installer.subprocess, "run", side_effect=command
        ) as run:
            result = installer.register_mcp()
            self.assertIn("user scope", result)
            args = run.call_args.args[0]
            self.assertEqual(args[:3], ["claude", "mcp", "add"])
            self.assertIn("--scope", args)
            self.assertIn("user", args)
            self.assertIn("cortex-experience", args)


class McpToolsTests(unittest.TestCase):
    def test_explicit_consent_required_on_mutating_tools(self):
        registry = {}
        class FakeFastMCP:
            def __init__(self, name):
                self.name = name
            def tool(self):
                def decorator(fn):
                    registry[fn.__name__] = fn
                    return fn
                return decorator
        mcp = types.ModuleType("mcp")
        server = types.ModuleType("mcp.server")
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = FakeFastMCP
        with patch.dict(sys.modules, {
            "mcp": mcp, "mcp.server": server, "mcp.server.fastmcp": fastmcp,
        }):
            bridge.create_server()
        # These must refuse a write before instantiating a client.
        with patch.object(bridge, "CortexExperienceClient") as api:
            no_save = json.loads(registry["cortex_remember"](
                "Title", "Content", user_confirmed=False
            ))
            no_verify = json.loads(registry["cortex_verify"](
                "m1", "A verification note", user_confirmed=False
            ))
            no_edit = json.loads(registry["cortex_edit"](
                "m1", "Title", "Content", "knowledge", [], user_confirmed=False
            ))
            self.assertFalse(no_save["ok"])
            self.assertFalse(no_verify["ok"])
            self.assertFalse(no_edit["ok"])
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
