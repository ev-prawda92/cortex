"""
CORTEX MCP Server
─────────────────
Exposes CORTEX agents as MCP tools so they can be called from
Cursor, Claude Code, VS Code, or any MCP-compatible client.

Requirements:
    pip install "mcp[cli]" httpx

Usage (stdio — default for Cursor/Claude Code):
    python cortex_mcp_server.py

    Or with a custom CORTEX URL:
    CORTEX_URL=http://localhost:8000 python cortex_mcp_server.py

Usage (HTTP — for remote/team access):
    python cortex_mcp_server.py --http --port 3100
"""

import os
import sys
import json
import asyncio
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ── Configuration ──────────────────────────────────────────────
CORTEX_URL = os.environ.get("CORTEX_URL", "http://localhost:8000")
CORTEX_SESSION = os.environ.get("CORTEX_SESSION", "")  # optional auth cookie

mcp = FastMCP("cortex")


def _client() -> httpx.AsyncClient:
    """Create an async HTTP client pointed at the CORTEX API."""
    headers = {}
    cookies = {}
    if CORTEX_SESSION:
        cookies["session"] = CORTEX_SESSION
    return httpx.AsyncClient(
        base_url=CORTEX_URL,
        headers=headers,
        cookies=cookies,
        timeout=120.0,
    )


# ═══════════════════════════════════════════════════════════════
#  TOOLS
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def list_agents() -> str:
    """List all agents registered in CORTEX with their status, type, and metrics.

    Returns a summary of every agent including id, name, description,
    status (running/stopped/error), live flag, and key metrics.
    """
    async with _client() as client:
        r = await client.get("/api/agents")
        r.raise_for_status()
        data = r.json()

    agents = data.get("agents", [])
    if not agents:
        return "No agents registered in CORTEX."

    lines = [f"CORTEX Agents ({data['total']} total — {data['running']} running, {data['error']} error, {data['stopped']} stopped)\n"]
    for a in agents:
        status_icon = {"running": "🟢", "stopped": "⚪", "error": "🔴"}.get(a["status"], "⚪")
        live_tag = " [LIVE]" if a.get("live") else ""
        lines.append(
            f"  {status_icon} {a['id']} — {a['name']}{live_tag}\n"
            f"     {a.get('description', 'No description')}\n"
            f"     type: {a.get('type', 'custom')} | "
            f"data_sources: {a.get('data_sources_count', 0)} | "
            f"tools: {a.get('tools_count', 0)}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_agent(agent_id: str) -> str:
    """Get full details for a specific CORTEX agent including its configuration.

    Args:
        agent_id: The agent identifier (e.g. 'research', 'router', or a custom id)
    """
    async with _client() as client:
        r = await client.get(f"/api/agents/{agent_id}")
        r.raise_for_status()
        data = r.json()

    # Format a readable summary
    config = data.get("config", {})
    model = config.get("model", {})
    execution = config.get("execution", {})
    behavior = config.get("behavior", {})

    parts = [
        f"Agent: {data.get('name', agent_id)}",
        f"ID: {agent_id}",
        f"Status: {data.get('status', 'unknown')} | Live: {data.get('live', False)}",
        f"Version: {data.get('version', 1)} | History: {data.get('history_count', 0)} versions",
        f"\nModel:",
        f"  Provider: {model.get('provider', 'n/a')}",
        f"  Model: {model.get('model_name', 'n/a')}",
        f"  Temperature: {model.get('temperature', 'n/a')}",
        f"  Max tokens: {model.get('max_tokens', 'n/a')}",
        f"\nExecution:",
        f"  Max steps: {execution.get('max_steps', 'n/a')}",
        f"  Confidence threshold: {execution.get('confidence_threshold', 'n/a')}",
        f"  Escalation: {execution.get('escalation_policy', 'n/a')}",
        f"\nBehavior:",
        f"  System prompt: {(behavior.get('system_prompt', '') or 'none')[:200]}...",
    ]

    # Data sources
    sources = config.get("data_sources", [])
    if sources:
        parts.append(f"\nData Sources ({len(sources)}):")
        for s in sources:
            parts.append(f"  - {s.get('name', 'unnamed')} ({s.get('type', 'unknown')})")

    # Tools
    tools = config.get("tools", [])
    if tools:
        parts.append(f"\nTools ({len(tools)}):")
        for t in tools:
            parts.append(f"  - {t.get('name', 'unnamed')}: {t.get('description', '')}")

    return "\n".join(parts)


@mcp.tool()
async def run_agent(agent_id: str, input: str) -> str:
    """Execute a CORTEX agent with the given input and return the result.

    The agent must be live (deployed) to run. Use list_agents to see
    which agents are available and their live status.

    Args:
        agent_id: The agent to run (e.g. 'research', 'router')
        input: The input text/query to send to the agent
    """
    async with _client() as client:
        r = await client.post(
            f"/api/agents/{agent_id}/run",
            json={"claim": input},
        )
        r.raise_for_status()
        data = r.json()

    run = data.get("run", {})
    detail = run.get("detail", {})

    parts = [
        f"Agent: {agent_id}",
        f"Outcome: {run.get('outcome', 'unknown')}",
        f"Provider: {run.get('provider', 'n/a')} / {run.get('model', 'n/a')}",
        f"Steps: {run.get('steps_used', 0)}",
        f"\n── Summary ──",
        detail.get("summary", "No summary available."),
    ]

    if detail.get("reason"):
        parts.extend(["\n── Reasoning ──", detail["reason"]])

    if detail.get("citations"):
        parts.append("\n── Citations ──")
        for c in detail["citations"]:
            parts.append(f"  • {c}")

    if detail.get("route_to"):
        parts.append(f"\nRouted to: {detail['route_to']}")

    # Include trace if present
    trace = run.get("trace", [])
    if trace:
        parts.append(f"\n── Trace ({len(trace)} steps) ──")
        for step in trace:
            parts.append(f"  [{step.get('kind', '?')}] {step.get('text', '')}")

    return "\n".join(parts)


@mcp.tool()
async def get_agent_runs(agent_id: str) -> str:
    """Get the run history for a specific agent.

    Args:
        agent_id: The agent to get runs for
    """
    async with _client() as client:
        r = await client.get(f"/api/agents/{agent_id}/runs")
        r.raise_for_status()
        data = r.json()

    runs = data.get("runs", [])
    if not runs:
        return f"No runs recorded for agent '{agent_id}'."

    lines = [f"Run history for {agent_id} ({len(runs)} runs):\n"]
    for run in runs[-10:]:  # last 10
        lines.append(
            f"  [{run.get('outcome', '?')}] "
            f"{run.get('claim', '')[:80]} "
            f"({run.get('steps_used', 0)} steps, "
            f"v{run.get('config_version', '?')})"
        )
    return "\n".join(lines)


@mcp.tool()
async def diagnose_agent(agent_id: str) -> str:
    """Run diagnostics on a specific agent — check its health, config validity, and readiness.

    Args:
        agent_id: The agent to diagnose
    """
    async with _client() as client:
        r = await client.get(f"/api/agents/{agent_id}/diagnosis")
        r.raise_for_status()
        data = r.json()

    return json.dumps(data, indent=2)


@mcp.tool()
async def register_agent(
    name: str,
    description: str,
    provider: str = "anthropic",
    model_name: str = "claude-sonnet-5",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    system_prompt: str = "",
    endpoint_type: str = "embedded",
) -> str:
    """Register a new agent in CORTEX.

    Args:
        name: Human-readable agent name
        description: What this agent does
        provider: LLM provider — 'anthropic', 'openai', or 'gemini'
        model_name: Model to use (e.g. 'claude-sonnet-5', 'gpt-5.6-terra')
        temperature: Sampling temperature (0.0–1.0)
        max_tokens: Max response tokens
        system_prompt: System instructions for the agent
        endpoint_type: 'embedded' (runs in CORTEX), 'rest', or 'webhook'
    """
    payload = {
        "name": name,
        "description": description,
        "provider": provider,
        "model_name": model_name,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "system_prompt": system_prompt,
        "endpoint_type": endpoint_type,
    }

    async with _client() as client:
        r = await client.post("/api/agents/register", json=payload)
        r.raise_for_status()
        data = r.json()

    return f"Agent registered successfully!\n  ID: {data.get('id', 'unknown')}\n  Name: {name}\n  Provider: {provider}/{model_name}\n  Type: {endpoint_type}"


@mcp.tool()
async def control_agent(agent_id: str, action: str) -> str:
    """Control an agent — start, stop, or toggle its live/deployed state.

    Args:
        agent_id: The agent to control
        action: One of 'start', 'stop', 'golive', 'pause'
    """
    async with _client() as client:
        r = await client.post(
            f"/api/agents/{agent_id}/control",
            json={"action": action},
        )
        r.raise_for_status()
        data = r.json()

    return f"Agent '{agent_id}': {data.get('status', 'unknown')} (live: {data.get('live', False)})"


@mcp.tool()
async def import_agent(config: str, source_format: str = "auto") -> str:
    """Import an agent from another platform (LangChain, CrewAI, OpenAI, or raw JSON/YAML).

    Paste the agent's configuration and CORTEX will auto-detect the format
    and normalize it into a CORTEX agent.

    Args:
        config: The agent configuration as a JSON or YAML string
        source_format: Format hint — 'auto', 'langchain', 'crewai', 'openai', 'cortex', 'raw'
    """
    async with _client() as client:
        r = await client.post(
            "/api/agents/import",
            json={"config": config, "format": source_format},
        )
        r.raise_for_status()
        data = r.json()

    return (
        f"Agent imported successfully!\n"
        f"  ID: {data.get('id', 'unknown')}\n"
        f"  Name: {data.get('name', 'imported')}\n"
        f"  Detected format: {data.get('detected_format', source_format)}"
    )


@mcp.tool()
async def get_portfolio_metrics() -> str:
    """Get portfolio-wide metrics across all CORTEX agents — containment rate, resolution, escalation trends."""
    async with _client() as client:
        r = await client.get("/api/metrics/portfolio")
        r.raise_for_status()
        data = r.json()

    return json.dumps(data, indent=2)


@mcp.tool()
async def get_system_diagnostics() -> str:
    """Get CORTEX system diagnostics — overall health, provider status, and configuration checks."""
    async with _client() as client:
        r = await client.get("/api/diagnostics")
        r.raise_for_status()
        data = r.json()

    checks = data.get("checks", [])
    lines = [f"CORTEX Diagnostics — {data.get('status', 'unknown').upper()}\n"]
    for c in checks:
        icon = "✅" if c.get("ok") else "❌"
        lines.append(f"  {icon} {c.get('name', '?')}: {c.get('detail', '')}")
    return "\n".join(lines)


@mcp.tool()
async def get_settings() -> str:
    """Get the current CORTEX platform settings — active provider, configured models, API key status."""
    async with _client() as client:
        r = await client.get("/api/settings")
        r.raise_for_status()
        data = r.json()

    parts = [f"Active provider: {data.get('active', 'none')}\n"]
    for provider, info in data.get("providers", {}).items():
        configured = "✅ configured" if info.get("configured") else "❌ not configured"
        parts.append(f"  {provider}: {configured} (model: {info.get('model', 'default')})")
    return "\n".join(parts)


@mcp.tool()
async def generate_integration(agent_id: str, format: str = "python") -> str:
    """Generate integration code for an agent — ready-to-use snippets to call this agent from your app.

    Args:
        agent_id: The agent to generate code for
        format: Output format — 'python', 'curl', 'javascript', 'typescript'
    """
    async with _client() as client:
        r = await client.get(f"/api/agents/{agent_id}/integration/{format}")
        r.raise_for_status()
        data = r.json()

    return data.get("code", "No integration code generated.")


# ═══════════════════════════════════════════════════════════════
#  RESOURCES
# ═══════════════════════════════════════════════════════════════

@mcp.resource("cortex://agents")
async def resource_agents() -> str:
    """List of all CORTEX agents and their current status."""
    return await list_agents()


@mcp.resource("cortex://settings")
async def resource_settings() -> str:
    """Current CORTEX platform configuration."""
    return await get_settings()


@mcp.resource("cortex://diagnostics")
async def resource_diagnostics() -> str:
    """CORTEX system health and diagnostics."""
    return await get_system_diagnostics()


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    transport = "stdio"
    port = 3100

    args = sys.argv[1:]
    if "--http" in args:
        transport = "streamable-http"
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    if transport == "streamable-http":
        print(f"CORTEX MCP Server starting on http://0.0.0.0:{port}/mcp")
        mcp.run(transport=transport, port=port)
    else:
        mcp.run(transport="stdio")
