"""
CORTEX WebSocket Streaming
═══════════════════════════
Real-time streaming of agent execution, daemon status, and events.

Provides:
  - Live run trace streaming (steps appear as they execute)
  - Daemon status updates (cycle counts, errors, next-run countdown)
  - Event stream (agent status changes, messages, alerts)

Usage:
    # In cortex.py:
    from ws_streaming import StreamManager, ws_endpoint
    stream_mgr = StreamManager()

    # Client connects: ws://localhost:3000/ws/stream?agent_id=my-agent
"""

import json
import time
import asyncio
import threading
from datetime import datetime, timezone
from typing import Dict, Set, List, Optional, Any
from collections import defaultdict


class StreamManager:
    """Manages WebSocket connections and broadcasts events."""

    def __init__(self):
        self._connections: Dict[str, Set] = defaultdict(set)  # channel -> set of websocket objects
        self._event_buffer: List[dict] = []
        self._max_buffer = 500
        self._lock = threading.Lock()

    def register(self, channel: str, ws):
        """Register a WebSocket connection for a channel."""
        with self._lock:
            self._connections[channel].add(ws)

    def unregister(self, channel: str, ws):
        """Unregister a WebSocket connection."""
        with self._lock:
            self._connections[channel].discard(ws)
            if not self._connections[channel]:
                del self._connections[channel]

    def connection_count(self, channel: str = None) -> int:
        with self._lock:
            if channel:
                return len(self._connections.get(channel, set()))
            return sum(len(s) for s in self._connections.values())

    async def broadcast(self, channel: str, event: dict):
        """Broadcast an event to all connections on a channel."""
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        event_json = json.dumps(event)

        with self._lock:
            self._event_buffer.append(event)
            if len(self._event_buffer) > self._max_buffer:
                self._event_buffer = self._event_buffer[-self._max_buffer:]
            connections = list(self._connections.get(channel, set()))

        dead = []
        for ws in connections:
            try:
                await ws.send_text(event_json)
            except Exception:
                dead.append(ws)

        # Clean up dead connections
        if dead:
            with self._lock:
                for ws in dead:
                    self._connections[channel].discard(ws)

    def broadcast_sync(self, channel: str, event: dict):
        """Synchronous broadcast — creates an async task if there's a running loop."""
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        event_json = json.dumps(event)

        with self._lock:
            self._event_buffer.append(event)
            if len(self._event_buffer) > self._max_buffer:
                self._event_buffer = self._event_buffer[-self._max_buffer:]
            connections = list(self._connections.get(channel, set()))

        for ws in connections:
            try:
                # Try to get the running event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(ws.send_text(event_json), loop)
                else:
                    loop.run_until_complete(ws.send_text(event_json))
            except RuntimeError:
                # No event loop — skip (connection will be cleaned up next time)
                pass
            except Exception:
                pass

    # ── Convenience methods for common event types ──

    def emit_run_step(self, agent_id: str, run_id: str, step: dict):
        """Emit a run execution step."""
        self.broadcast_sync(f"agent:{agent_id}", {
            "type": "run.step", "agent_id": agent_id,
            "run_id": run_id, "step": step,
        })
        self.broadcast_sync("global", {
            "type": "run.step", "agent_id": agent_id,
            "run_id": run_id, "step": step,
        })

    def emit_run_complete(self, agent_id: str, run_id: str, outcome: str, summary: str = ""):
        """Emit run completion."""
        self.broadcast_sync(f"agent:{agent_id}", {
            "type": "run.complete", "agent_id": agent_id,
            "run_id": run_id, "outcome": outcome, "summary": summary[:500],
        })
        self.broadcast_sync("global", {
            "type": "run.complete", "agent_id": agent_id,
            "run_id": run_id, "outcome": outcome,
        })

    def emit_daemon_tick(self, agent_id: str, cycle: int, next_in: int, errors: int):
        """Emit daemon cycle update."""
        self.broadcast_sync(f"agent:{agent_id}", {
            "type": "daemon.tick", "agent_id": agent_id,
            "cycle": cycle, "next_run_in_seconds": next_in,
            "consecutive_errors": errors,
        })

    def emit_agent_status(self, agent_id: str, status: str, user_id: str = None):
        """Emit agent status change."""
        event = {"type": "agent.status", "agent_id": agent_id, "status": status}
        if user_id:
            event["changed_by"] = user_id
        self.broadcast_sync(f"agent:{agent_id}", event)
        self.broadcast_sync("global", event)

    def emit_message(self, from_agent: str, to_agent: str, msg_type: str, preview: str = ""):
        """Emit agent-to-agent message notification."""
        self.broadcast_sync(f"agent:{to_agent}", {
            "type": "message.received", "from": from_agent,
            "to": to_agent, "msg_type": msg_type, "preview": preview[:200],
        })
        self.broadcast_sync("global", {
            "type": "message.sent", "from": from_agent,
            "to": to_agent, "msg_type": msg_type,
        })

    def emit_alert(self, agent_id: str, level: str, message: str):
        """Emit an alert (error, warning, info)."""
        self.broadcast_sync("global", {
            "type": "alert", "agent_id": agent_id,
            "level": level, "message": message[:500],
        })

    def recent_events(self, channel: str = None, limit: int = 50) -> List[dict]:
        """Get recent events from the buffer."""
        with self._lock:
            events = self._event_buffer
        if channel:
            # Filter by channel relevance (approximate — events don't store their channel)
            pass
        return events[-limit:]

    def active_channels(self) -> dict:
        """List active channels and their connection counts."""
        with self._lock:
            return {ch: len(conns) for ch, conns in self._connections.items() if conns}


# Singleton
stream_manager = StreamManager()
