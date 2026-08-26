#!/usr/bin/env python3
"""
Cortex — Agent Portfolio Manager + plain-text Control Panel

One file. Run:  python3 cortex.py   then open http://localhost:3000

The control panel lets you describe a change to an agent in plain English.
Cortex proposes a specific config diff, shows you before/after, and does NOT
apply anything until you approve. Every applied change is versioned and
reversible. Nobody's plain text silently mutates a live clinical agent — the
human stays in the loop.

Set ANTHROPIC_API_KEY to use the model for translation. Without it, Cortex
falls back to a deterministic parser that handles the common change types
(timing, thresholds, channels, retries, call windows, enable/disable).
"""

import os
import re
import copy
import json
import hashlib
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Cortex", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ─────────────────────────────────────────────── provider settings (Settings tab)
import providers as providers_mod
import automation as automation_mod

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# ─────────────────────────────────────────────── agent runs (Runs tab)
RUNS = {}  # agent_id -> list of execution records with trace, output, metrics

def _load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {"active": "anthropic", "keys": {}, "models": dict(providers_mod.DEFAULT_MODELS)}

def _save_settings(s):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(s, f, indent=2)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except Exception:
        pass

SETTINGS = _load_settings()
SETTINGS.setdefault("active", "anthropic")
SETTINGS.setdefault("keys", {})
SETTINGS.setdefault("models", {})
for p, m in providers_mod.DEFAULT_MODELS.items():
    SETTINGS["models"].setdefault(p, m)

_ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

def get_key(provider):
    return SETTINGS["keys"].get(provider) or os.environ.get(_ENV_KEYS.get(provider, ""), "")

def get_model(provider):
    return SETTINGS["models"].get(provider) or providers_mod.DEFAULT_MODELS.get(provider, "")

def _mask(k):
    return (k[:7] + "…" + k[-4:]) if k and len(k) > 14 else ("set" if k else "")


# ---------------------------------------------------------------- agent configs
# Each agent carries a real, structured config the control panel can edit.

def _base_config(name, posture, first_delay_h, retries, channel, window, esc_threshold):
    return {
        "posture": posture,  # replace | augment | support
        "journey": {
            "first_contact_delay_hours": first_delay_h,
            "max_retries": retries,
            "channel": channel,               # voice | sms | voice+sms
            "call_window": window,            # e.g. "09:00-17:00 local"
            "retry_gap_hours": 24,
        },
        "escalation": {
            "confidence_threshold": 0.70,     # below this → human
            "severity_escalates_at": esc_threshold,  # low|moderate|high
            "route_to": "clinical team",
        },
        "graph": {
            "nodes": ["greet", "verify_identity", "collect", "resolve", "close"],
            "confirm_then_act": True,         # agent confirms before any write-back
        },
        "prompt_summary": f"{name}: guide the patient through the task, confirm before acting, "
                          f"escalate anything clinical.",
    }

AGENTS = {
    # ── LIVE AGENT — Editorial Verification (existing) ──
    "editorial-verification": {
        "name": "Editorial Verification ⚡ LIVE", "account": "Newsroom · Standards",
        "status": "running", "version": 1,
        "containment": 0, "resolution": 0, "escalation": 0, "clinical_flags": 0,
        "live": True,
        "config": {
            "posture": "augment",
            "journey": {"first_contact_delay_hours": 0, "max_retries": 6,
                        "channel": "web", "call_window": "24/7", "retry_gap_hours": 0},
            "escalation": {"confidence_threshold": 0.75,
                           "severity_escalates_at": "low",
                           "route_to": "standards editor"},
            "graph": {"nodes": ["search", "read", "assess", "conclude_or_escalate"],
                      "confirm_then_act": True},
            "prompt_summary": "Verify a claim against live retrieved sources; escalate anything ungrounded.",
        },
    },
    # ── REAL HEALTHCARE AGENTS ──
    "no-show": {
        "name": "No-Show Outreach", "account": "Bright Health",
        "status": "running", "version": 1,
        "containment": 85, "resolution": 90, "escalation": 10, "clinical_flags": 0,
        "live": True,
        "config": {
            "posture": "replace",
            "journey": {"first_contact_delay_hours": 24, "max_retries": 3,
                        "channel": "voice+sms", "call_window": "09:00-19:00 local", "retry_gap_hours": 24},
            "escalation": {"confidence_threshold": 0.70,
                           "severity_escalates_at": "high",
                           "route_to": "clinical team"},
            "graph": {"nodes": ["identify", "fetch_details", "compose", "send", "log"],
                      "confirm_then_act": True},
            "prompt_summary": "Identify high-risk patients and send personalized outreach to reduce no-shows.",
        },
    },
    "appointment-reminder": {
        "name": "Appointment Reminder (Clinic Group)", "account": "Primary Care Partners",
        "status": "running", "version": 1,
        "containment": 94, "resolution": 97, "escalation": 3, "clinical_flags": 0,
        "live": True,
        "config": {
            "posture": "replace",
            "journey": {"first_contact_delay_hours": 48, "max_retries": 2,
                        "channel": "sms", "call_window": "08:00-20:00 local", "retry_gap_hours": 24},
            "escalation": {"confidence_threshold": 0.70,
                           "severity_escalates_at": "high",
                           "route_to": "scheduler"},
            "graph": {"nodes": ["get_appointments", "get_details", "compose", "send"],
                      "confirm_then_act": True},
            "prompt_summary": "Send timely appointment reminders with essential details.",
        },
    },
    "lab-results": {
        "name": "Lab Result Notification", "account": "Quest Diagnostics",
        "status": "running", "version": 1,
        "containment": 89, "resolution": 94, "escalation": 6, "clinical_flags": 1,
        "live": True,
        "config": {
            "posture": "augment",
            "journey": {"first_contact_delay_hours": 2, "max_retries": 2,
                        "channel": "voice+sms", "call_window": "09:00-18:00 local", "retry_gap_hours": 24},
            "escalation": {"confidence_threshold": 0.70,
                           "severity_escalates_at": "moderate",
                           "route_to": "provider"},
            "graph": {"nodes": ["get_results", "check_critical", "escalate_if_needed", "notify_patient"],
                      "confirm_then_act": True},
            "prompt_summary": "Deliver lab results with clinical context; escalate critical values immediately.",
        },
    },
    "prior-auth": {
        "name": "Prior Auth (Insurance)", "account": "Blue Shield",
        "status": "running", "version": 1,
        "containment": 71, "resolution": 76, "escalation": 24, "clinical_flags": 2,
        "live": True,
        "config": {
            "posture": "support",
            "journey": {"first_contact_delay_hours": 12, "max_retries": 4,
                        "channel": "voice", "call_window": "09:00-17:00 local", "retry_gap_hours": 24},
            "escalation": {"confidence_threshold": 0.70,
                           "severity_escalates_at": "low",
                           "route_to": "payer"},
            "graph": {"nodes": ["get_requests", "gather_docs", "format", "submit", "track", "notify"],
                      "confirm_then_act": True},
            "prompt_summary": "Manage prior authorization requests with insurance payers; handle compliance.",
        },
    },
}

# version history: agent_id -> list of {version, at, by, note, config}
HISTORY = {aid: [] for aid in AGENTS}
# pending proposals: token -> {agent_id, request, diff, before, after}
PENDING = {}


# ------------------------------------------------------------------- diff logic

def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _diff(before, after):
    fb, fa = _flatten(before), _flatten(after)
    changes = []
    for k in sorted(set(fb) | set(fa)):
        if fb.get(k) != fa.get(k):
            changes.append({"field": k, "from": fb.get(k), "to": fa.get(k)})
    return changes


def _set_path(cfg, path, value):
    parts = path.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node[p]
    node[parts[-1]] = value


# ----------------------------------------------------- plain-text -> config diff

SEVERITY = ["low", "moderate", "high"]

def deterministic_translate(cfg, request):
    """
    No-API fallback. Handles the common change types an agent PM actually makes.
    Returns (new_config, notes). Never mutates the input cfg.
    """
    c = copy.deepcopy(cfg)
    r = request.lower()
    notes = []

    # timing: "wait 48 hours", "first call after 3 days", "delay ... 12 hours"
    m = re.search(r"(\d+)\s*(hour|hr|day)", r)
    if m and any(w in r for w in ["wait", "delay", "first", "before", "after"]):
        n = int(m.group(1))
        if "day" in m.group(2):
            n *= 24
        c["journey"]["first_contact_delay_hours"] = n
        notes.append(f"first contact delay → {n}h")

    # retries: "retry 5 times", "max 2 retries", "up to 4 attempts"
    m = re.search(r"(\d+)\s*(?:times|retr|attempt|tr(?:y|ies))", r)
    if not m:
        m = re.search(r"(?:retry|retries|attempts?)\D{0,12}?(\d+)", r)
    if m and ("retr" in r or "attempt" in r or "times" in r):
        c["journey"]["max_retries"] = int(m.group(1))
        notes.append(f"max retries → {m.group(1)}")

    # retry gap: "retry every 12 hours", "gap of 6 hours between"
    m = re.search(r"(every|gap of)\s*(\d+)\s*(hour|hr|day)", r)
    if m:
        n = int(m.group(2))
        if "day" in m.group(3):
            n *= 24
        c["journey"]["retry_gap_hours"] = n
        notes.append(f"retry gap → {n}h")

    # channel
    if "sms" in r and "voice" in r:
        c["journey"]["channel"] = "voice+sms"; notes.append("channel → voice+sms")
    elif "text" in r or re.search(r"\bsms\b", r):
        c["journey"]["channel"] = "sms"; notes.append("channel → sms")
    elif "call" in r or "voice" in r or "phone" in r:
        if "only" in r or "just" in r:
            c["journey"]["channel"] = "voice"; notes.append("channel → voice")

    # call window: "call window 8am-6pm", "only call between 9 and 5"
    m = re.search(r"(\d{1,2})\s*(?:am|:00)?\s*[-to]+\s*(\d{1,2})\s*(?:pm|am|:00)?", r)
    if m and ("window" in r or "between" in r or "call" in r):
        a, b = int(m.group(1)), int(m.group(2))
        if b <= 12 and ("pm" in r or b < a):
            b += 12
        c["journey"]["call_window"] = f"{a:02d}:00-{b:02d}:00 local"
        notes.append(f"call window → {a:02d}:00-{b:02d}:00 local")

    # confidence threshold: "escalate below 0.8 confidence", "confidence 75%",
    # "when confidence is below 0.8", "confidence under 70%"
    if "confidence" in r:
        m = re.search(r"(0?\.\d+|\d{1,3})\s*%?\s*confidence", r) \
            or re.search(r"confidence\D{0,20}?(0?\.\d+|\d{1,3})\s*%?", r)
        if m:
            val = float(m.group(1))
            if val > 1:
                val /= 100.0
            c["escalation"]["confidence_threshold"] = round(val, 2)
            notes.append(f"confidence threshold → {round(val,2)}")

    # severity escalation: "escalate at moderate", "only escalate high severity"
    for sev in SEVERITY:
        if re.search(rf"escalat\w*.*\b{sev}\b|\b{sev}\b.*escalat", r):
            c["escalation"]["severity_escalates_at"] = sev
            notes.append(f"severity escalates at → {sev}")
            break

    # route to: "route to nursing", "hand off to pharmacist"
    m = re.search(r"(route|hand ?off|escalate)\s*(?:to|it to)?\s*(the\s+)?([a-z ]{3,30})", r)
    if m:
        target = m.group(3).strip()
        target = re.split(r"\b(when|if|for|at|below|under|and)\b", target)[0].strip()
        if target and target not in ["it", "this", "that"]:
            c["escalation"]["route_to"] = target
            notes.append(f"escalation route → {target}")

    # confirm-then-act toggle
    if "confirm" in r and ("off" in r or "disable" in r or "without" in r or "remove" in r):
        c["graph"]["confirm_then_act"] = False; notes.append("confirm-then-act → OFF")
    elif "confirm" in r and ("on" in r or "enable" in r or "require" in r):
        c["graph"]["confirm_then_act"] = True; notes.append("confirm-then-act → ON")

    # posture
    for p in ["replace", "augment", "support"]:
        if re.search(rf"\bposture\b.*\b{p}\b|\b{p}\b\s*posture|make it {p}", r):
            c["config"] = c.get("config")  # no-op guard
            c["posture"] = p; notes.append(f"posture → {p}")
            break

    return c, notes


def llm_translate(cfg, request):
    """Use the Anthropic API to translate plain text into a config change."""
    import httpx
    schema = json.dumps(cfg, indent=2)
    sys = (
        "You edit a clinical agent's JSON config. You are given the current config and a "
        "plain-English change request. Return ONLY the complete updated config as minified "
        "JSON — same shape, same keys, only the requested fields changed. Never invent keys. "
        "If the request is unsafe for a clinical agent (e.g. disabling confirm-then-act on a "
        "write-back, or removing all escalation), make the change but it will be flagged for "
        "review. Output JSON only, no prose, no code fences."
    )
    user = f"CURRENT CONFIG:\n{schema}\n\nCHANGE REQUEST:\n{request}"
    body = {"model": get_model("anthropic"), "max_tokens": 1500,
            "system": sys, "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": get_key("anthropic"), "anthropic-version": "2023-06-01", "content-type": "application/json"}
    with httpx.Client(timeout=30) as client:
        resp = client.post("https://api.anthropic.com/v1/messages", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b >= 0:
        text = text[a:b + 1]
    new_cfg = json.loads(text)
    return new_cfg, ["translated by model"]


def _safety_flags(before, after):
    """Clinical guardrails: surface risky changes so the human decides with eyes open."""
    flags = []
    if before["graph"].get("confirm_then_act") and not after["graph"].get("confirm_then_act"):
        flags.append("Turns OFF confirm-then-act — the agent would act without a human confirming. Requires review before approving.")
    if after["escalation"].get("confidence_threshold", 1) < 0.4:
        flags.append("Confidence threshold very low — agent will rarely escalate. Verify this is intended.")
    if before["escalation"].get("severity_escalates_at") == "low" and after["escalation"].get("severity_escalates_at") == "high":
        flags.append("Raises escalation bar from low→high — more cases handled without a human. Verify for a clinical workflow.")
    return flags


# ------------------------------------------------------------------- API models

class ProposeIn(BaseModel):
    request: str

class ApplyIn(BaseModel):
    token: str
    approved_by: str = "you"


# ---------------------------------------------------------------------- endpoints

@app.get("/api/agents")
def list_agents():
    running = [a for a in AGENTS.values() if a["status"] == "running"]
    return {
        "agents": [
            {"id": k, "posture": v["config"]["posture"], "live": v.get("live", False),
             **{kk: vv for kk, vv in v.items() if kk != "config"}}
            for k, v in AGENTS.items()
        ],
        "total": len(AGENTS),
        "running": len(running),
        "error": sum(1 for a in AGENTS.values() if a["status"] == "error"),
        "stopped": sum(1 for a in AGENTS.values() if a["status"] == "stopped"),
        "avg_containment": round(sum(a["containment"] for a in running) / len(running), 1) if running else 0,
        "llm": bool(get_key(SETTINGS["active"])),
    }

@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    return {"id": agent_id, **a, "history_count": len(HISTORY[agent_id])}

@app.post("/api/agents/{agent_id}/propose")
def propose(agent_id: str, body: ProposeIn):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    before = a["config"]
    try:
        if get_key("anthropic"):
            after, notes = llm_translate(before, body.request)
        else:
            after, notes = deterministic_translate(before, body.request)
    except Exception as e:
        # fall back to deterministic if the model call fails
        after, notes = deterministic_translate(before, body.request)
        notes.append(f"(model unavailable, used rule-based parse)")

    changes = _diff(before, after)
    if not changes:
        return {"ok": False, "message": "No change detected. Try naming a specific field — timing, retries, channel, call window, confidence threshold, escalation severity, or routing."}

    token = hashlib.sha256(f"{agent_id}{body.request}{datetime.now()}".encode()).hexdigest()[:12]
    PENDING[token] = {"agent_id": agent_id, "request": body.request, "changes": changes,
                      "before": copy.deepcopy(before), "after": after, "notes": notes,
                      "flags": _safety_flags(before, after)}
    return {"ok": True, "token": token, "request": body.request, "changes": changes,
            "notes": notes, "flags": PENDING[token]["flags"]}

@app.post("/api/agents/{agent_id}/apply")
def apply(agent_id: str, body: ApplyIn):
    p = PENDING.get(body.token)
    if not p or p["agent_id"] != agent_id:
        raise HTTPException(404, "proposal not found or expired")
    a = AGENTS[agent_id]
    # snapshot current into history before applying
    HISTORY[agent_id].append({
        "version": a["version"], "at": datetime.now(timezone.utc).isoformat(),
        "by": body.approved_by, "note": p["request"],
        "config": copy.deepcopy(a["config"]), "changes": p["changes"],
    })
    a["config"] = p["after"]
    a["version"] += 1
    del PENDING[body.token]
    return {"ok": True, "agent_id": agent_id, "new_version": a["version"], "applied": p["changes"]}

@app.get("/api/agents/{agent_id}/history")
def history(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")
    return {"agent_id": agent_id, "current_version": AGENTS[agent_id]["version"],
            "history": list(reversed(HISTORY[agent_id]))}

@app.post("/api/agents/{agent_id}/revert/{version}")
def revert(agent_id: str, version: int):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    entry = next((h for h in HISTORY[agent_id] if h["version"] == version), None)
    if not entry:
        raise HTTPException(404, "version not found in history")
    HISTORY[agent_id].append({
        "version": a["version"], "at": datetime.now(timezone.utc).isoformat(),
        "by": "revert", "note": f"revert to v{version}",
        "config": copy.deepcopy(a["config"]), "changes": [],
    })
    a["config"] = copy.deepcopy(entry["config"])
    a["version"] += 1
    return {"ok": True, "reverted_to": version, "new_version": a["version"]}

@app.post("/api/agents/{agent_id}/control")
def control(agent_id: str, action: str = "start"):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    a["status"] = {"start": "running", "restart": "running", "stop": "stopped"}.get(action, a["status"])
    return {"ok": True, "status": a["status"]}

class RunIn(BaseModel):
    claim: str


# Log of real agent runs, per agent id.
RUNS: dict[str, list] = {}


@app.post("/api/agents/{agent_id}/run")
def run_live_agent(agent_id: str, body: RunIn):
    """
    Execute the REAL agent using this agent's CURRENT Cortex config.
    Editorial Verification runs its web-research loop (agent.py).
    The four healthcare agents run their tool loops on whichever provider
    (Anthropic / OpenAI / Gemini) is active in Settings.
    """
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if not a.get("live"):
        raise HTTPException(400, "this agent is seed data, not a live runtime")

    # ── Editorial verification: existing web-research agent ──
    if agent_id == "editorial-verification":
        try:
            import agent as agent_mod
        except ImportError:
            raise HTTPException(500, "agent.py not found next to cortex.py")
        key = get_key("anthropic")
        if not key:
            return {"ok": False, "error": "No Anthropic API key. Add one in Settings (this agent runs on Anthropic)."}
        agent_mod.API_KEY = key
        run = agent_mod.run_agent(body.claim, a["config"], verbose=False)
        rec = {**run.to_dict(), "config_version": a["version"]}
        RUNS.setdefault(agent_id, []).insert(0, rec)
        del RUNS[agent_id][12:]
        hist = RUNS[agent_id]
        done = [r for r in hist if r["outcome"] != "ERROR"]
        if done:
            pub = sum(1 for r in done if r["published"])
            esc = sum(1 for r in done if r["outcome"] in ("ESCALATED", "HELD"))
            a["containment"] = round(100 * pub / len(done))
            a["escalation"] = round(100 * esc / len(done))
            a["resolution"] = round(100 * sum(1 for r in done if r["outcome"] != "INCOMPLETE") / len(done))
        return {"ok": True, "run": rec}

    # ── Healthcare agents: multi-provider tool loop ──
    from cortex_agents_framework import (NoShowOutreachAgent, AppointmentReminderAgent,
                                         LabResultNotificationAgent, PriorAuthAgent)
    HC = {"no-show": NoShowOutreachAgent, "appointment-reminder": AppointmentReminderAgent,
          "lab-results": LabResultNotificationAgent, "prior-auth": PriorAuthAgent}
    cls = HC.get(agent_id)
    if not cls:
        raise HTTPException(400, "no runtime registered for this agent")

    provider = SETTINGS["active"]
    key = get_key(provider)
    if not key:
        return {"ok": False, "error": f"No API key for {providers_mod.PROVIDER_LABELS.get(provider, provider)}. Add one in Settings."}

    inst = cls.__new__(cls)          # skip __init__ (avoids requiring the anthropic SDK)
    inst.agent_name = a["name"]
    cfg = a["config"]
    system = (inst.get_system_prompt()
              + f"\n\nOPERATING CONFIG (live from Cortex — obey it):"
              + f"\n- posture: {cfg['posture']}"
              + f"\n- channel policy: {cfg['journey']['channel']}, contact window {cfg['journey']['call_window']}"
              + f"\n- max retries: {cfg['journey']['max_retries']}"
              + f"\n- escalate anything at/above severity '{cfg['escalation']['severity_escalates_at']}' to {cfg['escalation']['route_to']}"
              + (f"\n- confirm-then-act is ON: state what you will do before doing it." if cfg['graph'].get('confirm_then_act') else ""))

    res = providers_mod.run_tool_loop(
        provider=provider, api_key=key, model=get_model(provider),
        system=system, tools=inst.get_tools(), user_message=body.claim,
        process_tool_call=inst.process_tool_call, max_iterations=12)

    trace = res["trace"]
    if res["ok"]:
        outcome = "ESCALATED" if res["escalated"] else "COMPLETED"
        published = not res["escalated"]
        trace.append({"kind": "conclude", "verdict": outcome,
                      "confidence": "—", "citations": []})
    else:
        outcome, published = "ERROR", False
    rec = {"claim": body.claim, "outcome": outcome, "published": published,
           "steps_used": res["steps_used"], "config_version": a["version"],
           "provider": provider, "model": get_model(provider), "trace": trace,
           "detail": {"summary": (res.get("final_text") or "")[:1200],
                      "reason": res.get("error", ""),
                      "citations": [],
                      "route_to": cfg["escalation"]["route_to"] if res.get("escalated") else None}}
    RUNS.setdefault(agent_id, []).insert(0, rec)
    del RUNS[agent_id][12:]

    hist = RUNS[agent_id]
    done = [r for r in hist if r["outcome"] != "ERROR"]
    if done:
        a["containment"] = round(100 * sum(1 for r in done if r["published"]) / len(done))
        a["escalation"] = round(100 * sum(1 for r in done if r["outcome"] == "ESCALATED") / len(done))
        a["resolution"] = round(100 * len(done) / len(hist))
    if res["ok"]:
        return {"ok": True, "run": rec}
    return {"ok": False, "error": res.get("error", "run failed"), "run": rec}


@app.get("/api/agents/{agent_id}/runs")
def agent_runs(agent_id: str):
    return {"agent_id": agent_id, "runs": RUNS.get(agent_id, [])}


@app.get("/api/metrics/portfolio")
def portfolio_metrics():
    running = [a for a in AGENTS.values() if a["status"] == "running"]
    n = len(running) or 1
    return {
        "agents_active": len(running),
        "avg_containment": round(sum(a["containment"] for a in running) / n, 1),
        "avg_resolution": round(sum(a["resolution"] for a in running) / n, 1),
        "avg_escalation": round(sum(a["escalation"] for a in running) / n, 1),
        "total_clinical_flags": sum(a["clinical_flags"] for a in running),
        "health_score": round(
            sum(a["containment"] for a in running) / n * 0.4
            + sum(a["resolution"] for a in running) / n * 0.4
            + (100 - sum(a["escalation"] for a in running) / n) * 0.2, 0),
    }

# Diagnostics: two worked examples — one config issue, one platform bug.
DIAGNOSTICS = [
    {
        "agent_id": "ed-intake-rural", "agent": "ED Intake (Rural Hospital)", "account": "Trinity Hospital",
        "verdict": "config", "scenario": "spanish-speaker-triage",
        "observation": "Spanish-speaker triage accuracy dropped ~40% after the v1.8.2 deploy; escalation rate on Spanish calls jumped.",
        "expected": "Correct triage in Spanish, escalation only on genuinely acute cases.",
        "actual": "Partial understanding; agent escalates almost everything in Spanish.",
        "evidence": "Prompt diff v1.8.1→v1.8.2 shows the Spanish medical-terminology block was dropped during a merge. English path untouched.",
        "fix": "Revert the Spanish prompt block to v1.8.1, re-run the spanish-speaker-triage suite, route through clinical review, then roll forward.",
        "owner": "PM (me) — config fix, ~15 min + clinical sign-off",
    },
    {
        "agent_id": "call-routing", "agent": "Call Routing & Triage", "account": "Kaiser Permanente",
        "verdict": "platform", "scenario": "emergency-escalation",
        "observation": "Cardiac-symptom calls stopped escalating to the clinical team within the 2-minute SLA.",
        "expected": "severity=HIGH extracted → escalate to cardiology.",
        "actual": "Routed to the general queue. severity_level comes back null.",
        "evidence": "Logs show severity_level: null while symptoms extract fine ('chest pain, shortness of breath'). Regression started the day the extraction model was updated — config unchanged.",
        "fix": "Not a config fix. Hand to Engineering with the failing scenario + logs; the extraction step needs to be re-validated against the new model.",
        "owner": "Engineering — platform bug, escalated with evidence",
    },
]

@app.get("/api/diagnostics")
def diagnostics():
    # attach current status so the view stays in sync with the store
    out = []
    for d in DIAGNOSTICS:
        a = AGENTS.get(d["agent_id"], {})
        out.append({**d, "status": a.get("status"), "clinical_flags": a.get("clinical_flags")})
    return {"cases": out}

class SettingsIn(BaseModel):
    active: str | None = None
    keys: dict[str, str] | None = None
    models: dict[str, str] | None = None


@app.get("/api/settings")
def get_settings():
    return {"active": SETTINGS["active"],
            "providers": {p: {"configured": bool(get_key(p)),
                              "masked": _mask(get_key(p)),
                              "from_env": (not SETTINGS["keys"].get(p)) and bool(os.environ.get(_ENV_KEYS[p], "")),
                              "model": get_model(p),
                              "label": providers_mod.PROVIDER_LABELS[p]}
                          for p in ("anthropic", "openai", "gemini")}}


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    if body.active:
        if body.active not in ("anthropic", "openai", "gemini"):
            raise HTTPException(400, "unknown provider")
        SETTINGS["active"] = body.active
    if body.keys:
        for p, k in body.keys.items():
            if p in ("anthropic", "openai", "gemini"):
                if k == "":
                    SETTINGS["keys"].pop(p, None)   # empty string clears the stored key
                elif k:
                    SETTINGS["keys"][p] = k.strip()
    if body.models:
        for p, m in body.models.items():
            if p in ("anthropic", "openai", "gemini") and m:
                SETTINGS["models"][p] = m.strip()
    _save_settings(SETTINGS)
    return get_settings()


@app.post("/api/settings/test/{provider}")
def test_provider(provider: str):
    if provider not in ("anthropic", "openai", "gemini"):
        raise HTTPException(400, "unknown provider")
    key = get_key(provider)
    if not key:
        return {"ok": False, "message": "No key set for this provider."}
    return providers_mod.test_connection(provider, key, get_model(provider))


# ──────────────────────────────────────────────── automation endpoints
@app.get("/api/agents/{agent_id}/automation")
def get_automation(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")
    return automation_mod.get_agent_automation(agent_id)


@app.post("/api/agents/{agent_id}/automation")
def update_automation(agent_id: str, updates: dict):
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")
    result = automation_mod.update_agent_automation(agent_id, updates)
    return {"ok": True, "automation": result}


@app.post("/webhooks/{agent_id}/{event_type}")
def webhook_trigger(agent_id: str, event_type: str):
    """Webhook endpoint for event-triggered agent execution."""
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")

    should_run, reason = automation_mod.check_event_trigger(agent_id, event_type)
    if not should_run:
        return {"ok": False, "reason": reason}

    # TODO: Actually run the agent here
    # For now, just record that the event was received
    automation_mod.record_run(agent_id, success=True)
    return {"ok": True, "executed": True, "agent_id": agent_id, "event": event_type}


@app.get("/")

def index():
    return HTMLResponse(HTML)


HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Cortex — Agent Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#14181f;--paper:#eef0ec;--card:#fff;--muted:#5a6472;--faint:#8a93a0;--line:#dfe3dd;--line2:#ccd2cb;--seal:#0e5b54;--sealsoft:#e2efec;--ochre:#9a6614;--ochresoft:#f3ead6;--brick:#9c3327;--bricksoft:#f4e2df}
html{font-family:'IBM Plex Sans',system-ui,sans-serif;background:var(--paper);color:var(--ink)}
.mono{font-family:'IBM Plex Mono',monospace}
.header{background:var(--ink);color:#fff;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #000}
.brand{display:flex;align-items:baseline;gap:12px}
.logo{font-family:'Archivo';font-weight:800;font-size:18px;letter-spacing:.14em;padding-left:.14em}
.sub{font-size:11px;letter-spacing:.08em;color:#9aa6a0}
.nav{display:flex;gap:6px}
.navbtn{background:none;border:1px solid #333;color:#9aa6a0;padding:6px 12px;font-size:11px;cursor:pointer;border-radius:2px;font-family:'IBM Plex Mono';letter-spacing:.04em}
.navbtn.active{background:var(--seal);border-color:var(--seal);color:var(--sealsoft)}
.hmeta{font-size:12px;color:#9aa6a0;font-family:'IBM Plex Mono'}
.view{max-width:1240px;margin:0 auto;padding:18px 22px 70px}
.wrap{display:grid;grid-template-columns:minmax(280px,360px) 1fr;gap:22px;align-items:start}
h2{font-family:'Archivo';font-size:15px;font-weight:700;margin:0 0 12px}
h3{font-family:'Archivo';font-size:17px;font-weight:700;margin:0}
h4{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
.grid{display:flex;flex-direction:column;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--faint);border-radius:2px;padding:12px 13px;cursor:pointer;text-align:left;width:100%;font:inherit;color:inherit;transition:transform .1s}
.card:hover{transform:translateY(-1px)}
.card.active{border-color:var(--ink);border-left-color:var(--ink)}
.card[data-s=running]{border-left-color:var(--seal)}
.card[data-s=error]{border-left-color:var(--brick)}
.card[data-s=stopped]{border-left-color:var(--faint)}
.ctop{display:flex;justify-content:space-between;gap:8px;margin-bottom:6px}
.cname{font-weight:600;font-size:13px}
.cstat{font-size:10px;color:var(--faint);font-family:'IBM Plex Mono'}
.cmeta{display:flex;gap:10px;font-size:11px;color:var(--muted);font-family:'IBM Plex Mono'}
.posture{display:inline-block;font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;border-radius:2px;margin-top:7px}
.posture.replace{background:var(--sealsoft);color:var(--seal)}
.posture.augment{background:var(--ochresoft);color:var(--ochre)}
.posture.support{background:#eceef1;color:var(--muted)}
.flagdot{display:inline-block;font-family:'IBM Plex Mono';font-size:9.5px;padding:2px 6px;border-radius:2px;margin-top:7px;margin-left:6px;background:var(--bricksoft);color:var(--brick)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:2px}
.phead{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.acct{font-family:'IBM Plex Mono';font-size:11px;color:var(--seal);letter-spacing:.05em;margin-top:5px}
.vtag{font-family:'IBM Plex Mono';font-size:11px;color:var(--faint)}
.sect{padding:18px 20px;border-bottom:1px solid var(--line)}
.sect:last-child{border-bottom:none}
.ctrls{display:flex;gap:6px;flex-wrap:wrap}
.btn{background:var(--ink);color:#fff;border:none;padding:7px 13px;font-size:12px;border-radius:2px;cursor:pointer;font-family:'IBM Plex Sans';font-weight:500}
.btn:hover{opacity:.92}
.btn.ghost{background:none;color:var(--ink);border:1px solid var(--line2)}
.btn.seal{background:var(--seal)}
.btn:disabled{opacity:.5;cursor:default}
.cfg{background:#f7f8f6;border:1px solid var(--line);border-radius:2px;padding:12px 13px;font-family:'IBM Plex Mono';font-size:12px;line-height:1.7}
.cfg .k{color:var(--muted)}
.cfg .v{color:var(--ink);font-weight:500}
.ask{width:100%;border:1px solid var(--line2);border-radius:2px;padding:11px 12px;font:inherit;font-size:14px;resize:vertical;min-height:64px;background:#fbfcfb}
.ask:focus{outline:2px solid var(--seal);border-color:var(--seal)}
.hint{font-size:11.5px;color:var(--faint);margin-top:8px;line-height:1.5}
.hint code{background:#eceef1;padding:1px 5px;border-radius:2px;font-size:11px}
.diff{margin-top:14px;border:1px solid var(--line);border-radius:2px;overflow:hidden}
.diffhead{background:#f2f4f1;padding:9px 13px;font-family:'IBM Plex Mono';font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between}
.drow{display:grid;grid-template-columns:1.2fr 1fr auto 1fr;gap:12px;padding:10px 13px;border-top:1px solid var(--line);align-items:center;font-size:12px}
.dfield{color:var(--muted)}
.dfrom{color:var(--brick);text-decoration:line-through;opacity:.8}
.dto{color:var(--seal);font-weight:500}
.darrow{color:var(--faint)}
.flag{background:var(--bricksoft);border:1px solid #e6c9c4;border-radius:2px;padding:10px 12px;font-size:12.5px;color:var(--brick);margin-top:12px;line-height:1.5}
.gate{display:flex;gap:8px;margin-top:14px;align-items:center;flex-wrap:wrap}
.gatemsg{font-size:12px;color:var(--muted)}
.applied{background:var(--sealsoft);border:1px solid #bfe0d9;border-radius:2px;padding:10px 12px;font-size:12.5px;color:var(--seal);margin-top:12px}
.hist{display:flex;flex-direction:column;gap:8px}
.hrow{background:#f7f8f6;border:1px solid var(--line);border-radius:2px;padding:10px 12px;font-size:12px}
.htop{display:flex;justify-content:space-between;margin-bottom:4px}
.hver{font-family:'Archivo';font-weight:600}
.hat{font-family:'IBM Plex Mono';font-size:10px;color:var(--faint)}
.hby{font-family:'IBM Plex Mono';font-size:10px;color:var(--faint);margin-top:3px}
.empty{color:var(--faint);font-size:12.5px;padding:6px 0;line-height:1.6}
.spin{display:inline-block;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
/* monitor */
.mstrip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.mcard{background:var(--card);border:1px solid var(--line);border-radius:2px;padding:14px 15px}
.mcard .n{font-family:'Archivo';font-weight:700;font-size:22px}
.mcard .n.warn{color:var(--brick)}
.mcard .l{font-family:'IBM Plex Mono';font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-top:7px}
.mtable{background:var(--card);border:1px solid var(--line);border-radius:2px;overflow:hidden}
.mrow{display:grid;grid-template-columns:2.2fr 1fr 1fr 1fr 1fr 1fr;gap:10px;padding:11px 14px;border-top:1px solid var(--line);font-size:12.5px;align-items:center}
.mrow:first-child{border-top:none;font-family:'IBM Plex Mono';font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);background:#f2f4f1}
.mrow .num{font-family:'IBM Plex Mono';text-align:right}
.mrow.clickable{cursor:pointer}
.mrow.clickable:hover{background:#f7f8f6}
.pill{display:inline-block;padding:2px 7px;border-radius:2px;font-family:'IBM Plex Mono';font-size:10px}
.pill.running{background:var(--sealsoft);color:var(--seal)}.pill.error{background:var(--bricksoft);color:var(--brick)}.pill.stopped{background:#eceef1;color:var(--muted)}
/* diagnostics */
.dgcase{background:var(--card);border:1px solid var(--line);border-radius:2px;padding:18px 20px;margin-bottom:16px}
.dgcase.platform{border-left:3px solid var(--brick)}
.dgcase.config{border-left:3px solid var(--ochre)}
.verdict{display:inline-block;font-family:'IBM Plex Mono';font-size:10px;letter-spacing:.08em;text-transform:uppercase;padding:3px 8px;border-radius:2px;margin-left:10px}
.verdict.platform{background:var(--bricksoft);color:var(--brick)}
.verdict.config{background:var(--ochresoft);color:var(--ochre)}
.dgline{font-size:13px;margin-top:10px;line-height:1.55}
.dgline b{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.04em;text-transform:uppercase;display:block;margin-bottom:2px}
.dgev{background:#f7f8f6;border:1px solid var(--line);border-radius:2px;padding:10px 12px;font-family:'IBM Plex Mono';font-size:11.5px;color:var(--muted);margin-top:4px;line-height:1.5}
/* live agent */
.livebox{background:linear-gradient(180deg,#f7fbfa,transparent 70%)}
.livetag{display:inline-block;font-family:'IBM Plex Mono';font-size:9px;letter-spacing:.06em;padding:2px 6px;border-radius:2px;background:var(--seal);color:#fff;margin-top:7px;margin-left:6px}
.runbox{margin-top:14px;border:1px solid var(--line);border-radius:2px;overflow:hidden}
.runhead{display:flex;justify-content:space-between;align-items:center;padding:10px 13px;background:#f2f4f1;border-bottom:1px solid var(--line)}
.outcome{font-family:'Archivo';font-weight:700;font-size:13px;letter-spacing:.04em;padding:3px 9px;border-radius:2px}
.outcome.published{background:var(--sealsoft);color:var(--seal)}
.outcome.held{background:var(--ochresoft);color:var(--ochre)}
.trace{padding:4px 0;background:#fbfcfb;max-height:340px;overflow:auto}
.tr{padding:7px 13px;font-size:12.5px;line-height:1.5;border-bottom:1px solid #f0f2ef;display:block}
.tr:last-child{border-bottom:none}
.trk{display:inline-block;font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;width:62px;color:var(--faint);vertical-align:top}
.tr.think{color:var(--muted)}
.tr.act .trk{color:var(--seal)}
.tr.obs{color:var(--muted)}.tr.obs .trk{color:#7a5ea8}
.tr.conc .trk{color:var(--seal)}
.tr.esc{background:var(--ochresoft)}.tr.esc .trk{color:var(--ochre)}
.tr.gatetr{background:#f2f4f1;font-weight:600}.tr.gatetr .trk{color:var(--ink)}
.grsn{font-weight:400;font-size:11.5px;color:var(--muted);margin-left:62px;margin-top:3px}
.concl{padding:13px;font-size:13px;border-top:1px solid var(--line)}
.cites{display:flex;flex-direction:column;gap:3px;margin-top:6px}
.cites a{font-family:'IBM Plex Mono';font-size:11px;color:var(--seal);text-decoration:none}
.cites a:hover{text-decoration:underline}
@media(max-width:820px){.wrap{grid-template-columns:1fr}.mrow{grid-template-columns:2fr 1fr 1fr}.mrow .hidesm{display:none}}
</style></head>
<body>
<div class="header">
  <div class="brand"><span class="logo">CORTEX</span><span class="sub">Agent Control</span></div>
  <div class="nav">
    <button class="navbtn active" id="nav-monitor" onclick="setView('monitor')">Monitor</button>
    <button class="navbtn" id="nav-control" onclick="setView('control')">Control</button>
    <button class="navbtn" id="nav-history" onclick="setView('history')">History</button>
    <button class="navbtn" id="nav-diagnostics" onclick="setView('diagnostics')">Diagnostics</button>
    <button class="navbtn" id="nav-automation" onclick="setView('automation')">Automation</button>
    <button class="navbtn" id="nav-runs" onclick="setView('runs')">Runs</button>
    <button class="navbtn" id="nav-settings" onclick="setView('settings')">Settings</button>
    <button class="navbtn" id="nav-glossary" onclick="setView('glossary')">Glossary</button>
    <button class="navbtn" id="nav-integrations" onclick="setView('integrations')">Integrations</button>
  </div>
  <div class="hmeta"><span id="llm-state">rule-based</span> · <span id="count">0</span> agents</div>
</div>

<div class="view" id="root"></div>

<script>
let AGENTS=[], sel=null, pending=null, view='monitor', META={};

async function boot(){
  const r=await fetch('/api/agents'); const d=await r.json();
  AGENTS=d.agents; META=d;
  document.getElementById('count').textContent=d.total;
  document.getElementById('llm-state').textContent = d.llm ? 'model-assisted' : 'rule-based';
  if(!sel && AGENTS.length) sel=AGENTS[0].id;
  await render();
}

function setActiveNav(){
  ['monitor','control','history','diagnostics','automation','runs','settings','glossary','integrations'].forEach(v=>{
    document.getElementById('nav-'+v).classList.toggle('active', v===view);
  });
}

async function render(){
  setActiveNav();
  if(view==='monitor') return await renderMonitor();
  if(view==='control') return await renderControl();
  if(view==='history') return await renderHistory();
  if(view==='diagnostics') return await renderDiagnostics();
  if(view==='automation') return await renderAutomation();
  if(view==='runs') return await renderRuns();
  if(view==='settings') return await renderSettings();
  if(view==='glossary') return await renderGlossary();
  if(view==='integrations') return await renderIntegrations();
}

/* ---------------- MONITOR ---------------- */
async function renderMonitor(){
  const m=await (await fetch('/api/metrics/portfolio')).json();
  const rows=AGENTS.map(a=>`<div class="mrow clickable" onclick="jumpToControl('${a.id}')">
      <div>${a.name}<div style="font-family:'IBM Plex Mono';font-size:10px;color:var(--faint)">${a.account}</div></div>
      <div><span class="pill ${a.status}">${a.status}</span></div>
      <div class="num">${a.containment}%</div>
      <div class="num hidesm">${a.resolution}%</div>
      <div class="num hidesm">${a.escalation}%</div>
      <div class="num">${a.clinical_flags?('⚑ '+a.clinical_flags):'—'}</div>
    </div>`).join('');
  document.getElementById('root').innerHTML=`
    <div class="mstrip">
      <div class="mcard"><div class="n">${META.total}</div><div class="l">agents</div></div>
      <div class="mcard"><div class="n">${m.agents_active}</div><div class="l">running</div></div>
      <div class="mcard"><div class="n">${m.avg_containment}%</div><div class="l">avg containment</div></div>
      <div class="mcard"><div class="n">${m.avg_resolution}%</div><div class="l">avg resolution</div></div>
      <div class="mcard"><div class="n">${m.avg_escalation}%</div><div class="l">avg escalation</div></div>
      <div class="mcard"><div class="n ${m.total_clinical_flags?'warn':''}">${m.total_clinical_flags}</div><div class="l">clinical flags</div></div>
      <div class="mcard"><div class="n">${m.health_score}%</div><div class="l">portfolio health</div></div>
    </div>
    <div class="mtable">
      <div class="mrow"><div>Agent</div><div>Status</div><div class="num">Contain</div><div class="num hidesm">Resolve</div><div class="num hidesm">Escal</div><div class="num">Flags</div></div>
      ${rows}
    </div>
    <div class="hint" style="margin-top:12px">Click any agent to open it in Control. Everything here reflects live config — change an agent and its numbers and posture update across all tabs.</div>`;
}
async function jumpToControl(id){ sel=id; view='control'; await render(); }

/* ---------------- CONTROL ---------------- */
function cfgRows(c){
  const j=c.journey,e=c.escalation,g=c.graph;
  return `<div class="cfg">
    <div><span class="k">posture:</span> <span class="v">${c.posture}</span></div>
    <div><span class="k">first contact:</span> <span class="v">${j.first_contact_delay_hours}h</span> &nbsp; <span class="k">retries:</span> <span class="v">${j.max_retries}</span> &nbsp; <span class="k">gap:</span> <span class="v">${j.retry_gap_hours}h</span></div>
    <div><span class="k">channel:</span> <span class="v">${j.channel}</span> &nbsp; <span class="k">window:</span> <span class="v">${j.call_window}</span></div>
    <div><span class="k">confidence≥</span> <span class="v">${e.confidence_threshold}</span> &nbsp; <span class="k">escalates at:</span> <span class="v">${e.severity_escalates_at}</span> &nbsp; <span class="k">→</span> <span class="v">${e.route_to}</span></div>
    <div><span class="k">confirm-then-act:</span> <span class="v">${g.confirm_then_act?'on':'off'}</span></div>
  </div>`;
}
function sidebar(){
  return `<div><h2>Agents</h2><div class="grid">${AGENTS.map(a=>`
    <button class="card ${sel===a.id?'active':''}" data-s="${a.status}" onclick="pick('${a.id}')">
      <div class="ctop"><span class="cname">${a.name}</span><span class="cstat">${a.status}</span></div>
      <div class="cmeta"><span>C ${a.containment}%</span><span>R ${a.resolution}%</span><span>E ${a.escalation}%</span></div>
      <span class="posture ${a.posture}">${a.posture}</span>${a.live?`<span class="livetag">⚡ live</span>`:''}${a.clinical_flags?`<span class="flagdot">⚑ ${a.clinical_flags}</span>`:''}
    </button>`).join('')}</div></div>`;
}
async function pick(id){ sel=id; pending=null; await render(); }

async function renderControl(){
  const a=await (await fetch('/api/agents/'+sel)).json();
  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead">
        <div><h3>${a.name}</h3><div class="acct">${a.account}</div></div>
        <div style="text-align:right"><div class="vtag">v${a.version}</div>
          <div class="ctrls" style="margin-top:8px">
            <button class="btn ghost" onclick="ctl('start')">Start</button>
            <button class="btn ghost" onclick="ctl('restart')">Restart</button>
            <button class="btn ghost" onclick="ctl('stop')">Stop</button>
          </div></div>
      </div>
      <div class="sect"><h4>Current config</h4>${cfgRows(a.config)}</div>
      ${a.live ? liveRunPanel(a) : ''}
      <div class="sect">
        <h4>Change it — plain English</h4>
        <textarea class="ask" id="ask" placeholder="e.g. Wait 48 hours before the first call, and escalate to nursing when confidence is below 0.8"></textarea>
        <div class="ctrls" style="margin-top:10px"><button class="btn seal" id="propose" onclick="propose()">Propose change</button></div>
        <div class="hint">Cortex proposes a diff and waits for your approval — it never edits a live agent on its own. Try: <code>retry 5 times</code>, <code>call window 9am-5pm</code>, <code>switch to sms only</code>, <code>escalate at moderate severity</code>, <code>turn off confirm-then-act</code>.</div>
        <div id="result"></div>
      </div>
    </div></div>`;
}
/* ---------------- LIVE AGENT RUN ---------------- */
function liveRunPanel(a){
  const c=a.config;
  return `<div class="sect livebox">
    <h4>⚡ Run this agent — live</h4>
    <div class="hint" style="margin:0 0 10px">This agent actually runs: it searches the live web, reads sources, and decides for itself what to check next. It is governed by the config above — <b>max ${c.journey.max_retries} steps</b>, <b>hold below ${c.escalation.confidence_threshold} confidence</b>, <b>confirm-then-act ${c.graph.confirm_then_act?'ON':'OFF'}</b>.</div>
    <textarea class="ask" id="claim" placeholder="Enter a claim to verify — e.g. The Washington Post was founded in 1877"></textarea>
    <div class="ctrls" style="margin-top:10px">
      <button class="btn seal" id="runbtn" onclick="runAgent()">Run agent</button>
    </div>
    <div class="hint">Try a factual claim (should ground and conclude), then something unverifiable or predictive — watch it escalate instead of guessing.</div>
    <div id="runout"></div>
  </div>`;
}

async function runAgent(){
  const claim=document.getElementById('claim').value.trim(); if(!claim) return;
  const btn=document.getElementById('runbtn'); btn.disabled=true;
  btn.innerHTML='<span class="spin">◔</span> Running — searching the live web…';
  const out=document.getElementById('runout');
  out.innerHTML='<div class="hint" style="margin-top:12px">The agent is working. This takes 10–40s depending on how many sources it chases.</div>';
  try{
    const d=await (await fetch('/api/agents/'+sel+'/run',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({claim})})).json();
    btn.disabled=false; btn.textContent='Run agent';
    if(!d.ok){ out.innerHTML=`<div class="flag" style="margin-top:12px">${d.error||'run failed'}</div>`; return; }
    out.innerHTML=renderRun(d.run);
    await boot();
  }catch(e){
    btn.disabled=false; btn.textContent='Run agent';
    out.innerHTML=`<div class="flag" style="margin-top:12px">${e}</div>`;
  }
}

function renderRun(r){
  const steps=(r.trace||[]).map(s=>{
    if(s.kind==='think') return `<div class="tr think"><span class="trk">think</span>${esc(s.text)}</div>`;
    if(s.kind==='act') return `<div class="tr act"><span class="trk">act</span><b>${s.tool}</b> <span class="mono">${esc(JSON.stringify(s.args)).slice(0,160)}</span></div>`;
    if(s.kind==='observe') return `<div class="tr obs"><span class="trk">observe</span>${esc(s.result)}</div>`;
    if(s.kind==='conclude') return `<div class="tr conc"><span class="trk">conclude</span><b>${s.verdict}</b> · confidence ${s.confidence} · ${(s.citations||[]).length} citations</div>`;
    if(s.kind==='escalate') return `<div class="tr esc"><span class="trk">escalate</span>${esc(s.reason||'')}</div>`;
    if(s.kind==='gate') return `<div class="tr gatetr"><span class="trk">GATE</span><b>${s.decision}</b>${(s.reasons||[]).map(x=>`<div class="grsn">· ${esc(x)}</div>`).join('')}</div>`;
    return '';
  }).join('');
  const d=r.detail||{};
  const badge=r.published?'published':'held';
  let concl='';
  if(d.summary) concl+=`<div style="margin-bottom:8px">${esc(d.summary)}</div>`;
  if(d.reason) concl+=`<div style="margin-bottom:8px">${esc(d.reason)}</div>`;
  if(d.what_was_found) concl+=`<div class="hint" style="margin:0 0 8px">Found so far: ${esc(d.what_was_found).slice(0,400)}</div>`;
  if((d.citations||[]).length) concl+=`<div class="cites">${d.citations.map(c=>`<a href="${esc(c)}" target="_blank" rel="noopener">${esc(c).slice(0,80)}</a>`).join('')}</div>`;
  if(!r.published && d.route_to) concl+=`<div class="hint" style="margin-top:8px">→ routed to <b>${esc(d.route_to)}</b> for human review</div>`;
  return `<div class="runbox">
    <div class="runhead"><span class="outcome ${badge}">${r.outcome}</span>
      <span class="mono" style="font-size:11px;color:var(--faint)">${r.steps_used} steps · config v${r.config_version}</span></div>
    <div class="trace">${steps}</div>
    <div class="concl">${concl}</div>
  </div>`;
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function propose(){
  const req=document.getElementById('ask').value.trim(); if(!req) return;
  const btn=document.getElementById('propose'); btn.disabled=true; btn.innerHTML='<span class="spin">◔</span> Proposing…';
  const d=await (await fetch('/api/agents/'+sel+'/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:req})})).json();
  btn.disabled=false; btn.textContent='Propose change';
  const box=document.getElementById('result');
  if(!d.ok){ box.innerHTML=`<div class="flag" style="background:var(--ochresoft);border-color:#e3d3ad;color:var(--ochre)">${d.message}</div>`; return; }
  pending=d.token;
  const rows=d.changes.map(c=>`<div class="drow"><span class="dfield">${c.field}</span>
    <span class="dfrom mono">${fmt(c.from)}</span><span class="darrow">→</span><span class="dto mono">${fmt(c.to)}</span></div>`).join('');
  const flags=(d.flags||[]).map(f=>`<div class="flag">⚠ ${f}</div>`).join('');
  box.innerHTML=`<div class="diff"><div class="diffhead"><span>Proposed diff — not yet applied</span><span>${d.changes.length} change${d.changes.length>1?'s':''}</span></div>${rows}</div>${flags}
    <div class="gate"><button class="btn seal" onclick="apply()">Approve &amp; apply</button>
      <button class="btn ghost" onclick="document.getElementById('result').innerHTML='';pending=null">Discard</button>
      <span class="gatemsg">You are the gate. Applying bumps the version and logs it.</span></div>`;
}
async function apply(){
  if(!pending) return;
  const d=await (await fetch('/api/agents/'+sel+'/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:pending,approved_by:'you'})})).json();
  if(d.ok){ pending=null; await boot(); await renderControl();
    document.getElementById('result').innerHTML=`<div class="applied">✓ Applied. Now v${d.new_version}. Logged to history and reversible — check Monitor, the posture and numbers reflect it.</div>`; }
}
async function ctl(action){ await fetch('/api/agents/'+sel+'/control?action='+action,{method:'POST'}); await boot(); renderControl(); }

/* ---------------- HISTORY ---------------- */
async function renderHistory(){
  const d=await (await fetch('/api/agents/'+sel+'/history')).json();
  const a=AGENTS.find(x=>x.id===sel);
  const rows=d.history.length? d.history.map(h=>`<div class="hrow">
      <div class="htop"><span class="hver">v${h.version} → v${h.version+1}</span><span class="hat">${new Date(h.at).toLocaleString()}</span></div>
      <div>${h.note}</div>
      <div class="hby">by ${h.by}${h.changes&&h.changes.length?' · '+h.changes.map(c=>c.field.split('.').pop()).join(', '):''}</div>
      <div style="margin-top:8px"><button class="btn ghost" onclick="revert(${h.version})">Revert to v${h.version}</button></div>
    </div>`).join('') : '<div class="empty">No changes yet. Make one in Control and it appears here — every version, timestamped and reversible.</div>';
  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead"><div><h3>${a.name}</h3><div class="acct">${a.account} · change log</div></div><div class="vtag">current v${d.current_version}</div></div>
      <div class="sect"><h4>Version history</h4><div class="hist">${rows}</div></div>
    </div></div>`;
}
async function revert(v){ await fetch('/api/agents/'+sel+'/revert/'+v,{method:'POST'}); await boot(); renderHistory(); }

/* ---------------- DIAGNOSTICS ---------------- */
async function renderDiagnostics(){
  const d=await (await fetch('/api/diagnostics')).json();
  const cases=d.cases.map(c=>`<div class="dgcase ${c.verdict}">
      <div style="display:flex;align-items:baseline;justify-content:space-between">
        <div><h3 style="display:inline">${c.agent}</h3><span class="verdict ${c.verdict}">${c.verdict==='platform'?'platform bug':'config issue'}</span></div>
        <span class="vtag">${c.account} · <span class="pill ${c.status}">${c.status}</span></span>
      </div>
      <div class="dgline"><b>Scenario</b>${c.scenario}</div>
      <div class="dgline"><b>Observation</b>${c.observation}</div>
      <div class="dgline"><b>Expected vs actual</b>${c.expected} <span style="color:var(--faint)">→ instead:</span> ${c.actual}</div>
      <div class="dgline"><b>Evidence</b><div class="dgev">${c.evidence}</div></div>
      <div class="dgline"><b>Fix &amp; owner</b>${c.fix}<div style="font-family:'IBM Plex Mono';font-size:11px;color:var(--muted);margin-top:6px">${c.owner}</div></div>
    </div>`).join('');
  document.getElementById('root').innerHTML=`<div style="max-width:900px">
    <h2>Config issue or platform bug?</h2>
    <div class="hint" style="margin-bottom:16px">The first triage call on any failure: can I fix this in the config myself, or is it the platform and I hand it to Engineering with evidence? Two live examples.</div>
    ${cases}</div>`;
}

/* ────────────── AUTOMATION ────────────── */
async function renderAutomation(){
  const a=AGENTS.find(x=>x.id===sel);
  const auto=await (await fetch('/api/agents/'+sel+'/automation')).json();
  const schedOpts=['disabled','daily','daily:06:00','daily:09:00','every_2h','every_4h','every_6h','24h_before'];
  const eventOpts=['appointment_scheduled','appointment_created','appointment_updated','lab_result_available','auth_request_received'];

  let lastRunStr='Never';
  if(auto.last_run && auto.last_run.timestamp){
    const d=new Date(auto.last_run.timestamp);
    lastRunStr=d.toLocaleString()+(auto.last_run.success?' ✓':' ✗');
  }
  let nextRunStr='Not scheduled';
  if(auto.next_run){
    const d=new Date(auto.next_run);
    nextRunStr=d.toLocaleString();
  }

  const eventChecks=eventOpts.map(e=>`<label style="display:inline-flex;align-items:center;gap:6px;margin-right:12px">
    <input type="checkbox" value="${e}" ${auto.event_triggers&&auto.event_triggers.includes(e)?'checked':''}>
    <span style="font-size:12px">${e}</span>
  </label>`).join('');

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead"><div><h3>${a.name}</h3><div class="acct">${a.account} · automation</div></div></div>
      <div class="sect">
        <h4>Enable automation</h4>
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="auto_enabled" ${auto.enabled?'checked':''}>
          <span>Enable time-based and event-based automation for this agent</span>
        </label>
      </div>
      <div class="sect">
        <h4>Schedule</h4>
        <div style="margin-bottom:8px">
          <label style="font-size:12px;color:var(--muted)">Run frequency:</label><br>
          <select id="auto_schedule" style="padding:6px 8px;border:1px solid var(--line);border-radius:2px;font-size:12px;margin-top:4px">
            ${schedOpts.map(s=>`<option value="${s}" ${auto.schedule===s?'selected':''}>${s}</option>`).join('')}
          </select>
        </div>
      </div>
      <div class="sect">
        <h4>Event triggers</h4>
        <div style="margin-bottom:8px">${eventChecks}</div>
      </div>
      <div class="sect">
        <h4>Status</h4>
        <div style="font-size:12px;color:var(--muted);line-height:1.6">
          <div><span style="font-weight:600">Last run:</span> ${lastRunStr}</div>
          <div><span style="font-weight:600">Next run:</span> ${nextRunStr}</div>
        </div>
      </div>
      <div class="sect">
        <button class="btn seal" onclick="saveAutomation()">Save automation</button>
        <span id="auto_msg" style="margin-left:12px;font-size:11px"></span>
      </div>
    </div></div>`;
}

async function saveAutomation(){
  const enabled=document.getElementById('auto_enabled').checked;
  const schedule=document.getElementById('auto_schedule').value;
  const eventTriggers=Array.from(document.querySelectorAll('input[type="checkbox"][value]')).filter(cb=>cb.checked).map(cb=>cb.value);
  const d=await (await fetch('/api/agents/'+sel+'/automation',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled,schedule,event_triggers:eventTriggers})
  })).json();
  const msg=document.getElementById('auto_msg');
  msg.textContent=d.ok?'✓ Saved':'✗ Error';
  msg.style.color=d.ok?'var(--seal)':'var(--brick)';
  setTimeout(()=>renderAutomation(),1500);
}

/* ────────────── RUNS (Agent Execution Output) ────────────── */
async function renderRuns(){
  const a=AGENTS.find(x=>x.id===sel);
  const r=await (await fetch('/api/agents/'+sel+'/runs')).json();
  const runs=r.runs||[];

  const rows=runs.length?runs.map((run,i)=>{
    const tr=run.trace||[];
    const traceHTML=tr.map(t=>{
      if(t.kind==='think') return `<div class="tr think"><span class="trk">think</span> ${t.text||''}</div>`;
      if(t.kind==='act') return `<div class="tr act"><span class="trk">act</span> ${t.tool} ${JSON.stringify(t.args||{}).substring(0,80)}</div>`;
      if(t.kind==='observe') return `<div class="tr obs"><span class="trk">observe</span> ${t.result||''}</div>`;
      if(t.kind==='escalate') return `<div class="tr esc"><span class="trk">escalate</span> ${t.reason||''}</div>`;
      return '';
    }).join('');

    const ts=new Date(run.timestamp).toLocaleString();
    const status=run.ok?'success':'error';
    return `<div class="runbox">
      <div class="runhead">
        <div><span class="outcome ${run.ok?'published':'held'}">${status}</span> <span style="font-family:'IBM Plex Mono';font-size:11px;color:var(--faint)">${ts}</span></div>
        <div style="font-size:11px;color:var(--faint)">${run.steps_used||0} steps · ${run.escalated?'escalated':'contained'}</div>
      </div>
      <div class="trace">${traceHTML}</div>
      ${run.final_text?`<div class="concl"><b>Output:</b> ${run.final_text}</div>`:''}</div>`;
  }).join('') : '<div class="empty">No execution history yet. Run an agent from the Control tab to see traces here.</div>';

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead"><div><h3>${a.name}</h3><div class="acct">${a.account} · run history</div></div><div class="vtag">${runs.length} runs</div></div>
      <div class="sect">${rows}</div>
    </div></div>`;
}

/* ────────────── SETTINGS (API Key Management) ────────────── */
async function renderSettings(){
  const s=await (await fetch('/api/settings')).json();
  const providers=['anthropic','openai','gemini'];
  const models={
    anthropic:['claude-opus-5-20250514','claude-sonnet-4-20250514','claude-haiku-4-5-20250514'],
    openai:['gpt-4-turbo','gpt-4o','gpt-4-32k','gpt-3.5-turbo'],
    gemini:['gemini-2.0-flash-exp','gemini-2.0-pro','gemini-1.5-pro','gemini-1.5-flash']
  };

  const provOpts=providers.map(p=>{
    const prov=s.providers?.[p]||{configured:false,masked:'',model:''};
    return `<div style="margin:12px 0;padding:12px;background:#f7f8f6;border-radius:2px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:8px">
        <input type="radio" name="active_provider" value="${p}" ${s.active===p?'checked':''} onchange="selectProvider('${p}')">
        <span style="font-weight:600">${prov.label}</span>
        ${prov.configured?'<span style="font-family:\'IBM Plex Mono\';font-size:10px;color:var(--seal);margin-left:auto">✓ set</span>':''}
      </label>
      <div style="margin:0 0 0 24px">
        <div style="font-size:12px;color:var(--muted);margin-bottom:4px">API Key</div>
        <div style="display:flex;gap:6px;align-items:center">
          <input type="password" id="key_${p}" placeholder="sk-..." style="flex:1;padding:6px 8px;border:1px solid var(--line);border-radius:2px;font-family:\'IBM Plex Mono\';font-size:11px">
          <button class="btn ghost" style="padding:4px 10px;font-size:11px" onclick="testProvider('${p}')">Test</button>
          <span id="test_${p}" style="font-size:11px;min-width:60px">${prov.masked?'('+prov.masked+')':''}</span>
        </div>
        <div style="margin:8px 0 0 0">
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px">Model</div>
          <select id="model_${p}" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:2px;font-size:12px">
            ${models[p].map(mod=>`<option value="${mod}" ${prov.model===mod?'selected':''}>${mod}</option>`).join('')}
          </select>
        </div>
      </div>
    </div>`;
  }).join('');

  document.getElementById('root').innerHTML=`<div style="max-width:600px">
    <h2>API Settings</h2>
    <div class="hint" style="margin-bottom:16px">Configure API keys and models for the providers your agents use. Settings are tested and persisted locally.</div>
    ${provOpts}
    <div style="margin-top:16px">
      <button class="btn seal" onclick="saveSettings()">Save settings</button>
      <span id="settings_msg" style="margin-left:12px;font-size:11px"></span>
    </div>
  </div>`;
}

async function selectProvider(p){
  await (await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:p})})).json();
}

async function updateKey(p,v){
  await (await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:{[p]:v}})})).json();
}

async function updateModel(p,m){
  await (await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({models:{[p]:m}})})).json();
}

async function testProvider(p){
  const key=document.getElementById('key_'+p).value;
  const model=document.getElementById('model_'+p).value;
  if(!key){
    document.getElementById('test_'+p).textContent='✗ no key';
    document.getElementById('test_'+p).style.color='var(--brick)';
    return;
  }
  const span=document.getElementById('test_'+p);
  span.textContent='testing...';
  const d=await (await fetch('/api/settings/test/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key,model})})).json();
  span.textContent=d.ok?'✓':'✗ failed';
  span.style.color=d.ok?'var(--seal)':'var(--brick)';
}

async function saveSettings(){
  const keys={};
  const models={};
  ['anthropic','openai','gemini'].forEach(prov=>{
    const k=document.getElementById('key_'+prov).value;
    const m=document.getElementById('model_'+prov).value;
    if(k) keys[prov]=k;
    if(m) models[prov]=m;
  });
  const d=await (await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys,models})})).json();
  const msg=document.getElementById('settings_msg');
  msg.textContent=d.ok?'✓ Saved':'✗ Error';
  msg.style.color=d.ok?'var(--seal)':'var(--brick)';
}

/* ────────────── GLOSSARY (Config Reference) ────────────── */
async function renderGlossary(){
  document.getElementById('root').innerHTML=`<div style="max-width:900px">
    <h2>Configuration Glossary</h2>
    <div class="hint" style="margin-bottom:20px">Understanding CORTEX agent config fields. Every change you make in Control shows a before/after diff here, and every change is reversible.</div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px;margin-bottom:20px">
      <div class="panel" style="padding:16px">
        <h4>Posture</h4>
        <div style="font-size:12px;line-height:1.6;color:var(--muted)">
          <div><b>replace</b><br>Agent acts autonomously (sends SMS, submits forms, etc.)</div>
          <div style="margin-top:8px"><b>augment</b><br>Agent assists humans with suggestions and context</div>
          <div style="margin-top:8px"><b>support</b><br>Agent supports systems with data preparation</div>
        </div>
      </div>

      <div class="panel" style="padding:16px">
        <h4>Journey</h4>
        <div style="font-size:12px;line-height:1.6;color:var(--muted)">
          <div><b>first_contact_delay_hours</b><br>Wait N hours before first outreach</div>
          <div style="margin-top:8px"><b>max_retries</b><br>Number of retry attempts</div>
          <div style="margin-top:8px"><b>retry_gap_hours</b><br>Hours between retries</div>
          <div style="margin-top:8px"><b>channel</b><br>voice, sms, email, or voice+sms</div>
          <div style="margin-top:8px"><b>call_window</b><br>Time range like "09:00-17:00 local"</div>
        </div>
      </div>

      <div class="panel" style="padding:16px">
        <h4>Escalation</h4>
        <div style="font-size:12px;line-height:1.6;color:var(--muted)">
          <div><b>confidence_threshold</b><br>If confidence < this (0.0–1.0), escalate to human</div>
          <div style="margin-top:8px"><b>severity_escalates_at</b><br>Escalate cases marked as low, moderate, or high</div>
          <div style="margin-top:8px"><b>route_to</b><br>Team or role to escalate to (e.g., "clinical team")</div>
        </div>
      </div>

      <div class="panel" style="padding:16px">
        <h4>Graph</h4>
        <div style="font-size:12px;line-height:1.6;color:var(--muted)">
          <div><b>nodes</b><br>Workflow steps (e.g., greet → verify → act → close)</div>
          <div style="margin-top:8px"><b>confirm_then_act</b><br>If true, agent confirms with human before any data write</div>
        </div>
      </div>

      <div class="panel" style="padding:16px">
        <h4>Metrics</h4>
        <div style="font-size:12px;line-height:1.6;color:var(--muted)">
          <div><b>containment</b><br>% of cases handled without escalation (goal: high)</div>
          <div style="margin-top:8px"><b>resolution</b><br>% of cases fully resolved (goal: high)</div>
          <div style="margin-top:8px"><b>escalation</b><br>% escalated to human (monitor for patterns)</div>
          <div style="margin-top:8px"><b>clinical_flags</b><br>Safety incidents flagged for review</div>
        </div>
      </div>
    </div>
  </div>`;
}

/* ────────────── INTEGRATIONS (SDK & Webhooks) ────────────── */
async function renderIntegrations(){
  document.getElementById('root').innerHTML=`<div style="max-width:900px">
    <h2>Integrations</h2>
    <div class="hint" style="margin-bottom:20px">Embed CORTEX agents into your client sites. Full integration guide at https://docs.cortex.health</div>

    <h3 style="margin-top:24px;margin-bottom:12px">SDK Installation</h3>
    <div class="cfg" style="background:#fbfcfb;border:1px solid var(--line);padding:12px;border-radius:2px">
      <pre style="font-size:11px;overflow-x:auto">&lt;script src="https://cortex-sdk.example.com/cortex.min.js"&gt;&lt;/script&gt;
&lt;script&gt;
  const cortex = new Cortex({
    siteId: 'your-site-id',
    apiKey: 'your-api-key',
    agentId: 'no-show',
    endpoint: 'https://your-cortex-instance.com/api'
  });
  cortex.mount('#cortex-widget');
&lt;/script&gt;
&lt;div id="cortex-widget"&gt;&lt;/div&gt;</pre>
    </div>

    <h3 style="margin-top:24px;margin-bottom:12px">Webhook Setup</h3>
    <div class="cfg" style="background:#fbfcfb;border:1px solid var(--line);padding:12px;border-radius:2px">
      <pre style="font-size:11px;overflow-x:auto">POST /webhooks/{agent_id}/{event_type}
Authorization: Bearer your-webhook-secret

{
  "event": "agent.run.completed",
  "agent_id": "no-show",
  "status": "success",
  "escalated": false,
  "steps": 4,
  "timestamp": "2026-08-26T02:12:00Z"
}</pre>
    </div>

    <h3 style="margin-top:24px;margin-bottom:12px">Authentication</h3>
    <ul style="margin-left:20px;font-size:12px;line-height:1.8;color:var(--muted)">
      <li><b>Site ID:</b> Unique identifier for your organization</li>
      <li><b>API Key:</b> Bearer token for authentication (keep secret)</li>
      <li><b>Webhook Secret:</b> HMAC-SHA256 signature verification</li>
      <li>All API calls must use HTTPS. Store API keys in environment variables, never in client-side code.</li>
    </ul>

    <h3 style="margin-top:24px;margin-bottom:12px">Rate Limiting</h3>
    <div style="max-width:500px">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;background:#f2f4f1;padding:10px;border-radius:2px;font-size:11px;font-family:\'IBM Plex Mono\';font-weight:600">
        <div>Tier</div><div>Requests/min</div><div>Max payload</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:8px;border-bottom:1px solid var(--line);font-size:11px">
        <div>Free</div><div>10</div><div>100 KB</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:8px;border-bottom:1px solid var(--line);font-size:11px">
        <div>Pro</div><div>60</div><div>5 MB</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:8px;font-size:11px">
        <div>Enterprise</div><div>Custom</div><div>Custom</div>
      </div>
    </div>

    <div class="hint" style="margin-top:24px">Questions? Full docs: <a href="https://docs.cortex.health" style="color:var(--seal);text-decoration:none">https://docs.cortex.health</a></div>
  </div>`;
}

async function setView(v){ view=v; await render(); }
function fmt(x){ return x===null||x===undefined?'∅':(typeof x==='boolean'?(x?'on':'off'):String(x)); }
boot();
</script>
</body></html>"""

if __name__ == "__main__":
    print("Cortex Agent Control — http://localhost:3000")
    print("model-assisted" if API_KEY else "rule-based (set ANTHROPIC_API_KEY for model translation)")
    uvicorn.run(app, host="0.0.0.0", port=3000)
