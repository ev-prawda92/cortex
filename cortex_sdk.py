#!/usr/bin/env python3
"""
Cortex SDK & CLI — Developer Interface
═══════════════════════════════════════
A Python SDK + command-line tool for interacting with Cortex.

SDK Usage:
    from cortex_sdk import CortexClient
    client = CortexClient("http://localhost:3000", api_key="ctx_...")
    agents = client.list_agents()
    result = client.run_agent("my-agent", "Analyze this data")

CLI Usage:
    cortex login http://localhost:3000
    cortex agents list
    cortex agents run my-agent "What's the latest?"
    cortex agents create --name "My Agent" --description "Does stuff"
    cortex runs list my-agent
    cortex status
    cortex config set my-agent temperature 0.5

Install:
    pip install cortex-sdk  (or just copy this file)
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Optional, Dict, List, Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

__version__ = "0.1.0"

# ═══════════════════════════════════════════════════════════════
#  SDK CLIENT
# ═══════════════════════════════════════════════════════════════

class CortexClient:
    """Python SDK for the Cortex Agent Operations Platform."""

    def __init__(self, base_url: str = None, api_key: str = None, token: str = None):
        self.base_url = (base_url or os.environ.get("CORTEX_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("CORTEX_API_KEY", "")
        self.token = token or os.environ.get("CORTEX_TOKEN", "")
        if not self.base_url:
            # Try config file
            cfg = self._load_config()
            self.base_url = cfg.get("url", "http://localhost:3000")
            if not self.api_key:
                self.api_key = cfg.get("api_key", "")
            if not self.token:
                self.token = cfg.get("token", "")

    def _load_config(self) -> dict:
        path = os.path.expanduser("~/.cortex/config.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}

    def _save_config(self, data: dict):
        path = os.path.expanduser("~/.cortex/config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _request(self, method: str, path: str, data: dict = None, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                return json.loads(err_body)
            except Exception:
                raise Exception(f"HTTP {e.code}: {err_body[:500]}")

    # ── Authentication ──

    def login(self, email: str, password: str) -> dict:
        """Login and store the session token."""
        resp = self._request("POST", "/api/login", {"email": email, "password": password})
        if resp.get("ok") and resp.get("token"):
            self.token = resp["token"]
            self._save_config({"url": self.base_url, "token": self.token, "email": email})
        return resp

    def signup(self, name: str, email: str, password: str) -> dict:
        """Create a new account."""
        return self._request("POST", "/api/signup", {"name": name, "email": email, "password": password})

    # ── Agents ──

    def list_agents(self) -> List[dict]:
        """List all agents."""
        return self._request("GET", "/api/agents")

    def get_agent(self, agent_id: str) -> dict:
        """Get agent details."""
        return self._request("GET", f"/api/agents/{agent_id}")

    def create_agent(self, name: str, description: str = "", endpoint_type: str = "embedded",
                     endpoint_url: str = "", config: dict = None) -> dict:
        """Register a new agent."""
        return self._request("POST", "/api/agents/register", {
            "name": name, "description": description,
            "endpoint_type": endpoint_type, "endpoint_url": endpoint_url,
            "config": config or {},
        })

    def update_config(self, agent_id: str, config: dict) -> dict:
        """Update an agent's full config."""
        return self._request("PUT", f"/api/agents/{agent_id}/config", {"config": config})

    def delete_agent(self, agent_id: str) -> dict:
        """Delete an agent."""
        return self._request("DELETE", f"/api/agents/{agent_id}")

    def control_agent(self, agent_id: str, action: str) -> dict:
        """Start, stop, pause, or resume an agent."""
        return self._request("POST", f"/api/agents/{agent_id}/control", params={"action": action})

    def set_standing_instruction(self, agent_id: str, instruction: str, interval: int = 60) -> dict:
        """Set an agent's standing instruction for continuous mode."""
        return self._request("POST", f"/api/agents/{agent_id}/standing-instruction", {
            "standing_instruction": instruction, "run_interval_seconds": interval,
        })

    # ── Runs ──

    def run_agent(self, agent_id: str, claim: str) -> dict:
        """Run an agent with the given input."""
        return self._request("POST", f"/api/agents/{agent_id}/run", {"claim": claim})

    def list_runs(self, agent_id: str) -> List[dict]:
        """List runs for an agent."""
        return self._request("GET", f"/api/agents/{agent_id}/runs")

    # ── CAR (Adaptive Runtime) ──

    def health(self) -> dict:
        """Get CAR health status."""
        return self._request("GET", "/api/car/health")

    def fingerprint(self, agent_id: str) -> dict:
        """Get behavioral fingerprint for an agent."""
        return self._request("GET", f"/api/car/fingerprint/{agent_id}")

    def route(self, agent_id: str, task_type: str, providers: list = None, optimize: str = "balanced") -> dict:
        """Get adaptive routing recommendation."""
        return self._request("POST", "/api/car/route", {
            "agent_id": agent_id, "task_type": task_type,
            "providers": providers or [], "optimize": optimize,
        })

    def predict(self, agent_id: str, task_type: str, provider: str, model: str) -> dict:
        """Get run prediction."""
        return self._request("POST", "/api/car/predict", {
            "agent_id": agent_id, "task_type": task_type,
            "provider": provider, "model": model,
        })

    def pressure(self) -> dict:
        """Get fleet-wide drift pressure."""
        return self._request("GET", "/api/car/pressure")

    # ── Daemon ──

    def daemon_status(self) -> dict:
        """Get daemon status."""
        return self._request("GET", "/api/daemon/status")

    def agent_daemon_state(self, agent_id: str) -> dict:
        """Get daemon state for an agent."""
        return self._request("GET", f"/api/agents/{agent_id}/daemon")

    # ── Integrations ──

    def list_integration_types(self) -> list:
        """List available integration types."""
        return self._request("GET", "/api/integrations/types")

    def add_integration(self, agent_id: str, name: str, integration_type: str, config: dict) -> dict:
        """Add an integration to an agent."""
        return self._request("POST", f"/api/integrations/{agent_id}", {
            "name": name, "type": integration_type, "config": config,
        })

    def execute_integration(self, agent_id: str, name: str, action: str, params: dict = None) -> dict:
        """Execute an integration action."""
        return self._request("POST", f"/api/integrations/{agent_id}/{name}/execute", {
            "action": action, "params": params or {},
        })

    # ── Teams ──

    def list_teams(self) -> list:
        """List teams."""
        return self._request("GET", "/api/teams")

    def create_team(self, name: str, description: str = "") -> dict:
        """Create a team."""
        return self._request("POST", "/api/teams", {"name": name, "description": description})

    # ── Plugins ──

    def list_plugins(self) -> list:
        """List installed plugins."""
        return self._request("GET", "/api/plugins")

    def install_plugin(self, manifest_url: str) -> dict:
        """Install a plugin from a manifest URL."""
        return self._request("POST", "/api/plugins/install", {"manifest_url": manifest_url})

    # ── Usage ──

    def usage_summary(self, period: str = "30d") -> dict:
        """Get usage summary."""
        return self._request("GET", "/api/usage/summary", params={"period": period})

    # ── Propose & Apply config changes ──

    def propose_change(self, agent_id: str, request: str) -> dict:
        """Propose a plain-English config change."""
        return self._request("POST", f"/api/agents/{agent_id}/propose", {"request": request})

    def apply_change(self, agent_id: str, token: str) -> dict:
        """Apply a proposed change."""
        return self._request("POST", f"/api/agents/{agent_id}/apply", {"token": token, "approved_by": "sdk"})


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def _fmt_table(rows: List[dict], columns: List[str] = None):
    """Format a list of dicts as an aligned table."""
    if not rows:
        print("  (no results)")
        return
    if not columns:
        columns = list(rows[0].keys())
    # Calculate widths
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            val = str(row.get(c, ""))[:60]
            widths[c] = max(widths[c], len(val))
    # Header
    header = "  ".join(c.upper().ljust(widths[c]) for c in columns)
    print(f"  {header}")
    print(f"  {'  '.join('─' * widths[c] for c in columns)}")
    # Rows
    for row in rows:
        line = "  ".join(str(row.get(c, ""))[:60].ljust(widths[c]) for c in columns)
        print(f"  {line}")


def _fmt_json(data):
    print(json.dumps(data, indent=2))


def cli_main():
    """Main CLI entry point."""
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        return

    client = CortexClient()
    cmd = args[0]

    try:
        if cmd == "login":
            url = args[1] if len(args) > 1 else input("Cortex URL: ")
            email = args[2] if len(args) > 2 else input("Email: ")
            import getpass
            password = args[3] if len(args) > 3 else getpass.getpass("Password: ")
            client.base_url = url.rstrip("/")
            resp = client.login(email, password)
            if resp.get("ok"):
                print(f"✓ Logged in as {email}")
                print(f"  Config saved to ~/.cortex/config.json")
            else:
                print(f"✗ Login failed: {resp.get('detail', 'unknown error')}")

        elif cmd == "signup":
            url = args[1] if len(args) > 1 else input("Cortex URL: ")
            name = input("Name: ")
            email = input("Email: ")
            import getpass
            password = getpass.getpass("Password: ")
            client.base_url = url.rstrip("/")
            resp = client.signup(name, email, password)
            if resp.get("ok"):
                print(f"✓ Account created for {email}")
            else:
                print(f"✗ Signup failed: {resp.get('detail', 'unknown error')}")

        elif cmd == "agents":
            sub = args[1] if len(args) > 1 else "list"

            if sub == "list":
                agents = client.list_agents()
                _fmt_table(agents, ["id", "name", "status", "account", "live"])

            elif sub == "get":
                agent_id = args[2]
                _fmt_json(client.get_agent(agent_id))

            elif sub == "create":
                name = args[2] if len(args) > 2 else input("Agent name: ")
                desc = args[3] if len(args) > 3 else input("Description: ")
                resp = client.create_agent(name, desc)
                if resp.get("ok"):
                    print(f"✓ Created agent: {resp.get('agent_id', '')}")
                else:
                    print(f"✗ Failed: {resp.get('detail', '')}")

            elif sub == "run":
                agent_id = args[2]
                claim = " ".join(args[3:]) if len(args) > 3 else input("Input: ")
                print(f"Running {agent_id}...")
                resp = client.run_agent(agent_id, claim)
                if resp.get("ok"):
                    run = resp.get("run", {})
                    print(f"✓ {run.get('outcome', 'DONE')} — {run.get('steps_used', 0)} steps, {run.get('total_tokens', 0)} tokens")
                    detail = run.get("detail", {})
                    if detail.get("summary"):
                        print(f"\n{detail['summary']}")
                else:
                    print(f"✗ Error: {resp.get('error', 'unknown')}")

            elif sub in ("start", "stop", "pause", "resume"):
                agent_id = args[2]
                resp = client.control_agent(agent_id, sub)
                print(f"✓ {agent_id} → {resp.get('status', sub)}")

            elif sub == "instruct":
                agent_id = args[2]
                instruction = " ".join(args[3:]) if len(args) > 3 else input("Standing instruction: ")
                interval = 60
                resp = client.set_standing_instruction(agent_id, instruction, interval)
                print(f"✓ Standing instruction set for {agent_id}")

            elif sub == "delete":
                agent_id = args[2]
                resp = client.delete_agent(agent_id)
                print(f"✓ Deleted {agent_id}" if resp.get("ok") else f"✗ {resp}")

            elif sub == "daemon":
                agent_id = args[2]
                _fmt_json(client.agent_daemon_state(agent_id))

        elif cmd == "runs":
            agent_id = args[1] if len(args) > 1 else input("Agent ID: ")
            runs = client.list_runs(agent_id)
            if isinstance(runs, list):
                _fmt_table(runs[:20], ["id", "outcome", "steps_used", "total_tokens", "started_at"])
            else:
                _fmt_json(runs)

        elif cmd == "status":
            health = client.health()
            daemon = client.daemon_status()
            print("=== Cortex Status ===")
            print(f"  CAR Health: {health.get('status', '?')}")
            print(f"  Agents tracked: {health.get('agents_tracked', 0)}")
            print(f"  Total runs: {health.get('total_runs', 0)}")
            print(f"  Daemon running: {daemon.get('running', False)}")
            agents_state = daemon.get("agents", {})
            if agents_state:
                print(f"  Active agents: {len(agents_state)}")
                for aid, st in agents_state.items():
                    print(f"    {aid}: cycle #{st.get('cycle_count', 0)}, errors={st.get('consecutive_errors', 0)}")

        elif cmd == "config":
            sub = args[1] if len(args) > 1 else "get"
            agent_id = args[2] if len(args) > 2 else input("Agent ID: ")

            if sub == "get":
                agent = client.get_agent(agent_id)
                _fmt_json(agent.get("config", {}))

            elif sub == "set":
                request_text = " ".join(args[3:])
                resp = client.propose_change(agent_id, request_text)
                if resp.get("ok"):
                    print("Proposed changes:")
                    for c in resp.get("changes", []):
                        print(f"  {c['field']}: {c.get('from', '?')} → {c.get('to', '?')}")
                    yn = input("Apply? [y/N]: ")
                    if yn.lower() == "y":
                        apply_resp = client.apply_change(agent_id, resp["token"])
                        print(f"✓ Applied. Now v{apply_resp.get('new_version', '?')}")
                else:
                    print(f"No changes: {resp.get('message', '')}")

        elif cmd == "health":
            _fmt_json(client.health())

        elif cmd == "pressure":
            _fmt_json(client.pressure())

        elif cmd == "usage":
            period = args[1] if len(args) > 1 else "30d"
            _fmt_json(client.usage_summary(period))

        elif cmd == "plugins":
            sub = args[1] if len(args) > 1 else "list"
            if sub == "list":
                _fmt_json(client.list_plugins())
            elif sub == "install":
                url = args[2]
                _fmt_json(client.install_plugin(url))

        elif cmd == "teams":
            sub = args[1] if len(args) > 1 else "list"
            if sub == "list":
                _fmt_json(client.list_teams())
            elif sub == "create":
                name = args[2] if len(args) > 2 else input("Team name: ")
                _fmt_json(client.create_team(name))

        elif cmd == "version":
            print(f"Cortex SDK v{__version__}")

        else:
            print(f"Unknown command: {cmd}")
            print(HELP_TEXT)

    except IndexError:
        print("Missing required argument. Use --help for usage.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


HELP_TEXT = """
Cortex CLI — Agent Operations from the Terminal

SETUP
  cortex login <url>                    Login to a Cortex instance
  cortex signup <url>                   Create an account

AGENTS
  cortex agents list                    List all agents
  cortex agents get <id>                Get agent details
  cortex agents create <name> [desc]    Register a new agent
  cortex agents run <id> <input>        Run an agent (one-off)
  cortex agents start|stop|pause|resume <id>   Control agent
  cortex agents instruct <id> <text>    Set standing instruction
  cortex agents delete <id>             Delete an agent
  cortex agents daemon <id>             Show daemon state

RUNS
  cortex runs <agent_id>                List recent runs

CONFIG
  cortex config get <id>                Show agent config
  cortex config set <id> <change>       Propose + apply config change

MONITORING
  cortex status                         Platform health + daemon status
  cortex health                         CAR engine health
  cortex pressure                       Fleet-wide drift pressure
  cortex usage [period]                 Usage summary

PLUGINS & TEAMS
  cortex plugins list|install <url>     Manage plugins
  cortex teams list|create <name>       Manage teams

ENV VARS
  CORTEX_URL        Base URL (default: http://localhost:3000)
  CORTEX_API_KEY    API key (ctx_...)
  CORTEX_TOKEN      JWT session token

CONFIG FILE: ~/.cortex/config.json
"""


if __name__ == "__main__":
    cli_main()
