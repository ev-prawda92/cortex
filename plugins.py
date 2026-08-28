"""
CORTEX Plugin System
═══════════════════
Extensible plugin architecture for adding tools, integrations, and behaviors.

Plugins are JSON manifests that declare:
  - Tools they provide (callable by agents)
  - Integrations they add
  - Hooks they attach to (pre-run, post-run, on-error, on-escalate)
  - Configuration they need

Plugin manifest format:
{
    "name": "my-plugin",
    "version": "1.0.0",
    "description": "What this plugin does",
    "author": "author@example.com",
    "tools": [...],
    "integrations": [...],
    "hooks": {...},
    "config_schema": {...}
}

Usage:
    from plugins import PluginManager
    mgr = PluginManager()
    mgr.install(manifest_dict)
    mgr.enable("my-plugin")
    result = mgr.call_tool("my-plugin", "tool_name", {"param": "value"})
"""

import json
import time
import copy
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any


# ═══════════════════════════════════════════════════════════════
#  PLUGIN MODEL
# ═══════════════════════════════════════════════════════════════

class Plugin:
    """Represents an installed plugin."""

    def __init__(self, manifest: dict):
        self.name = manifest.get("name", "unknown")
        self.version = manifest.get("version", "0.0.0")
        self.description = manifest.get("description", "")
        self.author = manifest.get("author", "")
        self.icon = manifest.get("icon", "🔌")
        self.category = manifest.get("category", "general")
        self.manifest = manifest

        # Tools this plugin provides
        self.tools: List[dict] = manifest.get("tools", [])

        # Integrations this plugin adds
        self.integrations: List[dict] = manifest.get("integrations", [])

        # Hooks: pre_run, post_run, on_error, on_escalate, on_status_change
        self.hooks: Dict[str, dict] = manifest.get("hooks", {})

        # Plugin config
        self.config_schema: dict = manifest.get("config_schema", {})
        self.config: dict = {}

        # State
        self.enabled = False
        self.installed_at = datetime.now(timezone.utc).isoformat()
        self.install_count = 0  # how many agents use this plugin
        self.last_invoked = None
        self.invoke_count = 0
        self.error_count = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "author": self.author,
            "icon": self.icon, "category": self.category,
            "enabled": self.enabled,
            "tools": [{"name": t["name"], "description": t.get("description", "")} for t in self.tools],
            "integrations": [i.get("type", "") for i in self.integrations],
            "hooks": list(self.hooks.keys()),
            "config_schema": self.config_schema,
            "config": self.config,
            "installed_at": self.installed_at,
            "install_count": self.install_count,
            "invoke_count": self.invoke_count,
            "error_count": self.error_count,
        }


# ═══════════════════════════════════════════════════════════════
#  BUILT-IN PLUGINS
# ═══════════════════════════════════════════════════════════════

BUILTIN_PLUGINS = [
    {
        "name": "cortex-logger",
        "version": "1.0.0",
        "description": "Enhanced logging — writes structured run logs with timing, token counts, and trace summaries",
        "author": "cortex",
        "icon": "📝",
        "category": "observability",
        "tools": [
            {"name": "log_structured", "description": "Write a structured log entry",
             "parameters": ["level", "message", "data"]},
        ],
        "hooks": {
            "post_run": {"action": "log", "template": "Run {outcome} in {duration}s — {tokens} tokens"},
        },
    },
    {
        "name": "cortex-alerts",
        "version": "1.0.0",
        "description": "Configurable alerting — send notifications on errors, escalations, or custom conditions",
        "author": "cortex",
        "icon": "🔔",
        "category": "monitoring",
        "tools": [
            {"name": "send_alert", "description": "Send an alert notification",
             "parameters": ["channel", "severity", "message"]},
        ],
        "hooks": {
            "on_error": {"action": "alert", "severity": "high"},
            "on_escalate": {"action": "alert", "severity": "medium"},
        },
        "config_schema": {
            "alert_channels": {"type": "array", "description": "Where to send alerts (slack, email, webhook)"},
            "min_severity": {"type": "string", "description": "Minimum severity to alert on", "default": "medium"},
        },
    },
    {
        "name": "cortex-cost-guard",
        "version": "1.0.0",
        "description": "Cost guardrails — set per-agent and fleet-wide spending limits with automatic pause",
        "author": "cortex",
        "icon": "💰",
        "category": "governance",
        "tools": [
            {"name": "check_budget", "description": "Check remaining budget for an agent",
             "parameters": ["agent_id"]},
        ],
        "hooks": {
            "pre_run": {"action": "check_budget", "pause_on_exceed": True},
        },
        "config_schema": {
            "daily_limit_usd": {"type": "number", "description": "Daily spend limit per agent", "default": 10.0},
            "monthly_limit_usd": {"type": "number", "description": "Monthly fleet limit", "default": 500.0},
        },
    },
    {
        "name": "cortex-data-redact",
        "version": "1.0.0",
        "description": "PII/secret redaction — automatically scrub sensitive data from agent inputs and outputs",
        "author": "cortex",
        "icon": "🔒",
        "category": "security",
        "tools": [],
        "hooks": {
            "pre_run": {"action": "redact_input", "patterns": ["ssn", "credit_card", "api_key", "email", "phone"]},
            "post_run": {"action": "redact_output", "patterns": ["ssn", "credit_card", "api_key"]},
        },
        "config_schema": {
            "patterns": {"type": "array", "description": "Patterns to redact", "default": ["ssn", "credit_card", "api_key"]},
            "replacement": {"type": "string", "description": "Replacement text", "default": "[REDACTED]"},
        },
    },
    {
        "name": "cortex-retry-smart",
        "version": "1.0.0",
        "description": "Intelligent retry — adapts retry strategy based on error type and provider health",
        "author": "cortex",
        "icon": "🔄",
        "category": "reliability",
        "tools": [],
        "hooks": {
            "on_error": {"action": "smart_retry", "max_retries": 3, "backoff": "exponential"},
        },
        "config_schema": {
            "max_retries": {"type": "integer", "default": 3},
            "backoff_base": {"type": "number", "default": 2.0},
            "retry_on": {"type": "array", "default": ["rate_limit", "timeout", "server_error"]},
        },
    },
    {
        "name": "cortex-eval",
        "version": "1.0.0",
        "description": "Output evaluation — score agent outputs for quality, relevance, and safety",
        "author": "cortex",
        "icon": "✅",
        "category": "quality",
        "tools": [
            {"name": "evaluate_output", "description": "Score an agent output",
             "parameters": ["output", "criteria"]},
        ],
        "hooks": {
            "post_run": {"action": "evaluate", "criteria": ["relevance", "completeness", "safety"]},
        },
        "config_schema": {
            "min_quality_score": {"type": "number", "default": 0.7},
            "auto_flag_below": {"type": "number", "default": 0.5},
        },
    },
]


# ═══════════════════════════════════════════════════════════════
#  PLUGIN MANAGER
# ═══════════════════════════════════════════════════════════════

class PluginManager:
    """Manages plugin lifecycle: install, enable, configure, invoke."""

    def __init__(self):
        self._plugins: Dict[str, Plugin] = {}
        self._agent_plugins: Dict[str, List[str]] = {}  # agent_id -> list of plugin names
        self._tool_handlers: Dict[str, Callable] = {}  # custom tool handlers
        # Pre-load builtins
        for manifest in BUILTIN_PLUGINS:
            self._plugins[manifest["name"]] = Plugin(manifest)

    # ── Install / Remove ──

    def install(self, manifest: dict) -> dict:
        """Install a plugin from a manifest."""
        name = manifest.get("name", "")
        if not name:
            return {"ok": False, "error": "Plugin name required"}
        if name in self._plugins:
            # Update existing
            old = self._plugins[name]
            self._plugins[name] = Plugin(manifest)
            self._plugins[name].enabled = old.enabled
            self._plugins[name].config = old.config
            return {"ok": True, "action": "updated", "plugin": name}
        self._plugins[name] = Plugin(manifest)
        return {"ok": True, "action": "installed", "plugin": name}

    def install_from_url(self, url: str) -> dict:
        """Install a plugin from a URL (fetches the manifest)."""
        try:
            from urllib.request import urlopen
            with urlopen(url, timeout=30) as resp:
                manifest = json.loads(resp.read().decode("utf-8"))
            return self.install(manifest)
        except Exception as e:
            return {"ok": False, "error": f"Failed to fetch: {e}"}

    def uninstall(self, name: str) -> dict:
        """Uninstall a plugin."""
        if name not in self._plugins:
            return {"ok": False, "error": "not found"}
        del self._plugins[name]
        # Remove from all agents
        for aid in self._agent_plugins:
            self._agent_plugins[aid] = [p for p in self._agent_plugins[aid] if p != name]
        return {"ok": True}

    # ── Enable / Disable ──

    def enable(self, name: str, config: dict = None) -> dict:
        """Enable a plugin (optionally with config)."""
        plugin = self._plugins.get(name)
        if not plugin:
            return {"ok": False, "error": "not found"}
        plugin.enabled = True
        if config:
            plugin.config = config
        return {"ok": True, "plugin": name, "enabled": True}

    def disable(self, name: str) -> dict:
        plugin = self._plugins.get(name)
        if not plugin:
            return {"ok": False, "error": "not found"}
        plugin.enabled = False
        return {"ok": True, "plugin": name, "enabled": False}

    def configure(self, name: str, config: dict) -> dict:
        """Update plugin configuration."""
        plugin = self._plugins.get(name)
        if not plugin:
            return {"ok": False, "error": "not found"}
        plugin.config.update(config)
        return {"ok": True, "config": plugin.config}

    # ── Agent assignment ──

    def assign_to_agent(self, agent_id: str, plugin_name: str) -> dict:
        """Assign a plugin to an agent."""
        if plugin_name not in self._plugins:
            return {"ok": False, "error": f"Plugin '{plugin_name}' not found"}
        if agent_id not in self._agent_plugins:
            self._agent_plugins[agent_id] = []
        if plugin_name not in self._agent_plugins[agent_id]:
            self._agent_plugins[agent_id].append(plugin_name)
            self._plugins[plugin_name].install_count += 1
        return {"ok": True}

    def unassign_from_agent(self, agent_id: str, plugin_name: str) -> dict:
        if agent_id in self._agent_plugins:
            self._agent_plugins[agent_id] = [p for p in self._agent_plugins[agent_id] if p != plugin_name]
            if plugin_name in self._plugins:
                self._plugins[plugin_name].install_count = max(0, self._plugins[plugin_name].install_count - 1)
        return {"ok": True}

    def agent_plugins(self, agent_id: str) -> List[dict]:
        """Get plugins assigned to an agent."""
        names = self._agent_plugins.get(agent_id, [])
        return [self._plugins[n].to_dict() for n in names if n in self._plugins]

    # ── Tool execution ──

    def register_tool_handler(self, tool_name: str, handler: Callable):
        """Register a custom handler for a plugin tool."""
        self._tool_handlers[tool_name] = handler

    def call_tool(self, plugin_name: str, tool_name: str, params: dict) -> dict:
        """Call a plugin tool."""
        plugin = self._plugins.get(plugin_name)
        if not plugin:
            return {"ok": False, "error": "plugin not found"}
        if not plugin.enabled:
            return {"ok": False, "error": "plugin not enabled"}

        tool = next((t for t in plugin.tools if t["name"] == tool_name), None)
        if not tool:
            return {"ok": False, "error": f"tool '{tool_name}' not found in plugin '{plugin_name}'"}

        plugin.invoke_count += 1
        plugin.last_invoked = datetime.now(timezone.utc).isoformat()

        # Check for custom handler
        handler = self._tool_handlers.get(tool_name)
        if handler:
            try:
                return handler(params, plugin.config)
            except Exception as e:
                plugin.error_count += 1
                return {"ok": False, "error": str(e)}

        # Default: return a structured response based on tool name
        return {"ok": True, "data": f"[Plugin '{plugin_name}' tool '{tool_name}' called with {json.dumps(params)[:200]}]"}

    # ── Hook execution ──

    def run_hooks(self, hook_name: str, agent_id: str, context: dict) -> List[dict]:
        """Run all hooks of a given type for an agent."""
        results = []
        # Global enabled plugins
        for name, plugin in self._plugins.items():
            if not plugin.enabled:
                continue
            if hook_name not in plugin.hooks:
                continue
            # Check if assigned to this agent (or global)
            agent_assigned = name in self._agent_plugins.get(agent_id, [])
            if not agent_assigned and plugin.install_count > 0:
                continue  # Plugin is agent-specific, skip

            hook_config = plugin.hooks[hook_name]
            plugin.invoke_count += 1
            plugin.last_invoked = datetime.now(timezone.utc).isoformat()

            results.append({
                "plugin": name, "hook": hook_name,
                "action": hook_config.get("action", ""),
                "config": hook_config,
                "plugin_config": plugin.config,
            })
        return results

    # ── Queries ──

    def list_plugins(self, category: str = None, enabled_only: bool = False) -> List[dict]:
        """List all plugins."""
        plugins = list(self._plugins.values())
        if category:
            plugins = [p for p in plugins if p.category == category]
        if enabled_only:
            plugins = [p for p in plugins if p.enabled]
        return [p.to_dict() for p in plugins]

    def get_plugin(self, name: str) -> Optional[dict]:
        p = self._plugins.get(name)
        return p.to_dict() if p else None

    def available_tools(self, agent_id: str = None) -> List[dict]:
        """List all tools available (globally or for a specific agent)."""
        tools = []
        for name, plugin in self._plugins.items():
            if not plugin.enabled:
                continue
            if agent_id:
                if name not in self._agent_plugins.get(agent_id, []):
                    continue
            for tool in plugin.tools:
                tools.append({
                    "plugin": name, "tool": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", []),
                })
        return tools

    def categories(self) -> List[dict]:
        """List plugin categories with counts."""
        cats = {}
        for p in self._plugins.values():
            if p.category not in cats:
                cats[p.category] = {"category": p.category, "count": 0, "enabled": 0}
            cats[p.category]["count"] += 1
            if p.enabled:
                cats[p.category]["enabled"] += 1
        return list(cats.values())

    def stats(self) -> dict:
        total = len(self._plugins)
        enabled = sum(1 for p in self._plugins.values() if p.enabled)
        total_invokes = sum(p.invoke_count for p in self._plugins.values())
        total_errors = sum(p.error_count for p in self._plugins.values())
        return {
            "total": total, "enabled": enabled, "disabled": total - enabled,
            "total_invocations": total_invokes, "total_errors": total_errors,
            "categories": self.categories(),
        }


# Singleton
plugin_manager = PluginManager()
