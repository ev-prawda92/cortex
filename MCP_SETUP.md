# CORTEX MCP Server — Setup Guide

CORTEX exposes its agent orchestration platform as an MCP server, so your agents are callable from **Cursor**, **Claude Code**, **VS Code**, and any MCP-compatible client.

## Prerequisites

```bash
pip install "mcp[cli]" httpx
```

Make sure CORTEX itself is running:

```bash
cd ~/Desktop/CortexUpdated
uvicorn cortex:app --reload
```

---

## Cursor

Add to `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for global):

```json
{
  "mcpServers": {
    "cortex": {
      "command": "python",
      "args": ["/Users/evan/Desktop/CortexUpdated/cortex_mcp_server.py"],
      "env": {
        "CORTEX_URL": "http://localhost:8000"
      }
    }
  }
}
```

Restart Cursor. Your CORTEX agents will appear as tools in Cursor's agent mode.

---

## Claude Code

Add to your Claude Code MCP config (`~/.claude/claude_code_config.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "cortex": {
      "command": "python",
      "args": ["/Users/evan/Desktop/CortexUpdated/cortex_mcp_server.py"],
      "env": {
        "CORTEX_URL": "http://localhost:8000"
      }
    }
  }
}
```

---

## VS Code (Copilot MCP)

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "cortex": {
      "command": "python",
      "args": ["${workspaceFolder}/cortex_mcp_server.py"],
      "env": {
        "CORTEX_URL": "http://localhost:8000"
      }
    }
  }
}
```

---

## Remote / Team Access (HTTP mode)

For sharing across a team or running on a server:

```bash
CORTEX_URL=http://localhost:8000 python cortex_mcp_server.py --http --port 3100
```

Then in any MCP client config:

```json
{
  "mcpServers": {
    "cortex": {
      "url": "http://your-server:3100/mcp"
    }
  }
}
```

---

## Available Tools

Once connected, these tools are available in your AI coding environment:

| Tool | Description |
|---|---|
| `list_agents` | List all CORTEX agents with status and metrics |
| `get_agent` | Get full config details for a specific agent |
| `run_agent` | Execute an agent with input and get results |
| `get_agent_runs` | View run history for an agent |
| `diagnose_agent` | Run health diagnostics on an agent |
| `register_agent` | Create a new agent from your IDE |
| `control_agent` | Start, stop, or deploy an agent |
| `import_agent` | Import agents from LangChain, CrewAI, OpenAI |
| `get_portfolio_metrics` | Portfolio-wide agent metrics |
| `get_system_diagnostics` | Platform health checks |
| `get_settings` | View provider and model configuration |
| `generate_integration` | Get ready-to-use code to call an agent |

## Example Usage in Cursor

Once configured, you can ask Cursor things like:

- *"List my CORTEX agents"*
- *"Run my research agent with: summarize the latest AI safety papers"*
- *"Register a new agent called 'code-reviewer' that reviews pull requests"*
- *"Show me the diagnostics for my router agent"*
- *"Generate a Python integration snippet for my research agent"*

The AI will call the CORTEX MCP tools automatically.
