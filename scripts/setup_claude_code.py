#!/usr/bin/env python3
"""Interactive local-only setup for Cortex Experience in Claude Code.

Logs into your existing Cortex account, creates a least-privilege durable
API key, stores it in a mode-0600 file outside Git, and registers the MCP
stdio server at Claude Code user scope for use across projects.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cortex_experience_client import (  # noqa: E402
    CortexExperienceClient, CortexConnectionError, credential_path,
    normalize_base_url,
)


def save_credentials(url: str, key: str) -> Path:
    """Atomic credentials file update with restrictive permissions."""
    path = credential_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise CortexConnectionError("Refusing to replace symlinked credentials.")
    fd, temp_name = tempfile.mkstemp(prefix=".credentials-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump({"url": url, "api_key": key}, target)
            target.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def provision_key(url: str, email: str, password: str) -> str:
    try:
        with httpx.Client(base_url=url, timeout=12, follow_redirects=False,
                          trust_env=False) as client:
            login = client.post("/api/auth/login", json={
                "email": email.strip(), "password": password,
            })
            if login.status_code != 200:
                raise CortexConnectionError(
                    f"Login failed (HTTP {login.status_code}). "
                    "Use the same email and password as the Cortex browser account, or "
                    "create a Claude Code key in Cortex Settings > API Keys and rerun "
                    "setup with --use-existing-key."
                )
            token = login.json().get("token")
            if not token:
                raise CortexConnectionError("Cortex did not provide a login token.")
            key_response = client.post("/api/keys", headers={
                "Authorization": f"Bearer {token}",
            }, json={
                "name": "Claude Code — Cortex Experience",
                "scopes": ["experience:read", "experience:write"],
            })
            if key_response.status_code != 200:
                raise CortexConnectionError(
                    f"Unable to create scoped key (HTTP {key_response.status_code})."
                )
            key = key_response.json().get("key", "")
            if not key.startswith("ctx_"):
                raise CortexConnectionError("Cortex did not return a usable API key.")
            return key
    except httpx.RequestError as exc:
        raise CortexConnectionError(
            "Cortex is unreachable. Start python cortex.py in another terminal."
        ) from exc


def install_skill() -> Path:
    source = ROOT / ".claude" / "skills" / "cortex" / "SKILL.md"
    destination = Path.home() / ".claude" / "skills" / "cortex" / "SKILL.md"
    if destination.exists():
        previous = destination.read_text(encoding="utf-8")
        if previous != source.read_text(encoding="utf-8") and (
            "Cortex Experience skill — maintained in ev-prawda92/cortex"
            not in previous
        ):
            raise CortexConnectionError(
                f"Different /cortex skill already exists at {destination}; "
                "move it manually before installing."
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def register_mcp() -> str:
    claude_cli = "claude" if shutil.which("claude") else None
    if claude_cli is None:
        local_cli = Path.home() / ".local" / "bin" / "claude"
        if local_cli.is_file():
            claude_cli = str(local_cli)
    if claude_cli is None:
        return ("Claude Code CLI not found. Install Claude Code, then run:\n"
                f'  claude mcp add --scope user --transport stdio cortex-experience -- '
                f'{sys.executable} {ROOT / "cortex_experience_mcp.py"}')
    check = subprocess.run([claude_cli, "mcp", "get", "cortex-experience"],
                           capture_output=True, text=True, check=False)
    if check.returncode == 0:
        return ("An MCP server named cortex-experience is already configured. "
                "Check its launch command with: claude mcp get cortex-experience. "
                "To replace an old installation, remove it manually with "
                "claude mcp remove --scope user cortex-experience, then rerun setup.")
    cmd = [claude_cli, "mcp", "add", "--scope", "user", "--transport", "stdio",
           "cortex-experience", "--", sys.executable,
           str(ROOT / "cortex_experience_mcp.py")]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ("Claude MCP registration did not succeed. Run this command manually:\n"
                + " ".join(cmd))
    return "Claude Code MCP registered at user scope: cortex-experience."


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect Cortex Experience to Claude Code")
    parser.add_argument("--url", default=os.environ.get("CORTEX_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--use-existing-key", action="store_true",
                        help="Paste an existing Cortex API key privately; works with OAuth accounts")
    args = parser.parse_args()
    try:
        url = normalize_base_url(args.url)
        print(f"Cortex Experience setup → {url}")
        if args.use_existing_key:
            key = getpass.getpass("Cortex API key (hidden): ").strip()
            if not key.startswith("ctx_"):
                raise CortexConnectionError("Expected a Cortex API key beginning with ctx_.")
        else:
            email = input("Cortex account email: ").strip()
            password = getpass.getpass("Cortex account password (hidden): ")
            if not email or not password:
                raise CortexConnectionError("Email and password are required.")
            key = provision_key(url, email, password)
        try:
            summary = CortexExperienceClient(base_url=url, api_key=key).status()
        except CortexConnectionError:
            raise
        path = save_credentials(url, key)
        skill_path = install_skill()
        print(f"Connected! Cortex memories visible to this account: {summary['memory_count']}")
        print(f"API key stored securely: {path} (mode 600)")
        print(f"Claude skill installed: {skill_path}")
        print(register_mcp())
        print("Next: start/restart 'claude' in any project, run /mcp, then /cortex.")
        return 0
    except (CortexConnectionError, OSError) as exc:
        print("Setup error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
