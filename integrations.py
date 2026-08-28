"""
CORTEX Integrations — Real External Service Connectors
═══════════════════════════════════════════════════════
Provides actual API integrations that agents can use as tools.

Supported integrations:
  - Slack: post messages, read channels, list channels, react
  - GitHub: list PRs, get PR details, comment on PRs, check CI status
  - Webhook: send/receive HTTP webhooks
  - REST API: generic REST client for any endpoint

Each integration is both:
  1. A configurable connection (stored in agent config)
  2. A set of tools agents can invoke during execution

Usage:
    from integrations import IntegrationManager
    mgr = IntegrationManager()
    mgr.register("slack", {"bot_token": "xoxb-..."})
    result = mgr.execute("slack", "post_message", {"channel": "#general", "text": "Hello"})
"""

import json
import os
import time
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


# ═══════════════════════════════════════════════════════════════
#  BASE INTEGRATION CLASS
# ═══════════════════════════════════════════════════════════════

class Integration:
    """Base class for all integrations."""
    name: str = "base"
    description: str = ""
    icon: str = "🔗"
    config_schema: dict = {}

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.connected = False
        self.last_used = None
        self.error_count = 0
        self.last_error = ""

    def connect(self) -> dict:
        """Test the connection. Returns {"ok": True/False, "detail": ...}"""
        return {"ok": False, "detail": "not implemented"}

    def execute(self, action: str, params: dict = None) -> dict:
        """Execute an action. Returns {"ok": True/False, "data": ..., "error": ...}"""
        self.last_used = datetime.now(timezone.utc).isoformat()
        method = getattr(self, f"action_{action}", None)
        if not method:
            return {"ok": False, "error": f"Unknown action: {action}. Available: {self.available_actions()}"}
        try:
            result = method(params or {})
            self.error_count = 0
            self.last_error = ""
            return result
        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)
            return {"ok": False, "error": str(e)}

    def available_actions(self) -> List[str]:
        return [m.replace("action_", "") for m in dir(self) if m.startswith("action_")]

    def status(self) -> dict:
        return {
            "name": self.name, "connected": self.connected,
            "last_used": self.last_used, "error_count": self.error_count,
            "last_error": self.last_error, "actions": self.available_actions(),
        }

    def _http(self, url, method="GET", headers=None, data=None, timeout=30):
        """Simple HTTP client using stdlib."""
        hdrs = headers or {}
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = Request(url, data=body, headers=hdrs, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"raw": raw}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise Exception(f"HTTP {e.code}: {body[:500]}")
        except URLError as e:
            raise Exception(f"Connection error: {e.reason}")


# ═══════════════════════════════════════════════════════════════
#  SLACK INTEGRATION
# ═══════════════════════════════════════════════════════════════

class SlackIntegration(Integration):
    """Real Slack Web API integration."""
    name = "slack"
    description = "Post messages, read channels, manage reactions in Slack workspaces"
    icon = "💬"
    config_schema = {
        "bot_token": {"type": "string", "description": "Slack Bot User OAuth Token (xoxb-...)", "required": True},
        "default_channel": {"type": "string", "description": "Default channel for messages", "required": False},
    }

    BASE = "https://slack.com/api"

    def _api(self, method, params=None, json_body=None):
        token = self.config.get("bot_token", "")
        if not token:
            raise Exception("No Slack bot_token configured")
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.BASE}/{method}"
        if json_body:
            headers["Content-Type"] = "application/json"
            return self._http(url, method="POST", headers=headers, data=json_body)
        elif params:
            url += "?" + urlencode(params)
        return self._http(url, headers=headers)

    def connect(self) -> dict:
        try:
            resp = self._api("auth.test")
            if resp.get("ok"):
                self.connected = True
                return {"ok": True, "detail": f"Connected as {resp.get('user', '?')} in {resp.get('team', '?')}",
                        "team": resp.get("team"), "user": resp.get("user")}
            return {"ok": False, "detail": resp.get("error", "auth failed")}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def action_post_message(self, params: dict) -> dict:
        """Post a message to a Slack channel."""
        channel = params.get("channel") or self.config.get("default_channel")
        text = params.get("text", "")
        if not channel:
            return {"ok": False, "error": "No channel specified"}
        if not text:
            return {"ok": False, "error": "No text specified"}
        body = {"channel": channel, "text": text}
        if params.get("thread_ts"):
            body["thread_ts"] = params["thread_ts"]
        if params.get("blocks"):
            body["blocks"] = params["blocks"]
        resp = self._api("chat.postMessage", json_body=body)
        if resp.get("ok"):
            return {"ok": True, "data": {"ts": resp.get("ts"), "channel": resp.get("channel")}}
        return {"ok": False, "error": resp.get("error", "post failed")}

    def action_read_channel(self, params: dict) -> dict:
        """Read recent messages from a Slack channel."""
        channel = params.get("channel") or self.config.get("default_channel")
        limit = min(int(params.get("limit", 20)), 100)
        if not channel:
            return {"ok": False, "error": "No channel specified"}
        resp = self._api("conversations.history", {"channel": channel, "limit": str(limit)})
        if resp.get("ok"):
            messages = []
            for m in resp.get("messages", []):
                messages.append({
                    "text": m.get("text", ""),
                    "user": m.get("user", ""),
                    "ts": m.get("ts", ""),
                    "type": m.get("subtype", "message"),
                })
            return {"ok": True, "data": {"messages": messages, "count": len(messages)}}
        return {"ok": False, "error": resp.get("error", "read failed")}

    def action_list_channels(self, params: dict) -> dict:
        """List available Slack channels."""
        limit = min(int(params.get("limit", 100)), 1000)
        resp = self._api("conversations.list", {
            "limit": str(limit),
            "types": params.get("types", "public_channel,private_channel"),
            "exclude_archived": "true",
        })
        if resp.get("ok"):
            channels = [{"id": c["id"], "name": c.get("name", ""), "topic": c.get("topic", {}).get("value", ""),
                          "members": c.get("num_members", 0)} for c in resp.get("channels", [])]
            return {"ok": True, "data": {"channels": channels, "count": len(channels)}}
        return {"ok": False, "error": resp.get("error", "list failed")}

    def action_add_reaction(self, params: dict) -> dict:
        """Add a reaction emoji to a message."""
        resp = self._api("reactions.add", json_body={
            "channel": params.get("channel", ""),
            "timestamp": params.get("ts", ""),
            "name": params.get("emoji", "thumbsup"),
        })
        return {"ok": resp.get("ok", False), "error": resp.get("error", "")}

    def action_search_messages(self, params: dict) -> dict:
        """Search for messages across channels."""
        query = params.get("query", "")
        if not query:
            return {"ok": False, "error": "No query specified"}
        # Note: search requires a user token, not bot token in most cases
        resp = self._api("search.messages", {"query": query, "count": str(params.get("limit", 20))})
        if resp.get("ok"):
            matches = resp.get("messages", {}).get("matches", [])
            return {"ok": True, "data": {"results": [{"text": m.get("text", ""), "channel": m.get("channel", {}).get("name", ""),
                        "user": m.get("user", ""), "ts": m.get("ts", "")} for m in matches[:20]]}}
        return {"ok": False, "error": resp.get("error", "search failed")}


# ═══════════════════════════════════════════════════════════════
#  GITHUB INTEGRATION
# ═══════════════════════════════════════════════════════════════

class GitHubIntegration(Integration):
    """Real GitHub API integration for PR monitoring and management."""
    name = "github"
    description = "Monitor PRs, check CI status, comment on issues, manage repositories"
    icon = "🐙"
    config_schema = {
        "token": {"type": "string", "description": "GitHub Personal Access Token", "required": True},
        "owner": {"type": "string", "description": "Repository owner (org or user)", "required": True},
        "repo": {"type": "string", "description": "Repository name", "required": True},
    }

    BASE = "https://api.github.com"

    def _api(self, path, method="GET", data=None):
        token = self.config.get("token", "")
        if not token:
            raise Exception("No GitHub token configured")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self.BASE}{path}"
        return self._http(url, method=method, headers=headers, data=data)

    def _repo_path(self):
        owner = self.config.get("owner", "")
        repo = self.config.get("repo", "")
        if not owner or not repo:
            raise Exception("GitHub owner and repo must be configured")
        return f"/repos/{owner}/{repo}"

    def connect(self) -> dict:
        try:
            resp = self._api("/user")
            self.connected = True
            return {"ok": True, "detail": f"Authenticated as {resp.get('login', '?')}",
                    "user": resp.get("login"), "name": resp.get("name")}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def action_list_prs(self, params: dict) -> dict:
        """List open pull requests."""
        state = params.get("state", "open")
        resp = self._api(f"{self._repo_path()}/pulls?state={state}&per_page=30&sort=updated&direction=desc")
        prs = [{"number": p["number"], "title": p["title"], "state": p["state"],
                "user": p.get("user", {}).get("login", ""), "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"), "labels": [l["name"] for l in p.get("labels", [])],
                "draft": p.get("draft", False), "mergeable_state": p.get("mergeable_state", ""),
                "url": p.get("html_url", "")} for p in resp]
        return {"ok": True, "data": {"pull_requests": prs, "count": len(prs)}}

    def action_get_pr(self, params: dict) -> dict:
        """Get details of a specific PR."""
        number = params.get("number")
        if not number:
            return {"ok": False, "error": "PR number required"}
        resp = self._api(f"{self._repo_path()}/pulls/{number}")
        return {"ok": True, "data": {
            "number": resp["number"], "title": resp["title"], "state": resp["state"],
            "body": (resp.get("body") or "")[:2000],
            "user": resp.get("user", {}).get("login", ""),
            "additions": resp.get("additions", 0), "deletions": resp.get("deletions", 0),
            "changed_files": resp.get("changed_files", 0),
            "mergeable": resp.get("mergeable"), "merged": resp.get("merged", False),
            "labels": [l["name"] for l in resp.get("labels", [])],
            "url": resp.get("html_url", ""),
        }}

    def action_pr_comments(self, params: dict) -> dict:
        """Get comments on a PR."""
        number = params.get("number")
        if not number:
            return {"ok": False, "error": "PR number required"}
        resp = self._api(f"{self._repo_path()}/issues/{number}/comments?per_page=30")
        comments = [{"user": c.get("user", {}).get("login", ""), "body": (c.get("body") or "")[:500],
                      "created_at": c.get("created_at"), "id": c.get("id")} for c in resp]
        return {"ok": True, "data": {"comments": comments, "count": len(comments)}}

    def action_comment_on_pr(self, params: dict) -> dict:
        """Post a comment on a PR."""
        number = params.get("number")
        body = params.get("body", "")
        if not number or not body:
            return {"ok": False, "error": "PR number and body required"}
        resp = self._api(f"{self._repo_path()}/issues/{number}/comments", method="POST", data={"body": body})
        return {"ok": True, "data": {"id": resp.get("id"), "url": resp.get("html_url", "")}}

    def action_check_ci(self, params: dict) -> dict:
        """Check CI/check status for a PR or commit."""
        ref = params.get("ref") or params.get("sha", "")
        if not ref and params.get("number"):
            pr = self._api(f"{self._repo_path()}/pulls/{params['number']}")
            ref = pr.get("head", {}).get("sha", "")
        if not ref:
            return {"ok": False, "error": "ref, sha, or PR number required"}
        resp = self._api(f"{self._repo_path()}/commits/{ref}/check-runs?per_page=50")
        checks = [{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion", ""),
                    "started_at": c.get("started_at"), "completed_at": c.get("completed_at")}
                   for c in resp.get("check_runs", [])]
        all_pass = all(c["conclusion"] == "success" for c in checks if c["status"] == "completed")
        return {"ok": True, "data": {"checks": checks, "all_passing": all_pass, "count": len(checks)}}

    def action_list_issues(self, params: dict) -> dict:
        """List repository issues."""
        state = params.get("state", "open")
        labels = params.get("labels", "")
        url = f"{self._repo_path()}/issues?state={state}&per_page=30&sort=updated"
        if labels:
            url += f"&labels={labels}"
        resp = self._api(url)
        issues = [{"number": i["number"], "title": i["title"], "state": i["state"],
                    "user": i.get("user", {}).get("login", ""), "labels": [l["name"] for l in i.get("labels", [])],
                    "comments": i.get("comments", 0), "created_at": i.get("created_at")}
                   for i in resp if "pull_request" not in i]
        return {"ok": True, "data": {"issues": issues, "count": len(issues)}}

    def action_create_issue(self, params: dict) -> dict:
        """Create a new issue."""
        title = params.get("title", "")
        body = params.get("body", "")
        if not title:
            return {"ok": False, "error": "Issue title required"}
        data = {"title": title, "body": body}
        if params.get("labels"):
            data["labels"] = params["labels"]
        resp = self._api(f"{self._repo_path()}/issues", method="POST", data=data)
        return {"ok": True, "data": {"number": resp.get("number"), "url": resp.get("html_url", "")}}


# ═══════════════════════════════════════════════════════════════
#  WEBHOOK INTEGRATION
# ═══════════════════════════════════════════════════════════════

class WebhookIntegration(Integration):
    """Send HTTP webhooks to external services."""
    name = "webhook"
    description = "Send HTTP requests to webhook endpoints"
    icon = "🪝"
    config_schema = {
        "url": {"type": "string", "description": "Webhook URL", "required": True},
        "secret": {"type": "string", "description": "HMAC signing secret (optional)", "required": False},
        "headers": {"type": "object", "description": "Additional headers", "required": False},
    }

    def connect(self) -> dict:
        url = self.config.get("url", "")
        if not url:
            return {"ok": False, "detail": "No URL configured"}
        self.connected = True
        return {"ok": True, "detail": f"Webhook configured: {url[:60]}"}

    def action_send(self, params: dict) -> dict:
        """Send a webhook POST request."""
        url = params.get("url") or self.config.get("url", "")
        if not url:
            return {"ok": False, "error": "No URL"}
        payload = params.get("payload") or params.get("data") or {}
        headers = dict(self.config.get("headers", {}))
        # HMAC signing
        secret = self.config.get("secret", "")
        if secret:
            body_bytes = json.dumps(payload).encode("utf-8")
            sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            headers["X-Cortex-Signature"] = f"sha256={sig}"
        resp = self._http(url, method="POST", headers=headers, data=payload)
        return {"ok": True, "data": resp}


# ═══════════════════════════════════════════════════════════════
#  REST API INTEGRATION
# ═══════════════════════════════════════════════════════════════

class RestApiIntegration(Integration):
    """Generic REST API client for any endpoint."""
    name = "rest_api"
    description = "Make HTTP requests to any REST API"
    icon = "🌐"
    config_schema = {
        "base_url": {"type": "string", "description": "Base URL for the API", "required": True},
        "auth_type": {"type": "string", "description": "Auth type: bearer, api_key, basic, none", "required": False},
        "auth_value": {"type": "string", "description": "Auth token/key value", "required": False},
        "auth_header": {"type": "string", "description": "Custom auth header name (for api_key)", "required": False},
    }

    def _build_headers(self):
        headers = {}
        auth_type = self.config.get("auth_type", "none")
        auth_value = self.config.get("auth_value", "")
        if auth_type == "bearer" and auth_value:
            headers["Authorization"] = f"Bearer {auth_value}"
        elif auth_type == "api_key" and auth_value:
            header_name = self.config.get("auth_header", "X-API-Key")
            headers[header_name] = auth_value
        elif auth_type == "basic" and auth_value:
            import base64
            headers["Authorization"] = f"Basic {base64.b64encode(auth_value.encode()).decode()}"
        return headers

    def connect(self) -> dict:
        base_url = self.config.get("base_url", "")
        if not base_url:
            return {"ok": False, "detail": "No base_url configured"}
        self.connected = True
        return {"ok": True, "detail": f"REST API configured: {base_url[:60]}"}

    def action_get(self, params: dict) -> dict:
        """Make a GET request."""
        path = params.get("path", "")
        url = self.config.get("base_url", "").rstrip("/") + "/" + path.lstrip("/")
        resp = self._http(url, headers=self._build_headers())
        return {"ok": True, "data": resp}

    def action_post(self, params: dict) -> dict:
        """Make a POST request."""
        path = params.get("path", "")
        url = self.config.get("base_url", "").rstrip("/") + "/" + path.lstrip("/")
        resp = self._http(url, method="POST", headers=self._build_headers(), data=params.get("body", {}))
        return {"ok": True, "data": resp}

    def action_put(self, params: dict) -> dict:
        """Make a PUT request."""
        path = params.get("path", "")
        url = self.config.get("base_url", "").rstrip("/") + "/" + path.lstrip("/")
        resp = self._http(url, method="PUT", headers=self._build_headers(), data=params.get("body", {}))
        return {"ok": True, "data": resp}

    def action_delete(self, params: dict) -> dict:
        """Make a DELETE request."""
        path = params.get("path", "")
        url = self.config.get("base_url", "").rstrip("/") + "/" + path.lstrip("/")
        resp = self._http(url, method="DELETE", headers=self._build_headers())
        return {"ok": True, "data": resp}


# ═══════════════════════════════════════════════════════════════
#  EMAIL / SMTP INTEGRATION
# ═══════════════════════════════════════════════════════════════

class EmailIntegration(Integration):
    """Send emails via SMTP."""
    name = "email"
    description = "Send emails via SMTP (Gmail, Outlook, custom SMTP)"
    icon = "📧"
    config_schema = {
        "smtp_host": {"type": "string", "description": "SMTP server host", "required": True},
        "smtp_port": {"type": "integer", "description": "SMTP port (587 for TLS)", "required": True},
        "username": {"type": "string", "description": "SMTP username", "required": True},
        "password": {"type": "string", "description": "SMTP password / app password", "required": True},
        "from_email": {"type": "string", "description": "Sender email address", "required": True},
    }

    def connect(self) -> dict:
        try:
            import smtplib
            host = self.config.get("smtp_host", "")
            port = int(self.config.get("smtp_port", 587))
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.starttls()
                s.login(self.config.get("username", ""), self.config.get("password", ""))
            self.connected = True
            return {"ok": True, "detail": f"SMTP connected: {host}:{port}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def action_send_email(self, params: dict) -> dict:
        """Send an email."""
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        to = params.get("to", "")
        subject = params.get("subject", "")
        body = params.get("body", "")
        if not to or not subject:
            return {"ok": False, "error": "to and subject required"}

        msg = MIMEMultipart()
        msg["From"] = self.config.get("from_email", "")
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html" if params.get("html") else "plain"))

        host = self.config.get("smtp_host", "")
        port = int(self.config.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(self.config.get("username", ""), self.config.get("password", ""))
            s.send_message(msg)
        return {"ok": True, "data": {"sent_to": to, "subject": subject}}


# ═══════════════════════════════════════════════════════════════
#  INTEGRATION MANAGER
# ═══════════════════════════════════════════════════════════════

# Registry of available integration types
INTEGRATION_TYPES = {
    "slack": SlackIntegration,
    "github": GitHubIntegration,
    "webhook": WebhookIntegration,
    "rest_api": RestApiIntegration,
    "email": EmailIntegration,
}


class IntegrationManager:
    """Manages all integrations for the platform."""

    def __init__(self):
        self._instances: Dict[str, Dict[str, Integration]] = {}  # agent_id -> {name -> Integration}
        self._global: Dict[str, Integration] = {}  # platform-wide integrations

    def register_global(self, name: str, integration_type: str, config: dict) -> dict:
        """Register a platform-wide integration."""
        cls = INTEGRATION_TYPES.get(integration_type)
        if not cls:
            return {"ok": False, "error": f"Unknown type: {integration_type}. Available: {list(INTEGRATION_TYPES.keys())}"}
        instance = cls(config)
        result = instance.connect()
        self._global[name] = instance
        return {"ok": True, "connected": result.get("ok", False), "detail": result.get("detail", "")}

    def register_for_agent(self, agent_id: str, name: str, integration_type: str, config: dict) -> dict:
        """Register an integration for a specific agent."""
        cls = INTEGRATION_TYPES.get(integration_type)
        if not cls:
            return {"ok": False, "error": f"Unknown type: {integration_type}"}
        instance = cls(config)
        result = instance.connect()
        if agent_id not in self._instances:
            self._instances[agent_id] = {}
        self._instances[agent_id][name] = instance
        return {"ok": True, "connected": result.get("ok", False), "detail": result.get("detail", "")}

    def execute(self, agent_id: str, integration_name: str, action: str, params: dict = None) -> dict:
        """Execute an integration action for an agent."""
        # Check agent-specific first, then global
        instance = self._instances.get(agent_id, {}).get(integration_name)
        if not instance:
            instance = self._global.get(integration_name)
        if not instance:
            return {"ok": False, "error": f"Integration '{integration_name}' not found for agent {agent_id}"}
        return instance.execute(action, params or {})

    def list_integrations(self, agent_id: str = None) -> dict:
        """List all integrations, optionally for a specific agent."""
        result = {}
        # Global
        for name, inst in self._global.items():
            result[name] = {**inst.status(), "scope": "global"}
        # Agent-specific
        if agent_id and agent_id in self._instances:
            for name, inst in self._instances[agent_id].items():
                result[name] = {**inst.status(), "scope": "agent"}
        return result

    def remove(self, agent_id: str = None, name: str = "") -> dict:
        """Remove an integration."""
        if agent_id and agent_id in self._instances and name in self._instances[agent_id]:
            del self._instances[agent_id][name]
            return {"ok": True}
        if name in self._global:
            del self._global[name]
            return {"ok": True}
        return {"ok": False, "error": "not found"}

    def available_types(self) -> list:
        """List all available integration types and their schemas."""
        return [{
            "type": name, "name": cls.name, "description": cls.description,
            "icon": cls.icon, "config_schema": cls.config_schema,
            "actions": cls({}).available_actions(),
        } for name, cls in INTEGRATION_TYPES.items()]


# Singleton
integration_manager = IntegrationManager()
