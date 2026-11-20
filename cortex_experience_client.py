"""Small, provider-neutral HTTP client for Cortex Experience v1.

Used by the Claude Code MCP integration, but independent of Claude and MCP.
No automatic memory capture or model-provider calls.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from urllib.parse import quote, urlsplit

import httpx


DEFAULT_URL = "http://127.0.0.1:3000"
DEFAULT_CREDENTIALS_FILE = Path.home() / ".config" / "cortex" / "credentials.json"


class CortexConnectionError(Exception):
    """An actionable, non-secret error for the developer."""


def credential_path() -> Path:
    custom = os.getenv("CORTEX_CREDENTIALS_FILE", "").strip()
    return Path(custom).expanduser() if custom else DEFAULT_CREDENTIALS_FILE


def load_credentials() -> dict:
    path = credential_path()
    if not path.exists():
        return {}
    if not path.is_file():
        raise CortexConnectionError("Cortex credentials path is not a file.")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CortexConnectionError(
            f"Cortex credentials are accessible to other users: {path}. "
            "Run chmod 600 on the file before continuing."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CortexConnectionError("Could not read Cortex credentials. Re-run setup.") from exc
    if not isinstance(data, dict):
        raise CortexConnectionError("Invalid Cortex credentials format.")
    return data


def normalize_base_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise CortexConnectionError("CORTEX_URL must be an http(s) URL.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise CortexConnectionError("CORTEX_URL must not contain credentials, query or fragment.")
    if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CortexConnectionError("Remote Cortex URLs must use HTTPS.")
    return cleaned


class CortexExperienceClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        # Environment variables override the local credential file.
        config = load_credentials() if not (base_url and api_key) else {}
        self.base_url = normalize_base_url(
            base_url or os.getenv("CORTEX_URL") or config.get("url") or DEFAULT_URL
        )
        self.api_key = api_key or os.getenv("CORTEX_API_KEY") or config.get("api_key") or ""
        if not self.api_key.startswith("ctx_"):
            raise CortexConnectionError(
                "Cortex API key missing. Run python scripts/setup_claude_code.py "
                "from your Cortex checkout or set CORTEX_API_KEY."
            )
        self.transport = transport

    def _request(self, method: str, endpoint: str, *, params=None, payload=None):
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=12.0,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = client.request(
                    method, f"/api/v1/experience{endpoint}", params=params, json=payload
                )
            if response.status_code in {401, 403}:
                raise CortexConnectionError(
                    f"Cortex rejected the credentials or scope (HTTP {response.status_code}). "
                    "Run setup again and use an account that owns the memories."
                )
            if response.status_code >= 400:
                # Never forward raw server exception details or secrets to Claude.
                raise CortexConnectionError(
                    f"Cortex API returned HTTP {response.status_code} for {method} {endpoint}."
                )
            return response.json() if response.content else None
        except httpx.RequestError as exc:
            raise CortexConnectionError(
                f"Could not contact Cortex at {self.base_url}. Start python cortex.py "
                "in a separate terminal."
            ) from exc
        except ValueError as exc:
            raise CortexConnectionError("Cortex returned invalid JSON.") from exc

    def status(self) -> dict:
        return self._request("GET", "/summary")

    def recall(self, query: str, *, verified_only=True, limit=10) -> dict:
        if not query.strip() or len(query) > 200:
            raise CortexConnectionError("Recall query must contain 1–200 characters.")
        return self._request(
            "GET", "/memories",
            params={"q": query, "limit": max(1, min(20, limit)),
                    **({"status": "verified"} if verified_only else {})},
        )

    def list_memories(self, *, verified_only=True, limit=10) -> dict:
        return self._request(
            "GET", "/memories",
            params={"limit": max(1, min(20, limit)),
                    **({"status": "verified"} if verified_only else {})},
        )

    def remember(
        self, *, title: str, content: str, kind="knowledge",
        tags: list[str] | None = None, agent_id: str | None = None,
    ) -> dict:
        # Always draft; training export requires separate human verification.
        return self._request(
            "POST", "/memories",
            payload={"title": title, "content": content, "kind": kind,
                     "tags": tags or [], "agent_id": agent_id,
                     "training_opt_in": False},
        )

    def review(self, memory_id: str, *, verification_note: str) -> dict:
        return self._request(
            "PATCH", f"/memories/{quote(memory_id, safe='')}",
            payload={"status": "verified", "verification_note": verification_note,
                     "training_opt_in": False},
        )

    def edit(self, memory_id: str, *, title: str, content: str,
             kind: str, tags: list[str]) -> dict:
        return self._request(
            "PATCH", f"/memories/{quote(memory_id, safe='')}/content",
            payload={"title": title, "content": content, "kind": kind, "tags": tags},
        )

    def performance(self, agent_id: str) -> dict:
        return self._request(
            "GET", f"/agents/{quote(agent_id, safe='')}/performance"
        )

    def dataset_preview(self) -> dict:
        return self._request("GET", "/datasets/preview")
