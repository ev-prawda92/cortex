"""
CORTEX Agent-to-Agent Communication
════════════════════════════════════
Message passing, event bus, and workflow chaining between agents.

Agents can:
  - Send messages to other agents (async or sync)
  - Trigger another agent's execution
  - Chain into multi-agent workflows
  - Subscribe to events from other agents

Usage:
    from agent_comm import MessageBus
    bus = MessageBus()
    bus.send("agent-a", "agent-b", {"type": "task", "data": "process this"})
    bus.trigger("agent-a", "agent-b", "Summarize the PR feedback")
"""

import json
import time
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any
from collections import defaultdict


# ═══════════════════════════════════════════════════════════════
#  MESSAGE TYPES
# ═══════════════════════════════════════════════════════════════

class Message:
    """A message between agents."""
    __slots__ = ("id", "from_agent", "to_agent", "msg_type", "payload",
                 "created_at", "delivered_at", "status", "reply_to", "user_id")

    _counter = 0

    def __init__(self, from_agent: str, to_agent: str, msg_type: str,
                 payload: dict, reply_to: str = None, user_id: str = None):
        Message._counter += 1
        self.id = f"msg-{Message._counter:06d}"
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.msg_type = msg_type  # task, event, result, trigger, notification
        self.payload = payload
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.delivered_at = None
        self.status = "pending"  # pending, delivered, processed, failed
        self.reply_to = reply_to
        self.user_id = user_id

    def to_dict(self):
        return {
            "id": self.id, "from": self.from_agent, "to": self.to_agent,
            "type": self.msg_type, "payload": self.payload,
            "created_at": self.created_at, "delivered_at": self.delivered_at,
            "status": self.status, "reply_to": self.reply_to, "user_id": self.user_id,
        }


# ═══════════════════════════════════════════════════════════════
#  WORKFLOW DEFINITION
# ═══════════════════════════════════════════════════════════════

class WorkflowStep:
    """A step in a multi-agent workflow."""
    def __init__(self, agent_id: str, instruction: str, depends_on: List[str] = None,
                 condition: str = None):
        self.agent_id = agent_id
        self.instruction = instruction
        self.depends_on = depends_on or []
        self.condition = condition  # "always", "on_success", "on_error"
        self.status = "pending"
        self.result = None


class Workflow:
    """A multi-agent workflow — a DAG of agent executions."""

    _counter = 0

    def __init__(self, name: str, steps: List[dict], created_by: str = None):
        Workflow._counter += 1
        self.id = f"wf-{Workflow._counter:04d}"
        # to_dict() truncates instructions for display; keep the original list
        # verbatim so a workflow can be replayed exactly as it was authored.
        self.raw_steps = [dict(s) for s in steps]
        self.name = name
        self.created_by = created_by
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.status = "pending"  # pending, running, completed, failed
        self.steps: Dict[str, WorkflowStep] = {}
        self.results: Dict[str, Any] = {}

        for i, s in enumerate(steps):
            step_id = s.get("id", f"step-{i}")
            self.steps[step_id] = WorkflowStep(
                agent_id=s["agent_id"],
                instruction=s["instruction"],
                depends_on=s.get("depends_on", []),
                condition=s.get("condition", "always"),
            )

    def ready_steps(self) -> List[str]:
        """Get steps whose dependencies are all completed."""
        ready = []
        for sid, step in self.steps.items():
            if step.status != "pending":
                continue
            deps_met = all(
                self.steps.get(d, WorkflowStep("", "")).status == "completed"
                for d in step.depends_on
            )
            if deps_met:
                # Check condition
                if step.condition == "on_success":
                    all_ok = all(self.results.get(d, {}).get("ok", False) for d in step.depends_on)
                    if not all_ok:
                        step.status = "skipped"
                        continue
                elif step.condition == "on_error":
                    any_err = any(not self.results.get(d, {}).get("ok", True) for d in step.depends_on)
                    if not any_err:
                        step.status = "skipped"
                        continue
                ready.append(sid)
        return ready

    def complete_step(self, step_id: str, result: dict):
        if step_id in self.steps:
            self.steps[step_id].status = "completed"
            self.steps[step_id].result = result
            self.results[step_id] = result
        # Check if workflow is done
        if all(s.status in ("completed", "skipped") for s in self.steps.values()):
            self.status = "completed"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "status": self.status,
            "created_by": self.created_by, "created_at": self.created_at,
            "steps": {sid: {"agent_id": s.agent_id, "instruction": s.instruction[:200],
                            "status": s.status, "depends_on": s.depends_on,
                            "result_ok": s.result.get("ok") if s.result else None}
                      for sid, s in self.steps.items()},
            "results_summary": {sid: {"ok": r.get("ok"), "outcome": r.get("outcome", "")}
                                for sid, r in self.results.items()},
        }


# ═══════════════════════════════════════════════════════════════
#  MESSAGE BUS
# ═══════════════════════════════════════════════════════════════

class MessageBus:
    """Central message bus for agent-to-agent communication."""

    def __init__(self):
        self._queues: Dict[str, List[Message]] = defaultdict(list)
        self._history: List[Message] = []
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._workflows: Dict[str, Workflow] = {}
        self._trigger_fn: Optional[Callable] = None  # set by cortex.py to actually run agents
        self._max_history = 1000
        self._lock = threading.Lock()

    def set_trigger_fn(self, fn: Callable):
        """Set the function used to trigger agent execution. Called by cortex.py."""
        self._trigger_fn = fn

    # ── Messaging ──

    def send(self, from_agent: str, to_agent: str, payload: dict,
             msg_type: str = "task", reply_to: str = None, user_id: str = None) -> dict:
        """Send a message from one agent to another."""
        msg = Message(from_agent, to_agent, msg_type, payload, reply_to, user_id)
        with self._lock:
            self._queues[to_agent].append(msg)
            self._history.append(msg)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
        # Notify subscribers
        for fn in self._subscribers.get(to_agent, []):
            try:
                fn(msg.to_dict())
            except Exception:
                pass
        return {"ok": True, "message_id": msg.id}

    def receive(self, agent_id: str, limit: int = 10) -> List[dict]:
        """Receive pending messages for an agent."""
        with self._lock:
            msgs = self._queues.get(agent_id, [])[:limit]
            # Mark as delivered
            for m in msgs:
                m.status = "delivered"
                m.delivered_at = datetime.now(timezone.utc).isoformat()
            # Remove delivered from queue
            self._queues[agent_id] = self._queues.get(agent_id, [])[limit:]
        return [m.to_dict() for m in msgs]

    def subscribe(self, agent_id: str, callback: Callable):
        """Subscribe to messages for an agent."""
        self._subscribers[agent_id].append(callback)

    # ── Triggering ──

    def trigger(self, from_agent: str, to_agent: str, instruction: str,
                context: dict = None, user_id: str = None) -> dict:
        """Trigger another agent's execution directly."""
        # Record the trigger as a message
        self.send(from_agent, to_agent, {
            "type": "trigger", "instruction": instruction,
            "context": context or {},
        }, msg_type="trigger", user_id=user_id)

        # Actually execute if we have a trigger function
        if self._trigger_fn:
            try:
                result = self._trigger_fn(to_agent, instruction, triggered_by=from_agent, user_id=user_id)
                return {"ok": True, "triggered": to_agent, "result": result}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True, "triggered": to_agent, "note": "queued (no executor configured)"}

    # ── Broadcast ──

    def broadcast(self, from_agent: str, payload: dict, msg_type: str = "event",
                  user_id: str = None) -> dict:
        """Broadcast a message to all subscribers."""
        with self._lock:
            all_agents = set(self._subscribers.keys())
        sent = 0
        for agent_id in all_agents:
            if agent_id != from_agent:
                self.send(from_agent, agent_id, payload, msg_type, user_id=user_id)
                sent += 1
        return {"ok": True, "broadcast_to": sent}

    # ── Workflows ──

    def create_workflow(self, name: str, steps: List[dict], created_by: str = None) -> dict:
        """Create a multi-agent workflow."""
        wf = Workflow(name, steps, created_by)
        self._workflows[wf.id] = wf
        return {"ok": True, "workflow_id": wf.id, "workflow": wf.to_dict()}

    def run_workflow(self, workflow_id: str) -> dict:
        """Execute ready steps in a workflow."""
        wf = self._workflows.get(workflow_id)
        if not wf:
            return {"ok": False, "error": "workflow not found"}
        wf.status = "running"
        ready = wf.ready_steps()
        results = {}
        for step_id in ready:
            step = wf.steps[step_id]
            step.status = "running"
            # Build instruction with context from dependencies
            instruction = step.instruction
            for dep_id in step.depends_on:
                dep_result = wf.results.get(dep_id, {})
                if dep_result:
                    summary = dep_result.get("detail", {}).get("summary", "")[:500]
                    instruction += f"\n\n[Context from {dep_id}]: {summary}"

            if self._trigger_fn:
                try:
                    result = self._trigger_fn(step.agent_id, instruction,
                                              triggered_by=f"workflow:{workflow_id}")
                    wf.complete_step(step_id, result)
                    results[step_id] = {"ok": True, "outcome": result.get("outcome", "")}
                except Exception as e:
                    wf.complete_step(step_id, {"ok": False, "error": str(e)})
                    results[step_id] = {"ok": False, "error": str(e)}
            else:
                wf.complete_step(step_id, {"ok": True, "note": "no executor"})
                results[step_id] = {"ok": True, "note": "queued"}

        return {"ok": True, "workflow": wf.to_dict(), "executed": results,
                "remaining": len(wf.ready_steps())}

    def get_workflow(self, workflow_id: str) -> dict:
        wf = self._workflows.get(workflow_id)
        return wf.to_dict() if wf else {"error": "not found"}

    def get_definition(self, workflow_id: str) -> List[dict]:
        """The original step list a workflow was created from, untruncated."""
        wf = self._workflows.get(workflow_id)
        return list(getattr(wf, "raw_steps", [])) if wf else []

    def list_workflows(self) -> List[dict]:
        return [wf.to_dict() for wf in self._workflows.values()]

    # ── History ──

    def history(self, agent_id: str = None, limit: int = 50) -> List[dict]:
        """Get message history, optionally filtered by agent."""
        with self._lock:
            msgs = self._history
            if agent_id:
                msgs = [m for m in msgs if m.from_agent == agent_id or m.to_agent == agent_id]
            return [m.to_dict() for m in msgs[-limit:]]

    def queue_depth(self, agent_id: str = None) -> dict:
        """Get queue depths."""
        with self._lock:
            if agent_id:
                return {"agent_id": agent_id, "depth": len(self._queues.get(agent_id, []))}
            return {aid: len(q) for aid, q in self._queues.items() if q}

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_messages": len(self._history),
                "active_queues": sum(1 for q in self._queues.values() if q),
                "total_queued": sum(len(q) for q in self._queues.values()),
                "subscribers": {aid: len(fns) for aid, fns in self._subscribers.items()},
                "workflows": len(self._workflows),
            }


# Singleton
message_bus = MessageBus()
