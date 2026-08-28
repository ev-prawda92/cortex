"""
Cortex Agent Daemon — Continuous Execution Engine
==================================================
Runs agents in the background on configurable intervals.

When an agent's status is "running", the daemon executes it on a loop:
  1. Read the agent's standing instruction (what it should do each cycle)
  2. Call the LLM provider with the agent's config
  3. Record the run (feeds into CAR automatically)
  4. Sleep for the configured interval
  5. Repeat until the agent is stopped or paused

The daemon runs in a background thread alongside the FastAPI server.
It checks every 10 seconds for agents whose next run is due.

Design principles:
  - Agents define WHAT they do (standing instruction) and HOW OFTEN (interval)
  - Start/Stop/Pause controls the loop, not individual runs
  - Every cycle produces a run record that feeds the Adaptive Runtime
  - Errors don't kill the loop — they're logged and the agent retries next cycle
  - Thread-safe: daemon reads DB state each tick, never caches stale config
"""

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, Optional

# These will be set by cortex.py at import time
_db_session_factory = None
_agent_model = None
_run_saver = None
_get_key = None
_get_model = None
_settings_getter = None
_providers = None
_log_event = None
_car_engine = None


def configure(session_factory, agent_model, run_saver, get_key_fn, get_model_fn,
              settings_getter, providers_mod, log_event_fn, car_engine):
    """Called once by cortex.py on startup to inject dependencies."""
    global _db_session_factory, _agent_model, _run_saver, _get_key
    global _get_model, _settings_getter, _providers, _log_event, _car_engine
    _db_session_factory = session_factory
    _agent_model = agent_model
    _run_saver = run_saver
    _get_key = get_key_fn
    _get_model = get_model_fn
    _settings_getter = settings_getter
    _providers = providers_mod
    _log_event = log_event_fn
    _car_engine = car_engine


# ── Agent state tracking ──────────────────────────────────────────────

class AgentRunState:
    """Tracks the daemon's view of one agent's continuous execution."""
    __slots__ = ("agent_id", "last_run_at", "next_run_at", "cycle_count",
                 "consecutive_errors", "paused", "last_error", "last_skip")

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.last_run_at: Optional[float] = None
        self.next_run_at: float = 0.0  # run immediately on first tick
        self.cycle_count: int = 0
        self.consecutive_errors: int = 0
        self.paused: bool = False
        self.last_error: str = ""
        self.last_skip: str = ""   # why the last tick was skipped, if it was


_agent_states: Dict[str, AgentRunState] = {}
_daemon_running = False
_daemon_thread: Optional[threading.Thread] = None


# ── Core execution ────────────────────────────────────────────────────

def _execute_cycle(agent_id: str, agent_dict: dict) -> dict:
    """Run one cycle of an agent. Returns the run record."""
    cfg = agent_dict.get("config", {})
    model_cfg = cfg.get("model", {})
    provider = model_cfg.get("provider", "anthropic")
    key = _get_key(provider)

    # A skip is not a run. Returning skipped=True keeps these out of the runs
    # table entirely: a missing key or a missing instruction is a setup problem,
    # and recording it as a run inflates run counts and drags down success rates
    # for a config version that never actually executed.
    if not key:
        return {"ok": False, "skipped": True,
                "skip_reason": f"No API key configured for {provider}",
                "config_version": agent_dict.get("version", 1)}

    # Build the standing instruction
    standing = cfg.get("standing_instruction", "").strip()
    if not standing:
        standing = agent_dict.get("description", "").strip()
    if not standing:
        # Previously this invented "Execute your primary function and report
        # results" and billed a model call for it every interval, forever.
        # An agent nobody has given an instruction has nothing to do.
        return {"ok": False, "skipped": True,
                "skip_reason": "No standing instruction or description — nothing to run",
                "config_version": agent_dict.get("version", 1)}

    # Add cycle context
    state = _agent_states.get(agent_id)
    cycle_num = state.cycle_count + 1 if state else 1
    cycle_context = f"\n\n[Continuous mode — cycle #{cycle_num}. Report what you find or do. Be concise.]"

    behavior = cfg.get("behavior", {})
    tools_cfg = cfg.get("tools", [])
    execution = cfg.get("execution", {})

    system_parts = [f"You are {agent_dict.get('name', agent_id)}."]
    if agent_dict.get("description"):
        system_parts.append(agent_dict["description"])
    system_parts.append(f"\nOPERATING CONFIG (live from Cortex):")
    system_parts.append(f"- mode: continuous (always-on)")
    system_parts.append(f"- confidence threshold: {behavior.get('confidence_threshold', 0.75)}")
    if tools_cfg:
        system_parts.append(f"- available tools: {', '.join(t['name'] for t in tools_cfg)}")
    system = "\n".join(system_parts)

    tools = []
    for t in tools_cfg:
        tools.append({
            "name": t["name"], "description": t.get("description", ""),
            "input_schema": {"type": "object", "properties": {
                p: {"type": "string"} for p in t.get("parameters", [])
            }}
        })

    def _tool_handler(name, input_data):
        return f"[Tool '{name}' called with {json.dumps(input_data)}. Placeholder response.]"

    model_name = model_cfg.get("model_name") or _get_model(provider)

    res = _providers.run_tool_loop(
        provider=provider, api_key=key,
        model=model_name,
        system=system, tools=tools,
        user_message=standing + cycle_context,
        process_tool_call=_tool_handler,
        max_iterations=execution.get("max_retries", 3) + 1,
    )

    run_end = datetime.now(timezone.utc).isoformat()

    if res["ok"]:
        outcome = "ESCALATED" if res.get("escalated") else "COMPLETED"
    else:
        outcome = "ERROR"

    return {
        "ok": res["ok"],
        "outcome": outcome,
        "claim": f"[continuous cycle #{cycle_num}] {standing[:200]}",
        "provider": provider,
        "model": model_name,
        "task_type": cfg.get("task_type", "general"),
        "steps_used": res.get("steps_used", 0),
        "config_version": agent_dict.get("version", 1),
        "trace": res.get("trace", []),
        "input_tokens": res.get("input_tokens", 0),
        "output_tokens": res.get("output_tokens", 0),
        "total_tokens": res.get("total_tokens", 0),
        "started_at": state.last_run_at if state else run_end,
        "finished_at": run_end,
        "detail": {
            "summary": (res.get("final_text") or "")[:1200],
            "reason": res.get("error", ""),
            "citations": [],
            "route_to": None,
        },
        "error": res.get("error", ""),
    }


def _tick():
    """One daemon tick: check all running agents, execute any that are due."""
    if not _db_session_factory:
        return

    db = _db_session_factory()
    try:
        running_agents = db.query(_agent_model).filter(
            _agent_model.status == "running",
            _agent_model.live == True,
        ).all()

        now = time.time()

        for a_row in running_agents:
            aid = a_row.id
            cfg = a_row.config or {}
            interval = cfg.get("run_interval_seconds", 60)

            # Initialize state if new
            if aid not in _agent_states:
                _agent_states[aid] = AgentRunState(aid)

            state = _agent_states[aid]

            # Skip if paused
            if state.paused:
                continue

            # Skip if not yet due
            if now < state.next_run_at:
                continue

            # Build agent dict
            agent_dict = {
                "id": aid,
                "name": a_row.name,
                "description": a_row.description or "",
                "config": cfg,
                "version": a_row.version or 1,
                "endpoint": a_row.endpoint or {},
            }

            # Execute
            state.last_run_at = now
            try:
                result = _execute_cycle(aid, agent_dict)

                if result.get("skipped"):
                    reason = result.get("skip_reason", "skipped")
                    # Log only when the reason changes, so a permanently
                    # misconfigured agent doesn't flood the event log.
                    if state.last_skip != reason:
                        state.last_skip = reason
                        if _log_event:
                            _log_event(aid, "daemon.cycle_skipped", {"reason": reason})
                    # Not an error and not a run — just nothing to do. Check
                    # again on a slow cadence in case the setup gets fixed.
                    state.next_run_at = time.time() + max(interval, 300)
                    continue

                state.last_skip = ""
                state.cycle_count += 1
                if _log_event:
                    _log_event(aid, "daemon.cycle_start", {"cycle": state.cycle_count})

                if result.get("ok"):
                    state.consecutive_errors = 0
                    state.last_error = ""
                else:
                    state.consecutive_errors += 1
                    state.last_error = result.get("error", "unknown")

                # Save the run (this also feeds CAR)
                if _run_saver:
                    result["started_at"] = datetime.fromtimestamp(state.last_run_at, tz=timezone.utc).isoformat()
                    _run_saver(aid, result)

                if _log_event:
                    _log_event(aid, "daemon.cycle_complete", {
                        "cycle": state.cycle_count,
                        "outcome": result.get("outcome", ""),
                        "tokens": result.get("total_tokens", 0),
                    })

            except Exception as e:
                state.consecutive_errors += 1
                state.last_error = str(e)
                if _log_event:
                    _log_event(aid, "daemon.cycle_error", {
                        "cycle": state.cycle_count,
                        "error": str(e)[:500],
                    })

            # Schedule next run with backoff on consecutive errors
            backoff = min(interval * (2 ** min(state.consecutive_errors, 4)), 3600)
            effective_interval = interval if state.consecutive_errors == 0 else backoff
            state.next_run_at = time.time() + effective_interval

        # Clean up states for agents that are no longer running
        running_ids = {a.id for a in running_agents}
        for aid in list(_agent_states.keys()):
            if aid not in running_ids:
                del _agent_states[aid]

    except Exception:
        traceback.print_exc()
    finally:
        db.close()


def _daemon_loop():
    """Main daemon loop. Ticks every 10 seconds."""
    global _daemon_running
    while _daemon_running:
        try:
            _tick()
        except Exception:
            traceback.print_exc()
        time.sleep(10)


# ── Public API ────────────────────────────────────────────────────────

def start_daemon():
    """Start the background daemon thread."""
    global _daemon_running, _daemon_thread
    if _daemon_running:
        return
    _daemon_running = True
    _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True, name="cortex-daemon")
    _daemon_thread.start()


def stop_daemon():
    """Stop the background daemon."""
    global _daemon_running
    _daemon_running = False


def pause_agent(agent_id: str):
    """Pause an agent's continuous execution without stopping it."""
    state = _agent_states.get(agent_id)
    if state:
        state.paused = True


def resume_agent(agent_id: str):
    """Resume a paused agent."""
    state = _agent_states.get(agent_id)
    if state:
        state.paused = False
        state.next_run_at = 0  # run immediately


def get_agent_daemon_state(agent_id: str) -> dict:
    """Get the daemon's view of an agent."""
    state = _agent_states.get(agent_id)
    if not state:
        return {"active": False, "agent_id": agent_id}
    now = time.time()
    return {
        "active": True,
        "agent_id": agent_id,
        "cycle_count": state.cycle_count,
        "paused": state.paused,
        "last_run_at": datetime.fromtimestamp(state.last_run_at, tz=timezone.utc).isoformat() if state.last_run_at else None,
        "next_run_in_seconds": max(0, round(state.next_run_at - now)),
        "consecutive_errors": state.consecutive_errors,
        "last_error": state.last_error,
    }


def get_all_daemon_states() -> dict:
    """Get daemon state for all tracked agents."""
    return {aid: get_agent_daemon_state(aid) for aid in _agent_states}


def is_running() -> bool:
    return _daemon_running
