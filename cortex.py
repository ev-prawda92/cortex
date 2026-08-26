#!/usr/bin/env python3
"""
Cortex — Agent Operations Hub

One file. Run:  python3 cortex.py   then open http://localhost:3000

A generic agent ops platform: register, configure, run, and monitor any agent.
The control panel lets you describe a change to an agent in plain English.
Cortex proposes a specific config diff, shows you before/after, and does NOT
apply anything until you approve. Every applied change is versioned and
reversible. Nobody's plain text silently mutates a live agent — the human
stays in the loop.

Agents support multiple endpoint types (embedded, REST, webhook), configurable
models, tools, data sources, and integration code generation.

Set ANTHROPIC_API_KEY to use the model for translation. Without it, Cortex
falls back to a deterministic parser that handles the common change types
(timeouts, thresholds, retries, model settings, escalation, enable/disable).
"""

import os
import re
import copy
import json
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Cortex", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Auth / SSO ──────────────────────────────────────────────────────
# In production, swap this for real OAuth2/SAML. For now, local user store + sessions.
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
SESSIONS: dict[str, dict] = {}   # token -> {email, name, role, org}

def _load_users() -> list[dict]:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return []

def _save_users(users: list[dict]):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def _get_session(request: Request) -> dict | None:
    token = request.cookies.get("cortex_session")
    if token and token in SESSIONS:
        return SESSIONS[token]
    return None

class SignupBody(BaseModel):
    name: str
    email: str
    password: str
    role: str = "FDE"
    org: str = ""

class LoginBody(BaseModel):
    email: str
    password: str

@app.post("/api/auth/signup")
def signup(body: SignupBody):
    users = _load_users()
    if any(u["email"].lower() == body.email.lower() for u in users):
        raise HTTPException(400, "Email already registered")
    user = {"name": body.name, "email": body.email, "password": _hash_pw(body.password),
            "role": body.role, "org": body.org, "created": datetime.now(timezone.utc).isoformat()}
    users.append(user)
    _save_users(users)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"email": user["email"], "name": user["name"], "role": user["role"], "org": user["org"]}
    return {"ok": True, "token": token, "user": {"name": user["name"], "email": user["email"], "role": user["role"], "org": user["org"]}}

@app.post("/api/auth/login")
def login(body: LoginBody):
    users = _load_users()
    match = next((u for u in users if u["email"].lower() == body.email.lower()), None)
    if not match or match["password"] != _hash_pw(body.password):
        raise HTTPException(401, "Invalid email or password")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"email": match["email"], "name": match["name"], "role": match.get("role","FDE"), "org": match.get("org","")}
    return {"ok": True, "token": token, "user": {"name": match["name"], "email": match["email"], "role": match.get("role","FDE"), "org": match.get("org","")}}

@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("cortex_session")
    if token and token in SESSIONS:
        del SESSIONS[token]
    return {"ok": True}

@app.get("/api/auth/me")
def auth_me(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    return {"user": sess}

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# ─────────────────────────────────────────────── provider settings (Settings tab)
import providers as providers_mod
import automation as automation_mod

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# ─────────────────────────────────────────────── agent runs (Runs tab)
RUNS = {}  # agent_id -> list of execution records with trace, output, metrics

# ─────────────────────────────────────────────── event log & diagnosis
EVENT_LOG = []  # list of {timestamp, agent_id, event_type, data}
MAX_EVENTS = 500  # rolling buffer size

def log_event(agent_id: str, event_type: str, data: dict = None):
    """Log an event to the event log (newest first)."""
    global EVENT_LOG
    EVENT_LOG.insert(0, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "event_type": event_type,
        "data": data or {}
    })
    if len(EVENT_LOG) > MAX_EVENTS:
        EVENT_LOG = EVENT_LOG[:MAX_EVENTS]

def diagnose_agent(agent_id: str):
    """Analyze agent runs to classify as build vs training problem."""
    if agent_id not in RUNS or not RUNS[agent_id]:
        return {
            "type": "unknown",
            "confidence": 0,
            "checklist": [],
            "evidence": "No runs yet"
        }

    runs = RUNS[agent_id][:10]  # last 10 runs
    escalations = sum(1 for r in runs if r.get("escalated"))
    errors = sum(1 for r in runs if not r.get("ok"))
    avg_confidence = sum(r.get("confidence_threshold", 0.75) for r in runs) / len(runs) if runs else 0

    escalation_rate = escalations / len(runs) if runs else 0
    error_rate = errors / len(runs) if runs else 0

    if error_rate > 0.3:
        # BUILD PROBLEM
        return {
            "type": "build",
            "confidence": min(0.95, 0.6 + error_rate),
            "evidence": f"{error_rate*100:.0f}% error rate across runs",
            "checklist": [
                "Check agent timeout and retry settings",
                "Verify API connections and rate limits",
                "Review error logs for infrastructure issues",
                "Test with minimal config to isolate problem",
                "Check model availability and fallbacks"
            ]
        }
    elif escalation_rate > 0.4:
        # TRAINING PROBLEM
        return {
            "type": "training",
            "confidence": min(0.95, 0.5 + escalation_rate),
            "evidence": f"{escalation_rate*100:.0f}% escalation rate, avg confidence {avg_confidence:.2f}",
            "checklist": [
                "Review escalated cases for decision patterns",
                "Adjust confidence threshold if too conservative",
                "Enhance agent prompt with better context/examples",
                "Fine-tune model selection or parameters",
                "Add more tool context or clarifying instructions"
            ]
        }
    else:
        # HEALTHY
        return {
            "type": "healthy",
            "confidence": 0.9,
            "evidence": f"Low error rate ({error_rate*100:.0f}%), low escalation ({escalation_rate*100:.0f}%)",
            "checklist": []
        }

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

_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY", "perplexity": "PERPLEXITY_API_KEY", "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY", "meta": "TOGETHER_API_KEY",
}

def get_key(provider):
    return SETTINGS["keys"].get(provider) or os.environ.get(_ENV_KEYS.get(provider, ""), "")

def get_model(provider):
    return SETTINGS["models"].get(provider) or providers_mod.DEFAULT_MODELS.get(provider, "")

def _mask(k):
    return (k[:7] + "…" + k[-4:]) if k and len(k) > 14 else ("set" if k else "")


# ---------------------------------------------------------------- agent configs
# Each agent carries a structured config the control panel can edit.
# Types: sample (built-in examples) | custom (user-created) | imported (from integration)

AGENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.json")

_DEFAULT_AGENTS = {
    "sample-research": {
        "name": "Research Agent",
        "description": "General-purpose research agent that searches and summarizes information",
        "account": "Sample",
        "status": "running", "version": 1,
        "containment": 0, "resolution": 0, "escalation": 0, "clinical_flags": 0,
        "live": True,
        "type": "sample",
        "endpoint": {"type": "embedded", "url": ""},
        "config": {
            "model": {"provider": "anthropic", "model_name": "claude-sonnet-5", "temperature": 0.7, "max_tokens": 4096},
            "execution": {"timeout_seconds": 300, "max_retries": 3, "retry_delay_seconds": 60},
            "behavior": {"confidence_threshold": 0.75, "escalation_threshold": "high", "auto_escalate_on_error": True, "confirm_before_action": True},
            "data_sources": [],
            "tools": [
                {"name": "web_search", "description": "Search the web", "parameters": ["query"], "rate_limit": 100},
                {"name": "fetch_url", "description": "Fetch and read a URL", "parameters": ["url"], "rate_limit": 50},
                {"name": "summarize", "description": "Summarize text content", "parameters": ["text"], "rate_limit": 100}
            ],
            "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": True}
        },
    },
    "sample-router": {
        "name": "Router Agent",
        "description": "Routes incoming requests to the appropriate handler based on intent",
        "account": "Sample",
        "status": "stopped", "version": 1,
        "containment": 0, "resolution": 0, "escalation": 0, "clinical_flags": 0,
        "live": True,
        "type": "sample",
        "endpoint": {"type": "embedded", "url": ""},
        "config": {
            "model": {"provider": "anthropic", "model_name": "claude-sonnet-5", "temperature": 0.3, "max_tokens": 1024},
            "execution": {"timeout_seconds": 30, "max_retries": 2, "retry_delay_seconds": 5},
            "behavior": {"confidence_threshold": 0.8, "escalation_threshold": "moderate", "auto_escalate_on_error": True, "confirm_before_action": False},
            "data_sources": [],
            "tools": [
                {"name": "classify_intent", "description": "Classify the intent of a message", "parameters": ["message", "categories"], "rate_limit": 200},
                {"name": "route_request", "description": "Route to appropriate handler", "parameters": ["intent", "payload"], "rate_limit": 200}
            ],
            "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": False}
        },
    },
    "sample-action": {
        "name": "Action Agent",
        "description": "Executes actions in external systems based on instructions",
        "account": "Sample",
        "status": "stopped", "version": 1,
        "containment": 0, "resolution": 0, "escalation": 0, "clinical_flags": 0,
        "live": True,
        "type": "sample",
        "endpoint": {"type": "rest", "url": "https://your-api.example.com/agent"},
        "config": {
            "model": {"provider": "openai", "model_name": "gpt-5.6-terra", "temperature": 0.5, "max_tokens": 2048},
            "execution": {"timeout_seconds": 120, "max_retries": 3, "retry_delay_seconds": 30},
            "behavior": {"confidence_threshold": 0.85, "escalation_threshold": "low", "auto_escalate_on_error": True, "confirm_before_action": True},
            "data_sources": [
                {"name": "example_crm", "type": "api", "endpoint": "https://crm.example.com/api", "auth_type": "api_key", "auth_value": ""}
            ],
            "tools": [
                {"name": "create_record", "description": "Create a new record", "parameters": ["type", "data"], "rate_limit": 50},
                {"name": "update_record", "description": "Update an existing record", "parameters": ["id", "data"], "rate_limit": 50},
                {"name": "send_notification", "description": "Send a notification", "parameters": ["recipient", "message"], "rate_limit": 100}
            ],
            "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": True}
        },
    },
}

def _load_agents():
    try:
        with open(AGENTS_PATH) as f:
            return json.load(f)
    except Exception:
        return copy.deepcopy(_DEFAULT_AGENTS)

def _save_agents():
    with open(AGENTS_PATH, "w") as f:
        json.dump(AGENTS, f, indent=2)
    try:
        os.chmod(AGENTS_PATH, 0o600)
    except Exception:
        pass

def _slugify(name: str) -> str:
    """Convert a name to a URL-safe slug."""
    s = re.sub(r"[^\w\s-]", "", name.lower().strip())
    return re.sub(r"[\s_]+", "-", s).strip("-")[:64]

def _default_config():
    """Return a blank generic agent config scaffold."""
    return {
        "model": {"provider": "anthropic", "model_name": "claude-sonnet-5", "temperature": 0.7, "max_tokens": 4096},
        "execution": {"timeout_seconds": 300, "max_retries": 3, "retry_delay_seconds": 60},
        "behavior": {"confidence_threshold": 0.75, "escalation_threshold": "high", "auto_escalate_on_error": True, "confirm_before_action": True},
        "data_sources": [],
        "tools": [],
        "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": True}
    }

AGENTS = _load_agents()

# version history: agent_id -> list of {version, at, by, note, config}
HISTORY = {aid: [] for aid in AGENTS}
# pending proposals: token -> {agent_id, request, diff, before, after}
PENDING = {}

def _ensure_history(agent_id: str):
    """Ensure HISTORY has an entry for this agent."""
    if agent_id not in HISTORY:
        HISTORY[agent_id] = []


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
    Works with both the new generic config schema and legacy configs.
    Returns (new_config, notes). Never mutates the input cfg.
    """
    c = copy.deepcopy(cfg)
    r = request.lower()
    notes = []

    # ── Generic config schema fields ──

    # temperature: "set temperature to 0.5", "temperature 0.9"
    if "temperature" in r:
        m = re.search(r"temperature\D{0,15}?(0?\.\d+|\d)", r)
        if m:
            val = float(m.group(1))
            if val > 2:
                val = 2.0
            c.setdefault("model", {})["temperature"] = round(val, 2)
            notes.append(f"temperature → {round(val, 2)}")

    # max tokens: "max tokens 2048", "set token limit to 8192"
    m = re.search(r"(?:max.?tokens|token.?limit)\D{0,10}?(\d{2,6})", r)
    if m:
        c.setdefault("model", {})["max_tokens"] = int(m.group(1))
        notes.append(f"max_tokens → {m.group(1)}")

    # timeout: "timeout 60 seconds", "set timeout to 5 minutes"
    m = re.search(r"timeout\D{0,15}?(\d+)\s*(second|sec|minute|min)", r)
    if m:
        n = int(m.group(1))
        if "min" in m.group(2):
            n *= 60
        c.setdefault("execution", {})["timeout_seconds"] = n
        notes.append(f"timeout → {n}s")

    # retries: "retry 5 times", "max 2 retries", "up to 4 attempts"
    m = re.search(r"(\d+)\s*(?:times|retr|attempt|tr(?:y|ies))", r)
    if not m:
        m = re.search(r"(?:retry|retries|attempts?)\D{0,12}?(\d+)", r)
    if m and ("retr" in r or "attempt" in r or "times" in r):
        val = int(m.group(1))
        c.setdefault("execution", {})["max_retries"] = val
        # Also update legacy journey.max_retries if present
        if "journey" in c:
            c["journey"]["max_retries"] = val
        notes.append(f"max retries → {m.group(1)}")

    # retry delay: "retry delay 30 seconds", "wait 2 minutes between retries"
    m = re.search(r"(?:retry.?delay|between retries|retry.?gap)\D{0,15}?(\d+)\s*(second|sec|minute|min|hour|hr)", r)
    if m:
        n = int(m.group(1))
        if "min" in m.group(2):
            n *= 60
        elif "hour" in m.group(2) or "hr" in m.group(2):
            n *= 3600
        c.setdefault("execution", {})["retry_delay_seconds"] = n
        notes.append(f"retry delay → {n}s")

    # confidence threshold: "escalate below 0.8 confidence", "confidence 75%"
    if "confidence" in r:
        m = re.search(r"(0?\.\d+|\d{1,3})\s*%?\s*confidence", r) \
            or re.search(r"confidence\D{0,20}?(0?\.\d+|\d{1,3})\s*%?", r)
        if m:
            val = float(m.group(1))
            if val > 1:
                val /= 100.0
            c.setdefault("behavior", {})["confidence_threshold"] = round(val, 2)
            # Also update legacy escalation.confidence_threshold if present
            if "escalation" in c:
                c["escalation"]["confidence_threshold"] = round(val, 2)
            notes.append(f"confidence threshold → {round(val,2)}")

    # escalation threshold: "escalate at moderate", "only escalate high severity"
    for sev in SEVERITY:
        if re.search(rf"escalat\w*.*\b{sev}\b|\b{sev}\b.*escalat", r):
            c.setdefault("behavior", {})["escalation_threshold"] = sev
            # Also update legacy escalation.severity_escalates_at if present
            if "escalation" in c:
                c["escalation"]["severity_escalates_at"] = sev
            notes.append(f"escalation threshold → {sev}")
            break

    # confirm before action toggle
    if "confirm" in r and ("off" in r or "disable" in r or "without" in r or "remove" in r):
        c.setdefault("behavior", {})["confirm_before_action"] = False
        if "graph" in c:
            c["graph"]["confirm_then_act"] = False
        notes.append("confirm before action → OFF")
    elif "confirm" in r and ("on" in r or "enable" in r or "require" in r):
        c.setdefault("behavior", {})["confirm_before_action"] = True
        if "graph" in c:
            c["graph"]["confirm_then_act"] = True
        notes.append("confirm before action → ON")

    # auto-escalate on error
    if "auto" in r and "escalat" in r:
        if "off" in r or "disable" in r or "no" in r:
            c.setdefault("behavior", {})["auto_escalate_on_error"] = False
            notes.append("auto-escalate on error → OFF")
        elif "on" in r or "enable" in r or "yes" in r:
            c.setdefault("behavior", {})["auto_escalate_on_error"] = True
            notes.append("auto-escalate on error → ON")

    # model provider: "switch to openai", "use gemini"
    for prov in providers_mod.ALL_PROVIDERS:
        if re.search(rf"\b{prov}\b", r) and any(w in r for w in ["switch", "use", "provider", "change to"]):
            c.setdefault("model", {})["provider"] = prov
            notes.append(f"provider → {prov}")
            break

    # ── Legacy config fields (for backward compatibility) ──

    # timing: "wait 48 hours", "first call after 3 days", "delay ... 12 hours"
    if "journey" in c:
        m = re.search(r"(\d+)\s*(hour|hr|day)", r)
        if m and any(w in r for w in ["wait", "delay", "first", "before", "after"]):
            n = int(m.group(1))
            if "day" in m.group(2):
                n *= 24
            c["journey"]["first_contact_delay_hours"] = n
            notes.append(f"first contact delay → {n}h")

    # channel (legacy)
    if "journey" in c:
        if "sms" in r and "voice" in r:
            c["journey"]["channel"] = "voice+sms"; notes.append("channel → voice+sms")
        elif "text" in r or re.search(r"\bsms\b", r):
            c["journey"]["channel"] = "sms"; notes.append("channel → sms")
        elif "call" in r or "voice" in r or "phone" in r:
            if "only" in r or "just" in r:
                c["journey"]["channel"] = "voice"; notes.append("channel → voice")

    # call window (legacy)
    if "journey" in c:
        m = re.search(r"(\d{1,2})\s*(?:am|:00)?\s*[-to]+\s*(\d{1,2})\s*(?:pm|am|:00)?", r)
        if m and ("window" in r or "between" in r or "call" in r):
            a, b = int(m.group(1)), int(m.group(2))
            if b <= 12 and ("pm" in r or b < a):
                b += 12
            c["journey"]["call_window"] = f"{a:02d}:00-{b:02d}:00 local"
            notes.append(f"call window → {a:02d}:00-{b:02d}:00 local")

    # route to: "route to nursing", "hand off to ops team"
    if "escalation" in c:
        m = re.search(r"(route|hand ?off|escalate)\s*(?:to|it to)?\s*(the\s+)?([a-z ]{3,30})", r)
        if m:
            target = m.group(3).strip()
            target = re.split(r"\b(when|if|for|at|below|under|and)\b", target)[0].strip()
            if target and target not in ["it", "this", "that"]:
                c["escalation"]["route_to"] = target
                notes.append(f"escalation route → {target}")

    # posture (legacy)
    if "posture" in c:
        for p in ["replace", "augment", "support"]:
            if re.search(rf"\bposture\b.*\b{p}\b|\b{p}\b\s*posture|make it {p}", r):
                c["posture"] = p; notes.append(f"posture → {p}")
                break

    return c, notes


def llm_translate(cfg, request):
    """Use the Anthropic API to translate plain text into a config change."""
    import httpx
    schema = json.dumps(cfg, indent=2)
    sys = (
        "You edit an agent's JSON config. You are given the current config and a "
        "plain-English change request. Return ONLY the complete updated config as minified "
        "JSON — same shape, same keys, only the requested fields changed. Never invent keys. "
        "If the request could be risky (e.g. disabling confirm-before-action, lowering "
        "confidence thresholds very low, removing escalation safeguards), make the change "
        "but it will be flagged for review. Output JSON only, no prose, no code fences."
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
    """Guardrails: surface risky changes so the human decides with eyes open."""
    flags = []
    # Check confirm-before-action (new schema) and confirm_then_act (legacy)
    b_confirm = before.get("behavior", {}).get("confirm_before_action", before.get("graph", {}).get("confirm_then_act"))
    a_confirm = after.get("behavior", {}).get("confirm_before_action", after.get("graph", {}).get("confirm_then_act"))
    if b_confirm and not a_confirm:
        flags.append("Turns OFF confirm-before-action — the agent would act without a human confirming. Requires review before approving.")
    # Check confidence threshold (new schema and legacy)
    a_conf = after.get("behavior", {}).get("confidence_threshold", after.get("escalation", {}).get("confidence_threshold", 1))
    if a_conf < 0.4:
        flags.append("Confidence threshold very low — agent will rarely escalate. Verify this is intended.")
    # Check escalation threshold changes (new schema and legacy)
    b_esc = before.get("behavior", {}).get("escalation_threshold", before.get("escalation", {}).get("severity_escalates_at"))
    a_esc = after.get("behavior", {}).get("escalation_threshold", after.get("escalation", {}).get("severity_escalates_at"))
    if b_esc == "low" and a_esc == "high":
        flags.append("Raises escalation bar from low→high — more cases handled without a human. Verify this is intended.")
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
            {"id": k,
             "name": v.get("name", k),
             "description": v.get("description", ""),
             "account": v.get("account", ""),
             "status": v.get("status", "stopped"),
             "version": v.get("version", 1),
             "live": v.get("live", False),
             "type": v.get("type", "custom"),
             "endpoint": v.get("endpoint", {}),
             "containment": v.get("containment", 0),
             "resolution": v.get("resolution", 0),
             "escalation": v.get("escalation", 0),
             "clinical_flags": v.get("clinical_flags", 0),
             "data_sources_count": len(v.get("config", {}).get("data_sources", [])),
             "tools_count": len(v.get("config", {}).get("tools", [])),
             # Legacy field for backward compatibility with frontend
             "posture": v.get("config", {}).get("posture", v.get("type", "custom")),
             }
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
    _ensure_history(agent_id)
    return {"id": agent_id, **a, "history_count": len(HISTORY[agent_id])}

@app.post("/api/agents/{agent_id}/propose")
def propose(agent_id: str, body: ProposeIn):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    _ensure_history(agent_id)
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
    _ensure_history(agent_id)
    # snapshot current into history before applying
    HISTORY[agent_id].append({
        "version": a["version"], "at": datetime.now(timezone.utc).isoformat(),
        "by": body.approved_by, "note": p["request"],
        "config": copy.deepcopy(a["config"]), "changes": p["changes"],
    })
    a["config"] = p["after"]
    a["version"] += 1
    del PENDING[body.token]
    _save_agents()
    return {"ok": True, "agent_id": agent_id, "new_version": a["version"], "applied": p["changes"]}

@app.get("/api/agents/{agent_id}/history")
def history(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")
    _ensure_history(agent_id)
    return {"agent_id": agent_id, "current_version": AGENTS[agent_id]["version"],
            "history": list(reversed(HISTORY[agent_id]))}

@app.post("/api/agents/{agent_id}/revert/{version}")
def revert(agent_id: str, version: int):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    _ensure_history(agent_id)
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
    _save_agents()
    return {"ok": True, "reverted_to": version, "new_version": a["version"]}

@app.post("/api/agents/{agent_id}/control")
def control(agent_id: str, action: str = "start"):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    a["status"] = {"start": "running", "restart": "running", "stop": "stopped"}.get(action, a["status"])
    _save_agents()
    return {"ok": True, "status": a["status"]}


# ──────────────────────────────────────────────── agent registration & management

class RegisterAgentIn(BaseModel):
    name: str
    description: str = ""
    account: str = ""
    endpoint_type: str = "rest"  # rest | webhook | embedded
    endpoint_url: str = ""
    config: dict = {}

@app.post("/api/agents/register")
def register_agent(body: RegisterAgentIn):
    """Register a new agent."""
    agent_id = _slugify(body.name)
    if not agent_id:
        raise HTTPException(400, "name must produce a valid slug")
    # Ensure unique ID
    base_id = agent_id
    counter = 2
    while agent_id in AGENTS:
        agent_id = f"{base_id}-{counter}"
        counter += 1

    # Merge user config onto defaults
    cfg = _default_config()
    if body.config:
        for section in ("model", "execution", "behavior", "audit"):
            if section in body.config:
                cfg[section].update(body.config[section])
        if "data_sources" in body.config:
            cfg["data_sources"] = body.config["data_sources"]
        if "tools" in body.config:
            cfg["tools"] = body.config["tools"]

    agent = {
        "name": body.name,
        "description": body.description,
        "account": body.account or "Custom",
        "status": "stopped",
        "version": 1,
        "containment": 0, "resolution": 0, "escalation": 0, "clinical_flags": 0,
        "live": True,
        "type": "custom",
        "endpoint": {"type": body.endpoint_type, "url": body.endpoint_url},
        "config": cfg,
    }
    AGENTS[agent_id] = agent
    _ensure_history(agent_id)
    _save_agents()
    log_event(agent_id, "agent.registered", {"name": body.name})
    return {"ok": True, "agent_id": agent_id, "agent": {"id": agent_id, **agent}}

class ImportAgentIn(BaseModel):
    config_json: str  # raw JSON or YAML string
    source_format: str = "auto"  # auto | cortex | langchain | crewai | openai | raw

def _detect_and_normalize(raw: dict, source_format: str) -> dict:
    """Detect agent config format and normalize to CORTEX schema."""
    fmt = source_format

    if fmt == "auto":
        # Auto-detect format
        if "llm" in raw and "tasks" in raw:
            fmt = "crewai"
        elif "assistant_id" in raw or "instructions" in raw and "model" in raw.get("", {}).__class__.__name__ == "str":
            fmt = "openai"
        elif "llm" in raw and ("prompt" in raw or "chain_type" in raw or "agent_type" in raw):
            fmt = "langchain"
        elif "model" in raw and "execution" in raw:
            fmt = "cortex"
        else:
            fmt = "raw"

    result = {
        "name": raw.get("name", "Imported Agent"),
        "description": raw.get("description", ""),
        "source_format": fmt,
    }

    if fmt == "cortex":
        # Direct CORTEX config — pass through
        result["config"] = {k: raw[k] for k in ("model", "execution", "behavior", "data_sources", "tools", "audit") if k in raw}
        result["name"] = raw.get("name", result["name"])
        result["description"] = raw.get("description", result["description"])
        if "endpoint" in raw:
            result["endpoint_type"] = raw["endpoint"].get("type", "embedded")
            result["endpoint_url"] = raw["endpoint"].get("url", "")

    elif fmt == "langchain":
        llm = raw.get("llm", {})
        result["name"] = raw.get("name", raw.get("agent_type", "LangChain Agent"))
        result["description"] = raw.get("description", f"Imported from LangChain ({raw.get('agent_type', 'agent')})")
        result["config"] = {
            "model": {
                "provider": _detect_provider(llm.get("model_name", llm.get("model", ""))),
                "model_name": llm.get("model_name", llm.get("model", "unknown")),
                "temperature": llm.get("temperature", 0.7),
                "max_tokens": llm.get("max_tokens", 4096),
            },
            "tools": [{"name": t.get("name", t) if isinstance(t, dict) else str(t),
                        "description": t.get("description", "") if isinstance(t, dict) else "",
                        "parameters": t.get("args", []) if isinstance(t, dict) else [],
                        "rate_limit": 100} for t in raw.get("tools", [])],
        }

    elif fmt == "crewai":
        llm = raw.get("llm", {})
        result["name"] = raw.get("name", raw.get("role", "CrewAI Agent"))
        result["description"] = raw.get("goal", raw.get("description", f"Imported from CrewAI"))
        result["config"] = {
            "model": {
                "provider": _detect_provider(llm.get("model", "")),
                "model_name": llm.get("model", "unknown"),
                "temperature": llm.get("temperature", 0.7),
                "max_tokens": llm.get("max_tokens", 4096),
            },
            "tools": [{"name": t.get("name", str(t)) if isinstance(t, dict) else str(t),
                        "description": t.get("description", "") if isinstance(t, dict) else "",
                        "parameters": [], "rate_limit": 100} for t in raw.get("tools", [])],
        }
        if raw.get("backstory"):
            result["description"] += f" | Backstory: {raw['backstory'][:200]}"

    elif fmt == "openai":
        result["name"] = raw.get("name", "OpenAI Assistant")
        result["description"] = raw.get("description", raw.get("instructions", "")[:200])
        model_name = raw.get("model", "gpt-5.6-terra")
        result["config"] = {
            "model": {
                "provider": "openai",
                "model_name": model_name,
                "temperature": raw.get("temperature", 0.7),
                "max_tokens": raw.get("max_tokens", 4096),
            },
            "tools": [{"name": t.get("type", t.get("function", {}).get("name", "tool")),
                        "description": t.get("function", {}).get("description", ""),
                        "parameters": list(t.get("function", {}).get("parameters", {}).get("properties", {}).keys()) if isinstance(t, dict) else [],
                        "rate_limit": 100} for t in raw.get("tools", [])],
        }

    else:  # raw — best effort
        result["name"] = raw.get("name", raw.get("agent_name", "Imported Agent"))
        result["description"] = raw.get("description", raw.get("desc", "Imported agent"))
        result["config"] = {}
        # Try to find model info anywhere
        for mk in ("model", "llm", "model_config"):
            if mk in raw and isinstance(raw[mk], dict):
                result["config"]["model"] = {
                    "provider": _detect_provider(raw[mk].get("model_name", raw[mk].get("model", raw[mk].get("name", "")))),
                    "model_name": raw[mk].get("model_name", raw[mk].get("model", raw[mk].get("name", "unknown"))),
                    "temperature": raw[mk].get("temperature", 0.7),
                    "max_tokens": raw[mk].get("max_tokens", 4096),
                }
                break
        if "tools" in raw and isinstance(raw["tools"], list):
            result["config"]["tools"] = [{"name": t.get("name", str(t)) if isinstance(t, dict) else str(t),
                                           "description": t.get("description", "") if isinstance(t, dict) else "",
                                           "parameters": [], "rate_limit": 100} for t in raw["tools"]]

    return result

def _detect_provider(model_name: str) -> str:
    m = model_name.lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    elif "gpt" in m or "o3" in m or "o4" in m:
        return "openai"
    elif "gemini" in m:
        return "gemini"
    elif "grok" in m or "xai" in m:
        return "xai"
    elif "sonar" in m or "perplexity" in m:
        return "perplexity"
    elif "mistral" in m or "mixtral" in m or "codestral" in m or "pixtral" in m:
        return "mistral"
    elif "command" in m or "cohere" in m:
        return "cohere"
    elif "llama" in m or "meta" in m:
        return "meta"
    return "anthropic"

@app.post("/api/agents/import")
def import_agent(body: ImportAgentIn):
    """Import an agent from JSON config (supports LangChain, CrewAI, OpenAI Assistants, or raw)."""
    import yaml as yaml_mod
    try:
        # Try JSON first, then YAML
        try:
            raw = json.loads(body.config_json)
        except json.JSONDecodeError:
            try:
                raw = yaml_mod.safe_load(body.config_json)
            except Exception:
                raise HTTPException(400, "Could not parse config as JSON or YAML")
        if not isinstance(raw, dict):
            raise HTTPException(400, "Config must be a JSON/YAML object")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Parse error: {str(e)}")

    normalized = _detect_and_normalize(raw, body.source_format)

    # Register via the same path
    agent_id = _slugify(normalized["name"])
    if not agent_id:
        agent_id = "imported-agent"
    base_id = agent_id
    counter = 2
    while agent_id in AGENTS:
        agent_id = f"{base_id}-{counter}"
        counter += 1

    cfg = _default_config()
    imp_cfg = normalized.get("config", {})
    for section in ("model", "execution", "behavior", "audit"):
        if section in imp_cfg:
            cfg[section].update(imp_cfg[section])
    if "data_sources" in imp_cfg:
        cfg["data_sources"] = imp_cfg["data_sources"]
    if "tools" in imp_cfg:
        cfg["tools"] = imp_cfg["tools"]

    agent = {
        "name": normalized["name"],
        "description": normalized["description"],
        "account": f"Imported ({normalized.get('source_format', 'auto')})",
        "status": "stopped",
        "version": 1,
        "containment": 0, "resolution": 0, "escalation": 0, "clinical_flags": 0,
        "live": True,
        "type": "custom",
        "endpoint": {"type": normalized.get("endpoint_type", "embedded"), "url": normalized.get("endpoint_url", "")},
        "config": cfg,
    }
    AGENTS[agent_id] = agent
    _ensure_history(agent_id)
    _save_agents()
    log_event(agent_id, "agent.imported", {"name": normalized["name"], "source_format": normalized.get("source_format")})
    return {"ok": True, "agent_id": agent_id, "detected_format": normalized.get("source_format", "raw"),
            "agent": {"id": agent_id, **agent}}

@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Remove an agent."""
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")
    name = AGENTS[agent_id].get("name", agent_id)
    del AGENTS[agent_id]
    HISTORY.pop(agent_id, None)
    RUNS.pop(agent_id, None)
    _save_agents()
    log_event(agent_id, "agent.deleted", {"name": name})
    return {"ok": True, "deleted": agent_id}


# ──────────────────────────────────────────────── data sources

class DataSourceIn(BaseModel):
    name: str
    type: str = "api"  # api | database | file | webhook | graphql | grpc | custom
    endpoint: str = ""
    auth_type: str = "api_key"  # api_key | oauth2 | bearer | basic | connection_string | iam | none
    auth_value: str = ""
    refresh: str = "manual"  # realtime | 5m | 1h | 1d | manual

@app.post("/api/agents/{agent_id}/data-sources")
def add_data_source(agent_id: str, body: DataSourceIn):
    """Add a data source to an agent's config."""
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    ds = {"name": body.name, "type": body.type, "endpoint": body.endpoint,
          "auth_type": body.auth_type, "auth_value": body.auth_value, "refresh": body.refresh}
    a["config"].setdefault("data_sources", []).append(ds)
    _save_agents()
    log_event(agent_id, "datasource.added", {"name": body.name})
    return {"ok": True, "data_source": ds, "total": len(a["config"]["data_sources"])}

@app.delete("/api/agents/{agent_id}/data-sources/{source_name}")
def remove_data_source(agent_id: str, source_name: str):
    """Remove a data source from an agent's config."""
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    sources = a["config"].get("data_sources", [])
    before_len = len(sources)
    a["config"]["data_sources"] = [s for s in sources if s.get("name") != source_name]
    if len(a["config"]["data_sources"]) == before_len:
        raise HTTPException(404, "data source not found")
    _save_agents()
    log_event(agent_id, "datasource.removed", {"name": source_name})
    return {"ok": True, "removed": source_name, "remaining": len(a["config"]["data_sources"])}


# ──────────────────────────────────────────────── integration code generation

@app.get("/api/agents/{agent_id}/integration/{fmt}")
def generate_integration(agent_id: str, fmt: str):
    """Generate client integration code for an agent."""
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")

    endpoint = a.get("endpoint", {})
    ep_url = endpoint.get("url") or "http://localhost:3000/api/agents/{agent_id}/run"
    agent_name = a.get("name", agent_id)
    tools = a.get("config", {}).get("tools", [])
    tool_names = [t["name"] for t in tools]

    if fmt == "python":
        code = f'''"""Client for {agent_name}"""
import requests

AGENT_URL = "{ep_url}"

def run_agent(message: str, **kwargs):
    """Send a message to the {agent_name} agent."""
    resp = requests.post(AGENT_URL, json={{"claim": message, **kwargs}})
    resp.raise_for_status()
    return resp.json()

# Available tools: {", ".join(tool_names) or "none configured"}

if __name__ == "__main__":
    result = run_agent("Hello, agent!")
    print(result)
'''
    elif fmt == "javascript":
        code = f'''// Client for {agent_name}
const AGENT_URL = "{ep_url}";

async function runAgent(message, options = {{}}) {{
  const resp = await fetch(AGENT_URL, {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{claim: message, ...options}})
  }});
  if (!resp.ok) throw new Error(`Agent error: ${{resp.status}}`);
  return resp.json();
}}

// Available tools: {", ".join(tool_names) or "none configured"}

export {{ runAgent }};
'''
    elif fmt == "curl":
        code = f'''# {agent_name} — cURL example
curl -X POST "{ep_url}" \\
  -H "Content-Type: application/json" \\
  -d '{{"claim": "Your message here"}}'

# Available tools: {", ".join(tool_names) or "none configured"}
'''
    elif fmt == "openapi":
        spec = {
            "openapi": "3.0.3",
            "info": {"title": agent_name, "description": a.get("description", ""), "version": str(a.get("version", 1))},
            "paths": {
                "/run": {
                    "post": {
                        "summary": f"Run {agent_name}",
                        "requestBody": {"content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {"claim": {"type": "string", "description": "Input message"}},
                            "required": ["claim"]
                        }}}},
                        "responses": {"200": {"description": "Agent response"}}
                    }
                }
            },
            "servers": [{"url": ep_url}]
        }
        code = json.dumps(spec, indent=2)
    elif fmt == "webhook":
        code = f'''# Webhook setup for {agent_name}
# POST events to: http://localhost:3000/webhooks/{agent_id}/{{event_type}}
#
# Supported event types are configured in the Automation tab.
# The agent will execute when matching events are received.
#
# Example:
curl -X POST "http://localhost:3000/webhooks/{agent_id}/data_update" \\
  -H "Content-Type: application/json" \\
  -d '{{"event": "data_update", "payload": {{}}}}'
'''
    else:
        raise HTTPException(400, f"unsupported format: {fmt}. Use: python, javascript, curl, openapi, webhook")

    return {"ok": True, "format": fmt, "agent_id": agent_id, "code": code}


class RunIn(BaseModel):
    claim: str


# Log of real agent runs, per agent id.
RUNS: dict[str, list] = {}


@app.post("/api/agents/{agent_id}/run")
def run_live_agent(agent_id: str, body: RunIn):
    """
    Execute an agent using its CURRENT Cortex config.
    Behavior depends on the agent's endpoint type:
    - embedded: Run using the active LLM provider with the agent's tools
    - rest: POST to the agent's configured endpoint URL
    - webhook: Return instructions for the client to send data
    """
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if not a.get("live"):
        raise HTTPException(400, "this agent is not marked as live")

    run_start = datetime.now(timezone.utc).isoformat()
    log_event(agent_id, "run.start", {"input": body.claim[:200], "config_version": a["version"]})

    cfg = a["config"]
    endpoint = a.get("endpoint", {})
    ep_type = endpoint.get("type", "embedded")

    # ── Webhook endpoint: return instructions ──
    if ep_type == "webhook":
        log_event(agent_id, "run.webhook_info", {"endpoint": endpoint.get("url", "")})
        webhook_now = datetime.now(timezone.utc).isoformat()
        rec = {
            "claim": body.claim,
            "outcome": "WEBHOOK_PENDING",
            "published": False,
            "steps_used": 0,
            "config_version": a["version"],
            "provider": "webhook",
            "model": "",
            "trace": [{"kind": "info", "text": f"Send data to webhook: {endpoint.get('url', 'not configured')}"}],
            "started_at": run_start,
            "finished_at": webhook_now,
            "detail": {
                "summary": f"Webhook agent — POST your data to the configured endpoint or use /webhooks/{agent_id}/{{event_type}}",
                "reason": "", "citations": [], "route_to": None
            }
        }
        RUNS.setdefault(agent_id, []).insert(0, rec)
        del RUNS[agent_id][12:]
        return {"ok": True, "run": rec}

    # ── REST endpoint: proxy to external URL ──
    if ep_type == "rest" and endpoint.get("url"):
        import httpx
        try:
            with httpx.Client(timeout=cfg.get("execution", {}).get("timeout_seconds", 120)) as client:
                resp = client.post(endpoint["url"], json={"claim": body.claim, "config": cfg})
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            log_event(agent_id, "run.error", {"message": str(e)})
            rest_err_end = datetime.now(timezone.utc).isoformat()
            rec = {
                "claim": body.claim, "outcome": "ERROR", "published": False,
                "steps_used": 0, "config_version": a["version"],
                "provider": "rest", "model": "",
                "trace": [{"kind": "error", "text": str(e)}],
                "started_at": run_start, "finished_at": rest_err_end,
                "detail": {"summary": "", "reason": str(e), "citations": [], "route_to": None}
            }
            RUNS.setdefault(agent_id, []).insert(0, rec)
            del RUNS[agent_id][12:]
            return {"ok": False, "error": str(e), "run": rec}

        rest_end = datetime.now(timezone.utc).isoformat()
        rec = {
            "claim": body.claim, "outcome": "COMPLETED", "published": True,
            "steps_used": 1, "config_version": a["version"],
            "provider": "rest", "model": "",
            "trace": [{"kind": "rest_call", "url": endpoint["url"], "status": "ok"}],
            "started_at": run_start, "finished_at": rest_end,
            "detail": {"summary": json.dumps(result)[:1200], "reason": "", "citations": [], "route_to": None}
        }
        RUNS.setdefault(agent_id, []).insert(0, rec)
        del RUNS[agent_id][12:]
        log_event(agent_id, "run.complete", {"steps": 1, "published": True})
        return {"ok": True, "run": rec}

    # ── Embedded: run using the active LLM provider ──
    model_cfg = cfg.get("model", {})
    provider = model_cfg.get("provider", SETTINGS["active"])
    key = get_key(provider)
    if not key:
        return {"ok": False, "error": f"No API key for {providers_mod.PROVIDER_LABELS.get(provider, provider)}. Add one in Settings."}

    behavior = cfg.get("behavior", {})
    tools_cfg = cfg.get("tools", [])
    execution = cfg.get("execution", {})

    # Build system prompt from agent config
    system_parts = [f"You are {a.get('name', agent_id)}."]
    if a.get("description"):
        system_parts.append(a["description"])
    system_parts.append(f"\nOPERATING CONFIG (live from Cortex — obey it):")
    system_parts.append(f"- confidence threshold: {behavior.get('confidence_threshold', 0.75)}")
    system_parts.append(f"- escalation threshold: {behavior.get('escalation_threshold', 'high')}")
    if behavior.get("confirm_before_action"):
        system_parts.append("- confirm before action: state what you will do before doing it.")
    if behavior.get("auto_escalate_on_error"):
        system_parts.append("- auto-escalate on error: escalate to a human if an error occurs.")
    if tools_cfg:
        system_parts.append(f"- available tools: {', '.join(t['name'] for t in tools_cfg)}")
    system = "\n".join(system_parts)

    # Build tool definitions for the provider
    tools = []
    for t in tools_cfg:
        tools.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": {p: {"type": "string"} for p in t.get("parameters", [])},
            }
        })

    def _default_tool_handler(name, input_data):
        """Default tool handler that returns a placeholder response."""
        return f"[Tool '{name}' called with {json.dumps(input_data)}. No handler registered — returning placeholder.]"

    res = providers_mod.run_tool_loop(
        provider=provider, api_key=key,
        model=model_cfg.get("model_name") or get_model(provider),
        system=system, tools=tools, user_message=body.claim,
        process_tool_call=_default_tool_handler,
        max_iterations=execution.get("max_retries", 3) + 1)

    trace = res["trace"]
    if res["ok"]:
        outcome = "ESCALATED" if res["escalated"] else "COMPLETED"
        published = not res["escalated"]
        trace.append({"kind": "conclude", "verdict": outcome, "confidence": "—", "citations": []})
        if res["escalated"]:
            log_event(agent_id, "run.escalate", {"reason": res.get("escalation_reason", "threshold")})
        else:
            log_event(agent_id, "run.complete", {"steps": res["steps_used"], "published": True})
    else:
        outcome, published = "ERROR", False
        log_event(agent_id, "run.error", {"message": res.get("error", "unknown error")})

    run_end = datetime.now(timezone.utc).isoformat()
    rec = {
        "claim": body.claim, "outcome": outcome, "published": published,
        "steps_used": res["steps_used"], "config_version": a["version"],
        "provider": provider,
        "model": model_cfg.get("model_name") or get_model(provider),
        "trace": trace,
        "started_at": run_start,
        "finished_at": run_end,
        "detail": {
            "summary": (res.get("final_text") or "")[:1200],
            "reason": res.get("error", ""),
            "citations": [],
            "route_to": None
        }
    }
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


@app.get("/api/events")
def get_events(agent_id: str = None, limit: int = 100):
    """Get event log, optionally filtered by agent_id."""
    events = EVENT_LOG[:limit]
    if agent_id:
        events = [e for e in events if e["agent_id"] == agent_id]
    return {"events": events, "total": len(EVENT_LOG)}


@app.get("/api/agents/{agent_id}/diagnosis")
def get_agent_diagnosis(agent_id: str):
    """Get diagnostic analysis for an agent."""
    if agent_id not in AGENTS:
        raise HTTPException(404, "agent not found")
    return diagnose_agent(agent_id)


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
        "agent_id": "customer-support-bot", "agent": "Customer Support Agent", "account": "Acme Corp",
        "verdict": "config", "scenario": "multilingual-routing",
        "observation": "Non-English support requests dropped ~40% in accuracy after v1.8.2 deploy; escalation rate on non-English queries jumped.",
        "expected": "Correct intent classification across languages, escalation only on genuinely complex cases.",
        "actual": "Partial understanding; agent escalates almost everything in non-English languages.",
        "evidence": "Prompt diff v1.8.1→v1.8.2 shows the multilingual classification block was dropped during a merge. English path untouched.",
        "fix": "Revert the multilingual prompt block to v1.8.1, re-run the multilingual test suite, review with team lead, then roll forward.",
        "owner": "PM (me) — config fix, ~15 min + team sign-off",
    },
    {
        "agent_id": "request-router", "agent": "Request Router", "account": "GlobalTech",
        "verdict": "platform", "scenario": "priority-escalation",
        "observation": "High-priority requests stopped escalating to the on-call team within the 2-minute SLA.",
        "expected": "priority=HIGH extracted → escalate to on-call team.",
        "actual": "Routed to the general queue. priority_level comes back null.",
        "evidence": "Logs show priority_level: null while request details extract fine ('system outage, production down'). Regression started the day the extraction model was updated — config unchanged.",
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
                              "from_env": (not SETTINGS["keys"].get(p)) and bool(os.environ.get(_ENV_KEYS.get(p, ""), "")),
                              "model": get_model(p),
                              "label": providers_mod.PROVIDER_LABELS[p]}
                          for p in providers_mod.ALL_PROVIDERS}}


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    if body.active:
        if body.active not in providers_mod.ALL_PROVIDERS:
            raise HTTPException(400, "unknown provider")
        SETTINGS["active"] = body.active
    if body.keys:
        for p, k in body.keys.items():
            if p in providers_mod.ALL_PROVIDERS:
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
    if provider not in providers_mod.ALL_PROVIDERS:
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
def index(request: Request):
    sess = _get_session(request)
    if not sess:
        return HTMLResponse(LOGIN_HTML)
    return HTMLResponse(HTML)


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>CORTEX — Sign In</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;font-family:'IBM Plex Sans',system-ui,sans-serif}
body{background:linear-gradient(135deg,#1a1008 0%,#2d1810 40%,#1a1008 100%);display:flex;align-items:center;justify-content:center;min-height:100vh}
.login-card{background:#fffcf8;border-radius:12px;padding:40px 36px 36px;width:400px;max-width:92vw;box-shadow:0 20px 60px rgba(0,0,0,.4),0 0 0 1px rgba(196,99,42,.15)}
.logo-row{text-align:center;margin-bottom:28px}
.logo{font-family:'Archivo';font-weight:800;font-size:32px;letter-spacing:.14em;padding-left:.14em;background:linear-gradient(135deg,#f97316,#fbbf24);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:12px;letter-spacing:.08em;color:#92400e;margin-top:2px}
.tab-row{display:flex;gap:0;margin-bottom:24px;border-bottom:2px solid #e6ddd3}
.tab-btn{flex:1;padding:10px 0;text-align:center;font-size:12px;font-weight:600;letter-spacing:.06em;color:#8a93a0;border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;font-family:'IBM Plex Mono'}
.tab-btn.active{color:#c4632a;border-bottom-color:#c4632a}
.tab-btn:hover{color:#c4632a}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#92400e;margin-bottom:5px}
.form-group input,.form-group select{width:100%;padding:10px 12px;border:1px solid #e6ddd3;border-radius:6px;font-size:13px;font-family:'IBM Plex Sans';background:#f5f0eb;transition:border-color .15s}
.form-group input:focus,.form-group select:focus{outline:none;border-color:#c4632a;background:#fff}
.btn-primary{width:100%;padding:12px;background:linear-gradient(135deg,#c4632a,#ea580c);color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;font-family:'IBM Plex Sans';letter-spacing:.04em;transition:transform .1s,box-shadow .15s}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 4px 16px rgba(196,99,42,.35)}
.btn-primary:active{transform:translateY(0)}
.err{color:#dc2626;font-size:12px;margin-bottom:12px;min-height:18px}
.divider{display:flex;align-items:center;gap:12px;margin:20px 0;color:#8a93a0;font-size:11px;letter-spacing:.06em}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:#e6ddd3}
.sso-btn{width:100%;padding:11px;border:1px solid #e6ddd3;border-radius:6px;background:#fff;cursor:pointer;font-size:12px;font-family:'IBM Plex Sans';color:#14181f;display:flex;align-items:center;justify-content:center;gap:8px;transition:all .15s;margin-bottom:8px}
.sso-btn:hover{border-color:#c4632a;background:#fbe8dc}
.sso-btn svg{width:16px;height:16px}
.footer{text-align:center;margin-top:20px;font-size:10px;color:#8a93a0;letter-spacing:.04em}
.hidden{display:none}
</style></head>
<body>

<div class="login-card">
  <div class="logo-row">
    <div class="logo">CORTEX</div>
    <div class="sub">Agent Ops Hub</div>
  </div>

  <div class="tab-row">
    <button class="tab-btn active" id="tab-login" onclick="switchTab('login')">SIGN IN</button>
    <button class="tab-btn" id="tab-signup" onclick="switchTab('signup')">CREATE ACCOUNT</button>
  </div>

  <div id="err" class="err"></div>

  <!-- Login Form -->
  <form id="form-login" onsubmit="doLogin(event)">
    <div class="form-group">
      <label>Email</label>
      <input type="email" id="login-email" required placeholder="you@company.com"/>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" id="login-password" required placeholder="Enter your password"/>
    </div>
    <button type="submit" class="btn-primary">Sign In</button>
  </form>

  <!-- Signup Form -->
  <form id="form-signup" class="hidden" onsubmit="doSignup(event)">
    <div class="form-group">
      <label>Full Name</label>
      <input type="text" id="signup-name" required placeholder="Jane Smith"/>
    </div>
    <div class="form-group">
      <label>Email</label>
      <input type="email" id="signup-email" required placeholder="you@company.com"/>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" id="signup-password" required minlength="6" placeholder="At least 6 characters"/>
    </div>
    <div class="form-group">
      <label>Role</label>
      <select id="signup-role">
        <option value="FDE">FDE (Field Delivery Engineer)</option>
        <option value="PM">Product Manager</option>
        <option value="Engineer">Engineer</option>
        <option value="Admin">Admin</option>
      </select>
    </div>
    <div class="form-group">
      <label>Organization</label>
      <input type="text" id="signup-org" placeholder="Acme Corp (optional)"/>
    </div>
    <button type="submit" class="btn-primary">Create Account</button>
  </form>

  <div class="divider">or continue with</div>

  <button class="sso-btn" onclick="ssoPlaceholder('Google')">
    <svg viewBox="0 0 24 24"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>
    Sign in with Google SSO
  </button>
  <button class="sso-btn" onclick="ssoPlaceholder('SAML')">
    <svg viewBox="0 0 24 24" fill="none" stroke="#5a6472" stroke-width="2"><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
    Sign in with SAML / Okta
  </button>

  <div class="footer">CORTEX v0.2 · Secure single-session auth</div>
</div>

<script>
function switchTab(tab){
  document.getElementById('tab-login').classList.toggle('active', tab==='login');
  document.getElementById('tab-signup').classList.toggle('active', tab==='signup');
  document.getElementById('form-login').classList.toggle('hidden', tab!=='login');
  document.getElementById('form-signup').classList.toggle('hidden', tab!=='signup');
  document.getElementById('err').textContent='';
}

async function doLogin(e){
  e.preventDefault();
  const err=document.getElementById('err');
  err.textContent='';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('login-email').value,password:document.getElementById('login-password').value})});
    if(!r.ok){const d=await r.json(); err.textContent=d.detail||'Login failed'; return;}
    const d=await r.json();
    document.cookie='cortex_session='+d.token+';path=/;SameSite=Strict';
    window.location.reload();
  }catch(ex){err.textContent='Network error';}
}

async function doSignup(e){
  e.preventDefault();
  const err=document.getElementById('err');
  err.textContent='';
  try{
    const r=await fetch('/api/auth/signup',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('signup-name').value,email:document.getElementById('signup-email').value,
        password:document.getElementById('signup-password').value,role:document.getElementById('signup-role').value,
        org:document.getElementById('signup-org').value})});
    if(!r.ok){const d=await r.json(); err.textContent=d.detail||'Signup failed'; return;}
    const d=await r.json();
    document.cookie='cortex_session='+d.token+';path=/;SameSite=Strict';
    window.location.reload();
  }catch(ex){err.textContent='Network error';}
}

function ssoPlaceholder(provider){
  document.getElementById('err').textContent=provider+' SSO coming soon — use email sign-in for now';
}
</script>
</body></html>"""

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>CORTEX — Agent Ops Hub</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#14181f;--paper:#f5f0eb;--card:#fffcf8;--muted:#5a6472;--faint:#8a93a0;--line:#e6ddd3;--line2:#d9cfc3;--accent:#c4632a;--accentsoft:#fbe8dc;--seal:#0e5b54;--sealsoft:#e2efec;--ochre:#9a6614;--ochresoft:#f3ead6;--brick:#9c3327;--bricksoft:#f4e2df;--sunset1:#f97316;--sunset2:#ea580c;--sunset3:#dc2626;--warm:#d97706;--warmsoft:#fef3c7;--terra:#92400e;--terrasoft:#fde68a}
html{font-family:'IBM Plex Sans',system-ui,sans-serif;background:var(--paper);color:var(--ink)}
.mono{font-family:'IBM Plex Mono',monospace}
.header{background:linear-gradient(135deg,#1a1008 0%,#2d1810 50%,#1a1008 100%);color:#fff;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid var(--accent)}
.brand{display:flex;align-items:baseline;gap:12px}
.logo{font-family:'Archivo';font-weight:800;font-size:20px;letter-spacing:.14em;padding-left:.14em;background:linear-gradient(135deg,#f97316,#fbbf24);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:11px;letter-spacing:.08em;color:#c4956a}
.nav{display:flex;gap:4px;flex-wrap:wrap}
.navbtn{background:none;border:1px solid #3d2a1a;color:#c4956a;padding:5px 10px;font-size:10.5px;cursor:pointer;border-radius:3px;font-family:'IBM Plex Mono';letter-spacing:.04em;transition:all .15s}
.navbtn:hover{border-color:#c4632a;color:#f97316}
.navbtn.active{background:linear-gradient(135deg,#c4632a,#ea580c);border-color:#c4632a;color:#fff}
.hmeta{font-size:11px;color:#c4956a;font-family:'IBM Plex Mono'}
.view{max-width:1340px;margin:0 auto;padding:18px 22px 70px}
.wrap{display:grid;grid-template-columns:minmax(260px,320px) 1fr;gap:20px;align-items:start}
h2{font-family:'Archivo';font-size:16px;font-weight:700;margin:0 0 14px;color:var(--ink)}
h3{font-family:'Archivo';font-size:17px;font-weight:700;margin:0}
h4{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin:0 0 10px}
.grid{display:flex;flex-direction:column;gap:7px}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--faint);border-radius:4px;padding:11px 12px;cursor:pointer;text-align:left;width:100%;font:inherit;color:inherit;transition:all .15s}
.card:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(196,99,42,.1)}
.card.active{border-color:var(--accent);border-left-color:var(--accent);box-shadow:0 2px 12px rgba(196,99,42,.15)}
.card[data-s=running]{border-left-color:var(--seal)}
.card[data-s=error]{border-left-color:var(--brick)}
.card[data-s=stopped]{border-left-color:var(--faint)}
.ctop{display:flex;justify-content:space-between;gap:8px;margin-bottom:5px}
.cname{font-weight:600;font-size:13px}
.cstat{font-size:10px;color:var(--faint);font-family:'IBM Plex Mono'}
.cmeta{display:flex;gap:10px;font-size:11px;color:var(--muted);font-family:'IBM Plex Mono'}
.typetag{display:inline-block;font-family:'IBM Plex Mono';font-size:9px;letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;border-radius:3px;margin-top:6px}
.typetag.sample{background:var(--ochresoft);color:var(--ochre)}
.typetag.custom{background:var(--accentsoft);color:var(--accent)}
.typetag.imported{background:var(--sealsoft);color:var(--seal)}
.livetag{display:inline-block;font-family:'IBM Plex Mono';font-size:9px;letter-spacing:.06em;padding:2px 6px;border-radius:3px;background:var(--seal);color:#fff;margin-top:6px;margin-left:5px}
.eptag{display:inline-block;font-family:'IBM Plex Mono';font-size:9px;letter-spacing:.06em;padding:2px 6px;border-radius:3px;background:#f3e8ff;color:#7c3aed;margin-top:6px;margin-left:5px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.phead{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.acct{font-family:'IBM Plex Mono';font-size:11px;color:var(--accent);letter-spacing:.05em;margin-top:5px}
.vtag{font-family:'IBM Plex Mono';font-size:11px;color:var(--faint)}
.sect{padding:18px 20px;border-bottom:1px solid var(--line)}
.sect:last-child{border-bottom:none}
.ctrls{display:flex;gap:6px;flex-wrap:wrap}
.btn{background:var(--ink);color:#fff;border:none;padding:7px 14px;font-size:12px;border-radius:3px;cursor:pointer;font-family:'IBM Plex Sans';font-weight:500;transition:all .15s}
.btn:hover{opacity:.92;transform:translateY(-1px)}
.btn.ghost{background:none;color:var(--ink);border:1px solid var(--line2)}
.btn.accent{background:linear-gradient(135deg,#c4632a,#ea580c);color:#fff}
.btn.seal{background:var(--seal)}
.btn:disabled{opacity:.5;cursor:default;transform:none}
.cfg{background:#faf8f5;border:1px solid var(--line);border-radius:3px;padding:12px 13px;font-family:'IBM Plex Mono';font-size:12px;line-height:1.7}
.cfg .k{color:var(--muted)}
.cfg .v{color:var(--ink);font-weight:500}
.ask{width:100%;border:1px solid var(--line2);border-radius:3px;padding:11px 12px;font:inherit;font-size:14px;resize:vertical;min-height:64px;background:#fffcf8}
.ask:focus{outline:2px solid var(--accent);border-color:var(--accent)}
.hint{font-size:11.5px;color:var(--faint);margin-top:8px;line-height:1.5}
.hint code{background:var(--accentsoft);padding:1px 5px;border-radius:2px;font-size:11px;color:var(--accent)}
.diff{margin-top:14px;border:1px solid var(--line);border-radius:3px;overflow:hidden}
.diffhead{background:#faf5ef;padding:9px 13px;font-family:'IBM Plex Mono';font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between}
.drow{display:grid;grid-template-columns:1.2fr 1fr auto 1fr;gap:12px;padding:10px 13px;border-top:1px solid var(--line);align-items:center;font-size:12px}
.dfield{color:var(--muted)}
.dfrom{color:var(--brick);text-decoration:line-through;opacity:.8}
.dto{color:var(--seal);font-weight:500}
.darrow{color:var(--faint)}
.flag{background:var(--bricksoft);border:1px solid #e6c9c4;border-radius:3px;padding:10px 12px;font-size:12.5px;color:var(--brick);margin-top:12px;line-height:1.5}
.gate{display:flex;gap:8px;margin-top:14px;align-items:center;flex-wrap:wrap}
.gatemsg{font-size:12px;color:var(--muted)}
.applied{background:var(--sealsoft);border:1px solid #bfe0d9;border-radius:3px;padding:10px 12px;font-size:12.5px;color:var(--seal);margin-top:12px}
.hist{display:flex;flex-direction:column;gap:8px}
.hrow{background:#faf8f5;border:1px solid var(--line);border-radius:3px;padding:10px 12px;font-size:12px}
.htop{display:flex;justify-content:space-between;margin-bottom:4px}
.hver{font-family:'Archivo';font-weight:600}
.hat{font-family:'IBM Plex Mono';font-size:10px;color:var(--faint)}
.hby{font-family:'IBM Plex Mono';font-size:10px;color:var(--faint);margin-top:3px}
.empty{color:var(--faint);font-size:12.5px;padding:6px 0;line-height:1.6}
.spin{display:inline-block;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
/* monitor dashboard */
.dash-hero{background:linear-gradient(135deg,#faf5ef 0%,#fde8d8 50%,#fef3c7 100%);border:1px solid var(--line);border-radius:6px;padding:20px 24px;margin-bottom:20px}
.dash-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.stat-card{background:var(--card);border:1px solid var(--line);border-radius:5px;padding:16px;text-align:center;transition:transform .15s}
.stat-card:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(196,99,42,.08)}
.stat-card .n{font-family:'Archivo';font-weight:800;font-size:28px;background:linear-gradient(135deg,#c4632a,#ea580c);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-card .n.green{background:linear-gradient(135deg,#0e5b54,#16a34a);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-card .n.warn{background:linear-gradient(135deg,#9c3327,#dc2626);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-card .l{font-family:'IBM Plex Mono';font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-top:6px}
.agent-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-bottom:20px}
.agent-card{background:var(--card);border:1px solid var(--line);border-radius:5px;padding:16px;cursor:pointer;transition:all .15s;position:relative;overflow:hidden}
.agent-card:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(196,99,42,.1);border-color:var(--accent)}
.agent-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.agent-card[data-s=running]::before{background:linear-gradient(90deg,var(--seal),#16a34a)}
.agent-card[data-s=error]::before{background:linear-gradient(90deg,var(--brick),#dc2626)}
.agent-card[data-s=stopped]::before{background:var(--faint)}
.agent-card .ac-name{font-weight:600;font-size:14px;margin-bottom:2px}
.agent-card .ac-desc{font-size:11.5px;color:var(--muted);margin-bottom:8px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.agent-card .ac-metrics{display:flex;gap:12px;font-family:'IBM Plex Mono';font-size:10.5px;color:var(--muted)}
.agent-card .ac-metrics .metric-val{font-weight:600;color:var(--ink)}
.pill{display:inline-block;padding:2px 7px;border-radius:3px;font-family:'IBM Plex Mono';font-size:10px}
.pill.running{background:var(--sealsoft);color:var(--seal)}.pill.error{background:var(--bricksoft);color:var(--brick)}.pill.stopped{background:#eceef1;color:var(--muted)}
/* activity feed */
.feed-panel{background:var(--card);border:1px solid var(--line);border-radius:5px;overflow:hidden}
.feed-head{padding:12px 16px;background:#faf5ef;border-bottom:1px solid var(--line);font-family:'Archivo';font-weight:700;font-size:13px;display:flex;justify-content:space-between;align-items:center}
.feed-item{padding:10px 16px;border-bottom:1px solid var(--line);font-size:11.5px;display:flex;gap:10px;align-items:start}
.feed-item:last-child{border-bottom:none}
.feed-dot{width:8px;height:8px;border-radius:50%;margin-top:4px;flex-shrink:0}
.feed-dot.start{background:var(--seal)}.feed-dot.complete{background:#16a34a}.feed-dot.error{background:var(--brick)}.feed-dot.escalate{background:var(--ochre)}.feed-dot.register{background:var(--accent)}
/* system health */
.health-row{display:flex;gap:8px;align-items:center;padding:8px 0;font-size:12px}
.health-dot{width:10px;height:10px;border-radius:50%}
.health-dot.green{background:#16a34a}.health-dot.yellow{background:#eab308}.health-dot.red{background:#dc2626}.health-dot.gray{background:#9ca3af}
/* diagnostics */
.dgcase{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:18px 20px;margin-bottom:16px}
.dgcase.platform{border-left:3px solid var(--brick)}
.dgcase.config{border-left:3px solid var(--ochre)}
.verdict{display:inline-block;font-family:'IBM Plex Mono';font-size:10px;letter-spacing:.08em;text-transform:uppercase;padding:3px 8px;border-radius:3px;margin-left:10px}
.verdict.platform{background:var(--bricksoft);color:var(--brick)}
.verdict.config{background:var(--ochresoft);color:var(--ochre)}
.dgline{font-size:13px;margin-top:10px;line-height:1.55}
.dgline b{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.04em;text-transform:uppercase;display:block;margin-bottom:2px}
.dgev{background:#faf8f5;border:1px solid var(--line);border-radius:3px;padding:10px 12px;font-family:'IBM Plex Mono';font-size:11.5px;color:var(--muted);margin-top:4px;line-height:1.5}
/* run trace */
.livebox{background:linear-gradient(180deg,#faf5ef,transparent 70%)}
.runbox{margin-top:14px;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.runhead{display:flex;justify-content:space-between;align-items:center;padding:10px 13px;background:#faf5ef;border-bottom:1px solid var(--line)}
.outcome{font-family:'Archivo';font-weight:700;font-size:13px;letter-spacing:.04em;padding:3px 9px;border-radius:3px}
.outcome.published{background:var(--sealsoft);color:var(--seal)}
.outcome.held{background:var(--ochresoft);color:var(--ochre)}
.outcome.COMPLETED{background:var(--sealsoft);color:var(--seal)}
.outcome.ERROR{background:var(--bricksoft);color:var(--brick)}
.outcome.ESCALATED{background:var(--ochresoft);color:var(--ochre)}
.outcome.WEBHOOK_PENDING{background:#f3e8ff;color:#7c3aed}
.trace{padding:4px 0;background:#fffcf8;max-height:340px;overflow:auto}
.tr{padding:7px 13px;font-size:12.5px;line-height:1.5;border-bottom:1px solid #f5f0ea;display:block}
.tr:last-child{border-bottom:none}
.trk{display:inline-block;font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;width:62px;color:var(--faint);vertical-align:top}
.tr.think{color:var(--muted)}
.tr.act .trk{color:var(--accent)}
.tr.obs{color:var(--muted)}.tr.obs .trk{color:#7a5ea8}
.tr.conc .trk{color:var(--seal)}
.tr.esc{background:var(--ochresoft)}.tr.esc .trk{color:var(--ochre)}
.tr.gatetr{background:#faf5ef;font-weight:600}.tr.gatetr .trk{color:var(--ink)}
.grsn{font-weight:400;font-size:11.5px;color:var(--muted);margin-left:62px;margin-top:3px}
.concl{padding:13px;font-size:13px;border-top:1px solid var(--line)}
.cites{display:flex;flex-direction:column;gap:3px;margin-top:6px}
.cites a{font-family:'IBM Plex Mono';font-size:11px;color:var(--accent);text-decoration:none}
.cites a:hover{text-decoration:underline}
/* registration form */
.reg-form{display:flex;flex-direction:column;gap:14px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.form-group input,.form-group select,.form-group textarea{padding:8px 10px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:13px;background:var(--card)}
.form-group input:focus,.form-group select:focus,.form-group textarea:focus{outline:2px solid var(--accent);border-color:var(--accent)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
/* code block */
.code-block{background:#1a1008;color:#fbbf24;border-radius:4px;padding:14px 16px;font-family:'IBM Plex Mono';font-size:11.5px;line-height:1.6;overflow-x:auto;position:relative}
.code-block .copy-btn{position:absolute;top:8px;right:8px;background:rgba(255,255,255,.1);border:none;color:#c4956a;padding:4px 8px;border-radius:3px;cursor:pointer;font-size:10px;font-family:'IBM Plex Mono'}
.code-block .copy-btn:hover{background:rgba(255,255,255,.2)}
/* deploy */
.env-card{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:14px 16px;margin-bottom:10px}
.env-card .env-name{font-weight:600;font-size:13px;margin-bottom:4px}
.env-card .env-url{font-family:'IBM Plex Mono';font-size:11px;color:var(--accent)}
.env-card .env-status{display:flex;gap:8px;align-items:center;margin-top:8px;font-size:11px;color:var(--muted)}
/* tabs within panels */
.tab-row{display:flex;gap:0;border-bottom:1px solid var(--line);padding:0 20px}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;padding:10px 14px;font:inherit;font-size:12px;color:var(--muted);cursor:pointer;font-weight:500}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-btn:hover{color:var(--ink)}
@media(max-width:820px){.wrap{grid-template-columns:1fr}.agent-grid{grid-template-columns:1fr}.dash-stats{grid-template-columns:repeat(2,1fr)}.nav{gap:3px}.form-row{grid-template-columns:1fr}}
</style></head>
<body>
<div class="header">
  <div class="brand"><span class="logo">CORTEX</span><span class="sub">Agent Ops Hub</span></div>
  <div class="nav">
    <button class="navbtn active" id="nav-monitor" onclick="setView('monitor')">Monitor</button>
    <button class="navbtn" id="nav-agents" onclick="setView('agents')">Agents</button>
    <button class="navbtn" id="nav-control" onclick="setView('control')">Control</button>
    <button class="navbtn" id="nav-runs" onclick="setView('runs')">Runs</button>
    <button class="navbtn" id="nav-integrations" onclick="setView('integrations')">Integrations</button>
    <button class="navbtn" id="nav-deploy" onclick="setView('deploy')">Deploy</button>
    <button class="navbtn" id="nav-events" onclick="setView('events')">Event Log</button>
    <button class="navbtn" id="nav-history" onclick="setView('history')">History</button>
    <button class="navbtn" id="nav-automation" onclick="setView('automation')">Automation</button>
    <button class="navbtn" id="nav-settings" onclick="setView('settings')">Settings</button>
  </div>
  <div class="hmeta" style="display:flex;align-items:center;gap:12px">
    <span><span id="llm-state">rule-based</span> · <span id="count">0</span> agents</span>
    <span id="user-badge" style="display:inline-flex;align-items:center;gap:6px;background:rgba(249,115,22,.15);border:1px solid rgba(249,115,22,.3);border-radius:20px;padding:3px 12px 3px 8px;font-size:10.5px">
      <span style="width:22px;height:22px;border-radius:50%;background:linear-gradient(135deg,#f97316,#fbbf24);display:flex;align-items:center;justify-content:center;font-family:Archivo;font-weight:700;font-size:10px;color:#1a1008" id="user-avatar"></span>
      <span id="user-name" style="color:#fbbf24"></span>
    </span>
    <button onclick="doLogout()" style="background:none;border:1px solid #3d2a1a;color:#c4956a;padding:3px 10px;font-size:10px;cursor:pointer;border-radius:3px;font-family:'IBM Plex Mono';letter-spacing:.04em;transition:all .15s" onmouseover="this.style.borderColor='#c4632a';this.style.color='#f97316'" onmouseout="this.style.borderColor='#3d2a1a';this.style.color='#c4956a'">Sign Out</button>
  </div>
</div>

<div class="view" id="root"></div>

<script>
let AGENTS=[], sel=null, pending=null, view='monitor', META={}, USER=null, advancedMode=false;
const TABS=['monitor','agents','control','runs','integrations','deploy','events','history','automation','settings'];

async function boot(){
  // Load user session
  try{
    const ur=await fetch('/api/auth/me');
    if(ur.ok){const ud=await ur.json(); USER=ud.user;
      document.getElementById('user-name').textContent=USER.name;
      document.getElementById('user-avatar').textContent=USER.name.split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2);
    }
  }catch(e){}
  const r=await fetch('/api/agents'); const d=await r.json();
  AGENTS=d.agents; META=d;
  document.getElementById('count').textContent=d.total;
  document.getElementById('llm-state').textContent = d.llm ? 'model-assisted' : 'rule-based';
  if(!sel && AGENTS.length) sel=AGENTS[0].id;
  await render();
}

async function doLogout(){
  await fetch('/api/auth/logout',{method:'POST'});
  document.cookie='cortex_session=;path=/;expires=Thu, 01 Jan 1970 00:00:00 GMT';
  window.location.reload();
}

function setActiveNav(){
  TABS.forEach(v=>{
    const el=document.getElementById('nav-'+v);
    if(el) el.classList.toggle('active', v===view);
  });
}

async function render(){
  setActiveNav();
  const fn={monitor:renderMonitor,agents:renderAgents,control:renderControl,runs:renderRuns,
    integrations:renderIntegrations,deploy:renderDeploy,events:renderEventLog,
    history:renderHistory,automation:renderAutomation,settings:renderSettings}[view];
  if(fn) await fn();
}

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmt(x){ return x===null||x===undefined?'--':(typeof x==='boolean'?(x?'on':'off'):String(x)); }

/* ═══════════════ MONITOR DASHBOARD ═══════════════ */
async function renderMonitor(){
  const m=await (await fetch('/api/metrics/portfolio')).json();
  const ev=await (await fetch('/api/events?limit=10')).json();
  const s=await (await fetch('/api/settings')).json();

  const running=AGENTS.filter(a=>a.status==='running').length;
  const stopped=AGENTS.filter(a=>a.status==='stopped').length;
  const errors=AGENTS.filter(a=>a.status==='error').length;

  const agentCards=AGENTS.map(a=>`
    <div class="agent-card" data-s="${a.status}" onclick="jumpToControl('${a.id}')">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:6px">
        <div class="ac-name">${esc(a.name)}</div>
        <span class="pill ${a.status}">${a.status}</span>
      </div>
      <div class="ac-desc">${esc(a.description||'No description')}</div>
      <div class="ac-metrics">
        <span>Success <span class="metric-val">${a.containment}%</span></span>
        <span>Sources <span class="metric-val">${a.data_sources_count||0}</span></span>
        <span>Tools <span class="metric-val">${a.tools_count||0}</span></span>
      </div>
      <div style="margin-top:8px">
        <span class="typetag ${a.type}">${a.type}</span>
        ${a.live?'<span class="livetag">LIVE</span>':''}
        <span class="eptag">${(a.endpoint||{}).type||'embedded'}</span>
      </div>
    </div>`).join('');

  const feedItems=(ev.events||[]).slice(0,8).map(e=>{
    const t=e.event_type.split('.').pop();
    const dotClass={start:'start',complete:'complete',error:'error',escalate:'escalate',registered:'register'}[t]||'start';
    const time=new Date(e.timestamp).toLocaleTimeString();
    return `<div class="feed-item">
      <div class="feed-dot ${dotClass}"></div>
      <div><div style="font-weight:500">${esc(e.event_type)}</div>
        <div style="color:var(--faint);font-size:10px">${esc(e.agent_id)} · ${time}</div>
      </div></div>`;
  }).join('')||'<div style="padding:16px;color:var(--faint);font-size:12px;text-align:center">No recent events</div>';

  const providerStatus=['anthropic','openai','gemini','xai','perplexity','mistral','cohere','meta'].map(p=>{
    const prov=s.providers?.[p]||{};
    const dot=prov.configured?'green':'gray';
    return `<div class="health-row">
      <div class="health-dot ${dot}"></div>
      <span style="font-weight:500">${prov.label||p}</span>
      <span style="color:var(--faint);margin-left:auto;font-family:'IBM Plex Mono';font-size:10px">${prov.configured?'connected':'not configured'}</span>
    </div>`;
  }).join('');

  document.getElementById('root').innerHTML=`
    <div class="dash-hero">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <h2 style="margin:0;font-size:18px">Agent Operations</h2>
        <div style="text-align:right">
          <div style="font-family:'IBM Plex Mono';font-size:11px;color:var(--accent)">${new Date().toLocaleDateString('en-US',{weekday:'long',month:'short',day:'numeric'})}</div>
          ${USER?`<div style="font-size:11px;color:var(--muted);margin-top:2px">${USER.name}${USER.role?' · '+USER.role:''}${USER.org?' · '+USER.org:''}</div>`:''}
        </div>
      </div>
      <div style="font-size:12px;color:var(--muted)">Monitoring ${META.total} agents across your fleet</div>
    </div>

    <div class="dash-stats">
      <div class="stat-card"><div class="n">${META.total}</div><div class="l">Total Agents</div></div>
      <div class="stat-card"><div class="n green">${running}</div><div class="l">Running</div></div>
      <div class="stat-card"><div class="n ${errors?'warn':''}">${errors}</div><div class="l">Errors</div></div>
      <div class="stat-card"><div class="n">${m.health_score}%</div><div class="l">Health Score</div></div>
      <div class="stat-card"><div class="n">${m.avg_containment}%</div><div class="l">Avg Success</div></div>
      <div class="stat-card"><div class="n">${m.avg_escalation}%</div><div class="l">Avg Escalation</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 340px;gap:20px;align-items:start">
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <h2 style="margin:0">Active Agents</h2>
          <button class="btn accent" onclick="setView('agents')" style="font-size:11px;padding:5px 12px">+ Register Agent</button>
        </div>
        <div class="agent-grid">${agentCards}</div>
      </div>
      <div>
        <div class="feed-panel" style="margin-bottom:16px">
          <div class="feed-head"><span>Activity Feed</span><button class="btn ghost" onclick="setView('events')" style="font-size:10px;padding:3px 8px">View All</button></div>
          ${feedItems}
        </div>
        <div class="panel" style="padding:16px">
          <h4>System Health</h4>
          ${providerStatus}
          <div class="health-row" style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line)">
            <div class="health-dot green"></div>
            <span style="font-weight:500">CORTEX API</span>
            <span style="color:var(--faint);margin-left:auto;font-family:'IBM Plex Mono';font-size:10px">operational</span>
          </div>
        </div>
      </div>
    </div>`;
}

async function jumpToControl(id){ sel=id; view='control'; await render(); }

/* ═══════════════ AGENTS — Registration & Management ═══════════════ */
async function renderAgents(){
  const agentList=AGENTS.map(a=>`
    <div class="agent-card" data-s="${a.status}" onclick="sel='${a.id}';setView('control')">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:4px">
        <div class="ac-name">${esc(a.name)}</div>
        <div style="display:flex;gap:4px;align-items:center">
          <span class="pill ${a.status}">${a.status}</span>
          <button class="btn ghost" style="padding:2px 6px;font-size:10px;color:var(--brick)" onclick="event.stopPropagation();deleteAgent('${a.id}','${esc(a.name)}')">Delete</button>
        </div>
      </div>
      <div class="ac-desc">${esc(a.description||'')}</div>
      <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
        <span class="typetag ${a.type}">${a.type}</span>
        <span class="eptag">${(a.endpoint||{}).type||'embedded'}</span>
        <span style="font-family:'IBM Plex Mono';font-size:9px;color:var(--faint);padding:2px 6px">${a.data_sources_count||0} sources · ${a.tools_count||0} tools</span>
      </div>
    </div>`).join('');

  document.getElementById('root').innerHTML=`
    <div style="display:grid;grid-template-columns:1fr 420px;gap:24px;align-items:start">
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h2 style="margin:0">Registered Agents</h2>
          <span style="font-family:'IBM Plex Mono';font-size:11px;color:var(--faint)">${AGENTS.length} agents</span>
        </div>
        <div class="agent-grid" style="grid-template-columns:1fr">${agentList}</div>
      </div>
      <div>
        <!-- Mode toggle -->
        <div style="display:flex;gap:0;margin-bottom:16px;border:1px solid var(--line);border-radius:6px;overflow:hidden">
          <button id="mode-register" class="btn" onclick="toggleAgentMode('register')" style="flex:1;border-radius:0;border:none;background:linear-gradient(135deg,#c4632a,#ea580c);color:#fff;padding:10px;font-size:12px;font-weight:600">Register New</button>
          <button id="mode-import" class="btn" onclick="toggleAgentMode('import')" style="flex:1;border-radius:0;border:none;background:var(--card);color:var(--muted);padding:10px;font-size:12px;font-weight:600">Import Agent</button>
        </div>

        <!-- Import Panel -->
        <div class="panel" id="import-panel" style="display:none">
          <div class="phead"><h3>Import Agent</h3></div>
          <div class="sect">
            <div style="font-size:12px;color:var(--muted);margin-bottom:14px">Import an agent from another system. CORTEX auto-detects the config format and maps it to its schema.</div>

            <div class="form-group"><label>Source Format</label>
              <select id="imp-format" style="padding:8px 10px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:13px;width:100%">
                <option value="auto">Auto-detect</option>
                <option value="cortex">CORTEX (native)</option>
                <option value="langchain">LangChain</option>
                <option value="crewai">CrewAI</option>
                <option value="openai">OpenAI Assistants</option>
                <option value="raw">Raw JSON / YAML</option>
              </select>
            </div>

            <div style="display:flex;gap:0;margin-bottom:12px;border:1px solid var(--line);border-radius:4px;overflow:hidden">
              <button class="btn" id="imp-tab-paste" onclick="switchImpTab('paste')" style="flex:1;border-radius:0;border:none;background:var(--accentsoft);color:var(--accent);padding:8px;font-size:11px;font-weight:600">Paste Config</button>
              <button class="btn" id="imp-tab-file" onclick="switchImpTab('file')" style="flex:1;border-radius:0;border:none;background:var(--card);color:var(--muted);padding:8px;font-size:11px;font-weight:600">Upload File</button>
              <button class="btn" id="imp-tab-url" onclick="switchImpTab('url')" style="flex:1;border-radius:0;border:none;background:var(--card);color:var(--muted);padding:8px;font-size:11px;font-weight:600">From URL</button>
            </div>

            <div id="imp-paste" class="form-group">
              <textarea id="imp-config" rows="12" placeholder='Paste agent config here (JSON or YAML)...\n\nExamples:\n\n// LangChain\n{"llm": {"model_name": "gpt-5.6-terra"}, "tools": [...]}\n\n// CrewAI\n{"role": "Researcher", "goal": "...", "llm": {"model": "claude-sonnet-5"}}\n\n// OpenAI Assistant\n{"name": "My Assistant", "model": "gpt-5.6-terra", "instructions": "..."}\n\n// CORTEX native\n{"name": "...", "model": {...}, "execution": {...}}' style="padding:10px;border:1px solid var(--line);border-radius:4px;font-family:'IBM Plex Mono';font-size:11px;resize:vertical;width:100%;background:var(--paper)"></textarea>
            </div>

            <div id="imp-file" style="display:none" class="form-group">
              <div style="border:2px dashed var(--line);border-radius:6px;padding:24px;text-align:center;cursor:pointer;transition:border-color .15s" onclick="document.getElementById('imp-file-input').click()" onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--line)'">
                <div style="font-size:24px;margin-bottom:8px">📄</div>
                <div style="font-size:12px;font-weight:600;color:var(--ink)">Drop a config file or click to browse</div>
                <div style="font-size:11px;color:var(--faint);margin-top:4px">Supports .json, .yaml, .yml, .toml</div>
                <input type="file" id="imp-file-input" accept=".json,.yaml,.yml,.toml,.txt" style="display:none" onchange="handleImportFile(this)"/>
              </div>
              <div id="imp-file-name" style="margin-top:8px;font-family:'IBM Plex Mono';font-size:11px;color:var(--accent)"></div>
            </div>

            <div id="imp-url" style="display:none" class="form-group">
              <label>Registry / Config URL</label>
              <input id="imp-url-input" placeholder="https://api.example.com/agents/my-agent/config" style="padding:8px 10px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:13px;width:100%"/>
              <div style="font-size:10px;color:var(--faint);margin-top:4px">Fetches JSON config from a remote URL</div>
            </div>

            <button class="btn accent" onclick="importAgent()" style="width:100%;margin-top:8px">Import Agent</button>
            <div id="imp-msg" style="margin-top:8px;font-size:12px"></div>
          </div>
        </div>

        <!-- Register Panel -->
        <div class="panel" id="register-panel">
          <div class="phead"><h3>Register New Agent</h3></div>
          <div class="sect">
            <div class="reg-form">
              <div class="form-group"><label>Agent Name</label><input id="reg-name" placeholder="e.g. Customer Support Bot"></div>
              <div class="form-group"><label>Description</label><textarea id="reg-desc" rows="2" placeholder="What does this agent do?" style="padding:8px 10px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:13px;resize:vertical"></textarea></div>
              <div class="form-group"><label>Account / Team</label><input id="reg-acct" placeholder="e.g. Engineering"></div>
              <div class="form-row">
                <div class="form-group"><label>Endpoint Type</label>
                  <select id="reg-eptype"><option value="embedded">Embedded (Python)</option><option value="rest">REST API</option><option value="webhook">Webhook</option></select>
                </div>
                <div class="form-group"><label>Endpoint URL</label><input id="reg-epurl" placeholder="https://..."></div>
              </div>
              <div style="border-top:1px solid var(--line);padding-top:14px;margin-top:4px">
                <h4>Model Config</h4>
                <div class="form-row">
                  <div class="form-group"><label>Provider</label>
                    <select id="reg-provider"><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option><option value="gemini">Google Gemini</option><option value="xai">xAI (Grok)</option><option value="perplexity">Perplexity</option><option value="mistral">Mistral AI</option><option value="cohere">Cohere</option><option value="meta">Meta (Together)</option></select>
                  </div>
                  <div class="form-group"><label>Model</label>
                    <select id="reg-model">
                      <optgroup label="Anthropic">
                        <option value="claude-fable-5">claude-fable-5</option>
                        <option value="claude-opus-5">claude-opus-5</option>
                        <option value="claude-sonnet-5" selected>claude-sonnet-5</option>
                        <option value="claude-haiku-4-5">claude-haiku-4-5</option>
                        <option value="claude-opus-4-8">claude-opus-4-8</option>
                        <option value="claude-opus-4-7">claude-opus-4-7</option>
                        <option value="claude-opus-4-6">claude-opus-4-6</option>
                        <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                      </optgroup>
                      <optgroup label="OpenAI">
                        <option value="gpt-5.6-sol">gpt-5.6-sol</option>
                        <option value="gpt-5.6-terra">gpt-5.6-terra</option>
                        <option value="gpt-5.6-luna">gpt-5.6-luna</option>
                      </optgroup>
                      <optgroup label="Google Gemini">
                        <option value="gemini-3.1-pro">gemini-3.1-pro</option>
                        <option value="gemini-3.7-flash">gemini-3.7-flash</option>
                      </optgroup>
                      <optgroup label="xAI (Grok)">
                        <option value="grok-3">grok-3</option>
                        <option value="grok-3-mini">grok-3-mini</option>
                        <option value="grok-2">grok-2</option>
                      </optgroup>
                      <optgroup label="Perplexity">
                        <option value="sonar-pro">sonar-pro</option>
                        <option value="sonar">sonar</option>
                        <option value="sonar-deep-research">sonar-deep-research</option>
                        <option value="sonar-reasoning-pro">sonar-reasoning-pro</option>
                      </optgroup>
                      <optgroup label="Mistral AI">
                        <option value="mistral-large-latest">mistral-large</option>
                        <option value="mistral-medium-latest">mistral-medium</option>
                        <option value="mistral-small-latest">mistral-small</option>
                        <option value="codestral-latest">codestral</option>
                      </optgroup>
                      <optgroup label="Cohere">
                        <option value="command-r-plus">command-r-plus</option>
                        <option value="command-r">command-r</option>
                        <option value="command-a-03-2025">command-a</option>
                      </optgroup>
                      <optgroup label="Meta (via Together)">
                        <option value="meta-llama/Llama-4-Maverick-17B-128E-Instruct-Turbo">Llama 4 Maverick</option>
                        <option value="meta-llama/Llama-4-Scout-17B-16E-Instruct">Llama 4 Scout</option>
                        <option value="meta-llama/Meta-Llama-3.3-70B-Instruct-Turbo">Llama 3.3 70B</option>
                      </optgroup>
                    </select>
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-group"><label>Temperature</label><input id="reg-temp" type="number" step="0.1" min="0" max="2" value="0.7"></div>
                  <div class="form-group"><label>Max Tokens</label><input id="reg-maxtok" type="number" value="4096"></div>
                </div>
              </div>
              <div style="border-top:1px solid var(--line);padding-top:14px;margin-top:4px">
                <h4>Behavior</h4>
                <div class="form-row">
                  <div class="form-group"><label>Confidence Threshold</label><input id="reg-conf" type="number" step="0.05" min="0" max="1" value="0.75"></div>
                  <div class="form-group"><label>Escalation Level</label>
                    <select id="reg-esc"><option value="low">Low</option><option value="moderate">Moderate</option><option value="high" selected>High</option></select>
                  </div>
                </div>
              </div>
              <div style="display:flex;gap:8px;margin-top:6px">
                <button class="btn accent" onclick="registerAgent()">Register Agent</button>
              </div>
              <div id="reg-msg" style="margin-top:8px;font-size:12px"></div>
            </div>
          </div>
        </div>

        <div class="panel" style="margin-top:16px">
          <div class="phead"><h3>Sample Templates</h3></div>
          <div class="sect" style="display:flex;flex-direction:column;gap:8px">
            <button class="btn ghost" style="text-align:left;padding:10px 12px" onclick="fillSample('research')">
              <div style="font-weight:600;font-size:12px">Research Agent</div>
              <div style="font-size:11px;color:var(--muted);margin-top:2px">Search, fetch, and summarize information</div>
            </button>
            <button class="btn ghost" style="text-align:left;padding:10px 12px" onclick="fillSample('router')">
              <div style="font-weight:600;font-size:12px">Router Agent</div>
              <div style="font-size:11px;color:var(--muted);margin-top:2px">Classify intent and route to handlers</div>
            </button>
            <button class="btn ghost" style="text-align:left;padding:10px 12px" onclick="fillSample('action')">
              <div style="font-weight:600;font-size:12px">Action Agent</div>
              <div style="font-size:11px;color:var(--muted);margin-top:2px">Execute actions in external systems</div>
            </button>
          </div>
        </div>
      </div>
    </div>`;
}

function fillSample(type){
  const samples={
    research:{name:'Research Agent',desc:'General-purpose research agent that searches and summarizes information',provider:'anthropic',model:'claude-sonnet-5',temp:'0.7',tokens:'4096',conf:'0.75',esc:'high',eptype:'embedded'},
    router:{name:'Router Agent',desc:'Routes incoming requests to the appropriate handler based on intent',provider:'anthropic',model:'claude-sonnet-5',temp:'0.3',tokens:'1024',conf:'0.8',esc:'moderate',eptype:'embedded'},
    action:{name:'Action Agent',desc:'Executes actions in external systems based on instructions',provider:'openai',model:'gpt-5.6-terra',temp:'0.5',tokens:'2048',conf:'0.85',esc:'low',eptype:'rest'}
  };
  const s=samples[type]; if(!s) return;
  document.getElementById('reg-name').value=s.name;
  document.getElementById('reg-desc').value=s.desc;
  document.getElementById('reg-provider').value=s.provider;
  document.getElementById('reg-model').value=s.model;
  document.getElementById('reg-temp').value=s.temp;
  document.getElementById('reg-maxtok').value=s.tokens;
  document.getElementById('reg-conf').value=s.conf;
  document.getElementById('reg-esc').value=s.esc;
  document.getElementById('reg-eptype').value=s.eptype;
}

async function registerAgent(){
  const name=document.getElementById('reg-name').value.trim();
  if(!name){document.getElementById('reg-msg').innerHTML='<span style="color:var(--brick)">Name is required</span>';return;}
  const body={
    name,
    description:document.getElementById('reg-desc').value.trim(),
    account:document.getElementById('reg-acct').value.trim(),
    endpoint_type:document.getElementById('reg-eptype').value,
    endpoint_url:document.getElementById('reg-epurl').value.trim(),
    config:{
      model:{provider:document.getElementById('reg-provider').value,model_name:document.getElementById('reg-model').value,temperature:parseFloat(document.getElementById('reg-temp').value),max_tokens:parseInt(document.getElementById('reg-maxtok').value)},
      behavior:{confidence_threshold:parseFloat(document.getElementById('reg-conf').value),escalation_threshold:document.getElementById('reg-esc').value}
    }
  };
  const d=await(await fetch('/api/agents/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){
    document.getElementById('reg-msg').innerHTML=`<span style="color:var(--seal)">Registered: ${esc(d.agent_id)}</span>`;
    await boot();
    setTimeout(()=>renderAgents(),600);
  }else{
    document.getElementById('reg-msg').innerHTML=`<span style="color:var(--brick)">${d.detail||'Registration failed'}</span>`;
  }
}

async function deleteAgent(id,name){
  if(!confirm('Delete agent "'+name+'"? This cannot be undone.')) return;
  await fetch('/api/agents/'+id,{method:'DELETE'});
  if(sel===id && AGENTS.length>1) sel=AGENTS.find(a=>a.id!==id)?.id||null;
  await boot();
  renderAgents();
}

/* ═══════════════ IMPORT AGENT ═══════════════ */
let agentPanelMode='register';
function toggleAgentMode(mode){
  agentPanelMode=mode;
  document.getElementById('register-panel').style.display=mode==='register'?'':'none';
  document.getElementById('import-panel').style.display=mode==='import'?'':'none';
  const regBtn=document.getElementById('mode-register');
  const impBtn=document.getElementById('mode-import');
  if(mode==='register'){
    regBtn.style.background='linear-gradient(135deg,#c4632a,#ea580c)';regBtn.style.color='#fff';
    impBtn.style.background='var(--card)';impBtn.style.color='var(--muted)';
  }else{
    impBtn.style.background='linear-gradient(135deg,#c4632a,#ea580c)';impBtn.style.color='#fff';
    regBtn.style.background='var(--card)';regBtn.style.color='var(--muted)';
  }
}

let impTab='paste';
function switchImpTab(tab){
  impTab=tab;
  ['paste','file','url'].forEach(t=>{
    const el=document.getElementById('imp-'+t);
    const btn=document.getElementById('imp-tab-'+t);
    if(el) el.style.display=t===tab?'':'none';
    if(btn){btn.style.background=t===tab?'var(--accentsoft)':'var(--card)';btn.style.color=t===tab?'var(--accent)':'var(--muted)';}
  });
}

let importFileContent='';
function handleImportFile(input){
  const file=input.files[0]; if(!file) return;
  document.getElementById('imp-file-name').textContent='Loaded: '+file.name;
  const reader=new FileReader();
  reader.onload=e=>{importFileContent=e.target.result;};
  reader.readAsText(file);
}

async function importAgent(){
  const msg=document.getElementById('imp-msg');
  msg.textContent='';
  let configStr='';

  if(impTab==='paste'){
    configStr=document.getElementById('imp-config').value.trim();
  }else if(impTab==='file'){
    configStr=importFileContent;
  }else if(impTab==='url'){
    const url=document.getElementById('imp-url-input').value.trim();
    if(!url){msg.innerHTML='<span style="color:var(--brick)">Enter a URL</span>';return;}
    try{
      msg.innerHTML='<span style="color:var(--muted)">Fetching config...</span>';
      const r=await fetch(url);
      if(!r.ok) throw new Error('HTTP '+r.status);
      configStr=await r.text();
    }catch(e){msg.innerHTML='<span style="color:var(--brick)">Failed to fetch: '+e.message+'</span>';return;}
  }

  if(!configStr){msg.innerHTML='<span style="color:var(--brick)">No config provided</span>';return;}

  const fmt=document.getElementById('imp-format').value;
  msg.innerHTML='<span style="color:var(--muted)">Importing...</span>';

  try{
    const r=await fetch('/api/agents/import',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({config_json:configStr,source_format:fmt})});
    const d=await r.json();
    if(d.ok){
      msg.innerHTML='<span style="color:var(--seal)">Imported: <strong>'+esc(d.agent_id)+'</strong> (detected: '+esc(d.detected_format)+')</span>';
      await boot();
      setTimeout(()=>renderAgents(),600);
    }else{
      msg.innerHTML='<span style="color:var(--brick)">'+(d.detail||'Import failed')+'</span>';
    }
  }catch(e){msg.innerHTML='<span style="color:var(--brick)">Error: '+e.message+'</span>';}
}

/* ═══════════════ CONTROL — Config + Run ═══════════════ */
function cfgRows(c){
  const model=c.model||{};
  const exec=c.execution||{};
  const beh=c.behavior||{};
  const ds=(c.data_sources||[]);
  const tools=(c.tools||[]);
  const audit=c.audit||{};
  return `<div class="cfg">
    <div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--line)">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px">Model</div>
      <div><span class="k">provider:</span> <span class="v">${model.provider||'--'}</span> &nbsp; <span class="k">model:</span> <span class="v">${model.model_name||'--'}</span></div>
      <div><span class="k">temperature:</span> <span class="v">${model.temperature??'--'}</span> &nbsp; <span class="k">max tokens:</span> <span class="v">${model.max_tokens||'--'}</span></div>
    </div>
    <div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--line)">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px">Execution</div>
      <div><span class="k">timeout:</span> <span class="v">${exec.timeout_seconds||'--'}s</span> &nbsp; <span class="k">retries:</span> <span class="v">${exec.max_retries??'--'}</span> &nbsp; <span class="k">delay:</span> <span class="v">${exec.retry_delay_seconds||'--'}s</span></div>
    </div>
    <div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--line)">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px">Behavior</div>
      <div><span class="k">confidence:</span> <span class="v">${beh.confidence_threshold??'--'}</span> &nbsp; <span class="k">escalation:</span> <span class="v">${beh.escalation_threshold||'--'}</span></div>
      <div><span class="k">auto-escalate:</span> <span class="v">${beh.auto_escalate_on_error?'on':'off'}</span> &nbsp; <span class="k">confirm first:</span> <span class="v">${beh.confirm_before_action?'on':'off'}</span></div>
    </div>
    <div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--line)">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px">Data Sources (${ds.length})</div>
      ${ds.length?ds.map(d=>`<div><span class="v">${esc(d.name)}</span> <span class="k">${d.type} · ${d.auth_type}</span></div>`).join(''):'<div style="color:var(--faint)">None configured</div>'}
    </div>
    <div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--line)">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px">Tools (${tools.length})</div>
      ${tools.length?tools.map(t=>`<div><span class="v">${esc(t.name)}</span> <span class="k">${esc(t.description||'')} · limit ${t.rate_limit||'--'}/min</span></div>`).join(''):'<div style="color:var(--faint)">None configured</div>'}
    </div>
    <div>
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px">Audit</div>
      <div><span class="k">log calls:</span> <span class="v">${audit.log_all_calls?'on':'off'}</span> &nbsp; <span class="k">log data:</span> <span class="v">${audit.log_data_access?'on':'off'}</span> &nbsp; <span class="k">track mods:</span> <span class="v">${audit.track_modifications?'on':'off'}</span></div>
    </div>
  </div>`;
}

function sidebar(){
  return `<div><h2>Agents</h2><div class="grid">${AGENTS.map(a=>`
    <button class="card ${sel===a.id?'active':''}" data-s="${a.status}" onclick="pick('${a.id}')">
      <div class="ctop"><span class="cname">${esc(a.name)}</span><span class="cstat">${a.status}</span></div>
      <div class="cmeta"><span>${a.data_sources_count||0} src</span><span>${a.tools_count||0} tools</span></div>
      <span class="typetag ${a.type}">${a.type}</span>${a.live?'<span class="livetag">LIVE</span>':''}
    </button>`).join('')}</div></div>`;
}

async function pick(id){ sel=id; pending=null; await render(); }

function toggleAdvanced(){ advancedMode=!advancedMode; renderControl(); }

async function renderControl(){
  if(!sel){document.getElementById('root').innerHTML='<div class="hint" style="padding:40px">Select or register an agent first.</div>';return;}
  const a=await (await fetch('/api/agents/'+sel)).json();
  const cfg=a.config||{};
  const beh=cfg.behavior||{};
  const exec=cfg.execution||{};
  const model=cfg.model||{};

  const modeToggle=`<div style="display:flex;align-items:center;gap:8px">
    <span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em">${advancedMode?'Advanced':'Simple'}</span>
    <button onclick="toggleAdvanced()" style="position:relative;width:36px;height:18px;border-radius:9px;border:none;background:${advancedMode?'var(--accent)':'#d4c8b8'};cursor:pointer;transition:background .2s">
      <span style="position:absolute;top:2px;${advancedMode?'right:2px':'left:2px'};width:14px;height:14px;border-radius:50%;background:white;transition:all .2s"></span>
    </button>
  </div>`;

  // Simple view: just the essentials
  const simpleConfig=`<div class="cfg">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
      <div style="padding:12px;background:#faf8f5;border-radius:6px;border:1px solid var(--line)">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:6px">Model</div>
        <div style="font-size:14px;font-weight:500">${model.model_name||'--'}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">${model.provider||'--'} · temp ${model.temperature??'--'}</div>
      </div>
      <div style="padding:12px;background:#faf8f5;border-radius:6px;border:1px solid var(--line)">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:6px">Status</div>
        <div style="font-size:14px;font-weight:500">${a.status} ${a.live?'· <span style="color:var(--seal)">LIVE</span>':''}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">v${a.version} · ${(a.endpoint||{}).type||'embedded'}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
      <div style="padding:8px 10px;background:#faf8f5;border-radius:4px;border:1px solid var(--line);text-align:center">
        <div style="font-size:16px;font-weight:600">${(cfg.data_sources||[]).length}</div>
        <div style="font-size:9px;color:var(--muted);text-transform:uppercase">Data Sources</div>
      </div>
      <div style="padding:8px 10px;background:#faf8f5;border-radius:4px;border:1px solid var(--line);text-align:center">
        <div style="font-size:16px;font-weight:600">${(cfg.tools||[]).length}</div>
        <div style="font-size:9px;color:var(--muted);text-transform:uppercase">Tools</div>
      </div>
      <div style="padding:8px 10px;background:#faf8f5;border-radius:4px;border:1px solid var(--line);text-align:center">
        <div style="font-size:16px;font-weight:600">${beh.confidence_threshold??'--'}</div>
        <div style="font-size:9px;color:var(--muted);text-transform:uppercase">Confidence</div>
      </div>
      <div style="padding:8px 10px;background:#faf8f5;border-radius:4px;border:1px solid var(--line);text-align:center">
        <div style="font-size:16px;font-weight:600">${exec.max_retries??'--'}</div>
        <div style="font-size:9px;color:var(--muted);text-transform:uppercase">Retries</div>
      </div>
    </div>
  </div>`;

  // Advanced view: full cfgRows + data source management
  const advancedConfig=`<div class="sect"><h4>Full Configuration</h4>${cfgRows(cfg)}</div>
    <div class="sect">
      <h4>Data Sources</h4>
      <div id="ds-list" style="margin-bottom:10px">
        ${(cfg.data_sources||[]).map(d=>`<div style="padding:8px 10px;margin-bottom:4px;background:#faf8f5;border-radius:4px;border:1px solid var(--line)">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div>
              <span style="font-weight:500">${esc(d.name)}</span>
              <span style="font-size:10px;padding:1px 6px;border-radius:3px;background:#e0d4c0;color:var(--ink);margin-left:4px">${d.type}</span>
              <span style="font-size:10px;padding:1px 6px;border-radius:3px;background:${d.auth_type==='none'?'#e8e0d4':'#d4dce0'};color:var(--ink);margin-left:4px">${d.auth_type}</span>
              ${d.refresh&&d.refresh!=='manual'?`<span style="font-size:10px;padding:1px 6px;border-radius:3px;background:#d4e0d6;color:#2a5a30;margin-left:4px">⟳ ${d.refresh}</span>`:''}
            </div>
            <button class="btn ghost" style="padding:2px 8px;font-size:10px" onclick="removeDataSource('${esc(d.name)}')">Remove</button>
          </div>
          ${d.endpoint?`<div style="font-size:10px;color:var(--faint);margin-top:4px;font-family:'IBM Plex Mono'">${esc(d.endpoint).substring(0,80)}</div>`:''}
        </div>`).join('')||'<div class="hint" style="margin:0">No data sources configured.</div>'}
      </div>
      <div style="padding:12px;background:#faf8f5;border-radius:6px;border:1px dashed var(--line);margin-top:8px">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);margin-bottom:8px">Add Data Source</div>
        <div class="form-row" style="grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px">
          <div class="form-group" style="margin:0"><label>Name</label><input id="ds-name" placeholder="e.g. customer_db" style="font-size:11px;padding:6px 8px"></div>
          <div class="form-group" style="margin:0"><label>Type</label><select id="ds-type" style="font-size:11px;padding:6px 8px"><option value="api">API</option><option value="database">Database</option><option value="file">File / S3</option><option value="webhook">Webhook</option><option value="graphql">GraphQL</option><option value="grpc">gRPC</option><option value="custom">Custom</option></select></div>
          <div class="form-group" style="margin:0"><label>Auth</label><select id="ds-auth" style="font-size:11px;padding:6px 8px"><option value="api_key">API Key</option><option value="oauth2">OAuth 2.0</option><option value="bearer">Bearer Token</option><option value="basic">Basic Auth</option><option value="connection_string">Connection String</option><option value="iam">IAM Role</option><option value="none">None</option></select></div>
        </div>
        <div class="form-row" style="grid-template-columns:2fr 1fr auto;gap:6px;align-items:end">
          <div class="form-group" style="margin:0"><label>Endpoint / Connection</label><input id="ds-endpoint" placeholder="https://api.example.com/v1 or postgres://..." style="font-size:11px;padding:6px 8px"></div>
          <div class="form-group" style="margin:0"><label>Refresh</label><select id="ds-refresh" style="font-size:11px;padding:6px 8px"><option value="realtime">Realtime</option><option value="5m">Every 5 min</option><option value="1h">Hourly</option><option value="1d">Daily</option><option value="manual">Manual</option></select></div>
          <button class="btn accent" style="padding:6px 12px;font-size:11px;margin-bottom:0" onclick="addDataSource()">Add</button>
        </div>
      </div>
    </div>`;

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead">
        <div>
          <h3>${esc(a.name)}</h3>
          <div class="acct">${esc(a.account||'Custom')}</div>
          <div style="margin-top:4px;font-size:11.5px;color:var(--muted)">${esc(a.description||'')}</div>
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
          ${modeToggle}
          <div class="ctrls">
            <button class="btn ghost" onclick="ctl('start')">Start</button>
            <button class="btn ghost" onclick="ctl('restart')">Restart</button>
            <button class="btn ghost" onclick="ctl('stop')">Stop</button>
            ${a.live?'<button class="btn ghost" style="color:var(--brick)" onclick="ctl(\'pause\')">Pause</button>':'<button class="btn accent" onclick="ctl(\'golive\')">Go Live</button>'}
          </div>
        </div>
      </div>
      ${advancedMode ? advancedConfig : `<div class="sect"><h4>Overview</h4>${simpleConfig}</div>`}

      ${a.live ? liveRunPanel(a) : ''}
      <div class="sect">
        <h4>Change Config — Plain English</h4>
        <textarea class="ask" id="ask" placeholder="e.g. Set temperature to 0.5, increase timeout to 10 minutes, escalate at moderate severity"></textarea>
        <div class="ctrls" style="margin-top:10px"><button class="btn accent" id="propose" onclick="propose()">Propose Change</button></div>
        <div class="hint">Cortex proposes a diff and waits for your approval. Try: <code>retry 5 times</code>, <code>timeout 60 seconds</code>, <code>confidence 0.9</code>, <code>switch to openai</code>, <code>turn off confirm</code>.</div>
        <div id="result"></div>
      </div>
    </div></div>`;
}

function liveRunPanel(a){
  const cfg=a.config||{};
  const beh=cfg.behavior||{};
  const exec=cfg.execution||{};
  return `<div class="sect livebox">
    <h4>Run Agent</h4>
    <div class="hint" style="margin:0 0 10px">This agent runs live using ${(cfg.model||{}).provider||'the configured'} provider. Governed by: <b>max ${exec.max_retries||3} retries</b>, <b>confidence threshold ${beh.confidence_threshold||0.75}</b>, <b>confirm ${beh.confirm_before_action?'ON':'OFF'}</b>.</div>
    <textarea class="ask" id="claim" placeholder="Enter input for this agent..."></textarea>
    <div class="ctrls" style="margin-top:10px">
      <button class="btn accent" id="runbtn" onclick="runAgent()">Run Agent</button>
    </div>
    <div id="runout"></div>
  </div>`;
}

async function addDataSource(){
  const name=document.getElementById('ds-name').value.trim();
  if(!name) return;
  const body={
    name,
    type:document.getElementById('ds-type').value,
    auth_type:document.getElementById('ds-auth').value,
    endpoint:document.getElementById('ds-endpoint').value.trim(),
    refresh:document.getElementById('ds-refresh').value
  };
  await fetch('/api/agents/'+sel+'/data-sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  await boot(); renderControl();
}
async function removeDataSource(name){
  await fetch('/api/agents/'+sel+'/data-sources/'+encodeURIComponent(name),{method:'DELETE'});
  await boot(); renderControl();
}

async function runAgent(){
  const claim=document.getElementById('claim').value.trim(); if(!claim) return;
  const btn=document.getElementById('runbtn'); btn.disabled=true;
  btn.innerHTML='<span class="spin">&#x25D4;</span> Running...';
  const out=document.getElementById('runout');
  out.innerHTML='<div class="hint" style="margin-top:12px">Agent is executing. This may take 10-40s.</div>';
  try{
    const d=await (await fetch('/api/agents/'+sel+'/run',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({claim})})).json();
    btn.disabled=false; btn.textContent='Run Agent';
    if(!d.ok){ out.innerHTML=`<div class="flag" style="margin-top:12px">${esc(d.error||'run failed')}</div>`; return; }
    out.innerHTML=renderRunTrace(d.run);
    await boot();
  }catch(e){
    btn.disabled=false; btn.textContent='Run Agent';
    out.innerHTML=`<div class="flag" style="margin-top:12px">${esc(String(e))}</div>`;
  }
}

function renderRunTrace(r){
  const steps=(r.trace||[]).map(s=>{
    if(s.kind==='think') return `<div class="tr think"><span class="trk">think</span>${esc(s.text)}</div>`;
    if(s.kind==='act') return `<div class="tr act"><span class="trk">act</span><b>${esc(s.tool||'')}</b> <span class="mono">${esc(JSON.stringify(s.args||{})).slice(0,160)}</span></div>`;
    if(s.kind==='observe') return `<div class="tr obs"><span class="trk">observe</span>${esc(s.result||'')}</div>`;
    if(s.kind==='conclude') return `<div class="tr conc"><span class="trk">conclude</span><b>${esc(s.verdict||'')}</b> · confidence ${s.confidence||'--'} · ${(s.citations||[]).length} citations</div>`;
    if(s.kind==='escalate') return `<div class="tr esc"><span class="trk">escalate</span>${esc(s.reason||'')}</div>`;
    if(s.kind==='gate') return `<div class="tr gatetr"><span class="trk">GATE</span><b>${esc(s.decision||'')}</b>${(s.reasons||[]).map(x=>`<div class="grsn">${esc(x)}</div>`).join('')}</div>`;
    if(s.kind==='info') return `<div class="tr"><span class="trk">info</span>${esc(s.text||'')}</div>`;
    if(s.kind==='rest_call') return `<div class="tr act"><span class="trk">rest</span>${esc(s.url||'')} · ${s.status||''}</div>`;
    if(s.kind==='error') return `<div class="tr esc"><span class="trk">error</span>${esc(s.text||'')}</div>`;
    return '';
  }).join('');
  const d=r.detail||{};
  const badge=r.published?'published':'held';
  let concl='';
  if(d.summary) concl+=`<div style="margin-bottom:8px">${esc(d.summary)}</div>`;
  if(d.reason) concl+=`<div style="margin-bottom:8px;color:var(--brick)">${esc(d.reason)}</div>`;
  if((d.citations||[]).length) concl+=`<div class="cites">${d.citations.map(c=>`<a href="${esc(c)}" target="_blank" rel="noopener">${esc(c).slice(0,80)}</a>`).join('')}</div>`;
  if(!r.published && d.route_to) concl+=`<div class="hint" style="margin-top:8px">Routed to <b>${esc(d.route_to)}</b></div>`;
  return `<div class="runbox" style="margin-top:14px">
    <div class="runhead"><span class="outcome ${r.outcome}">${r.outcome}</span>
      <span class="mono" style="font-size:11px;color:var(--faint)">${r.steps_used} steps · v${r.config_version} · ${r.provider||''} ${r.model||''}</span></div>
    <div class="trace">${steps||'<div style="padding:12px;color:var(--faint)">No trace data</div>'}</div>
    ${concl?`<div class="concl">${concl}</div>`:''}
  </div>`;
}

async function propose(){
  const req=document.getElementById('ask').value.trim(); if(!req) return;
  const btn=document.getElementById('propose'); btn.disabled=true; btn.innerHTML='<span class="spin">&#x25D4;</span> Proposing...';
  const d=await (await fetch('/api/agents/'+sel+'/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:req})})).json();
  btn.disabled=false; btn.textContent='Propose Change';
  const box=document.getElementById('result');
  if(!d.ok){ box.innerHTML=`<div class="flag" style="background:var(--ochresoft);border-color:#e3d3ad;color:var(--ochre)">${esc(d.message||'No changes detected')}</div>`; return; }
  pending=d.token;
  const rows=d.changes.map(c=>`<div class="drow"><span class="dfield">${esc(c.field)}</span>
    <span class="dfrom mono">${fmt(c.from)}</span><span class="darrow">&rarr;</span><span class="dto mono">${fmt(c.to)}</span></div>`).join('');
  const flags=(d.flags||[]).map(f=>`<div class="flag">${esc(f)}</div>`).join('');
  box.innerHTML=`<div class="diff"><div class="diffhead"><span>Proposed diff</span><span>${d.changes.length} change${d.changes.length>1?'s':''}</span></div>${rows}</div>${flags}
    <div class="gate"><button class="btn accent" onclick="apply()">Approve &amp; Apply</button>
      <button class="btn ghost" onclick="document.getElementById('result').innerHTML='';pending=null">Discard</button>
      <span class="gatemsg">You are the gate. Applying bumps the version.</span></div>`;
}
async function apply(){
  if(!pending) return;
  const d=await (await fetch('/api/agents/'+sel+'/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:pending,approved_by:'you'})})).json();
  if(d.ok){ pending=null; await boot(); await renderControl();
    document.getElementById('result').innerHTML=`<div class="applied">Applied. Now v${d.new_version}. Logged and reversible.</div>`; }
}
async function ctl(action){ await fetch('/api/agents/'+sel+'/control?action='+action,{method:'POST'}); await boot(); renderControl(); }

/* ═══════════════ RUNS — Execution History + Audit ═══════════════ */
let runsFilter='ALL';
function fmtTime(iso){if(!iso)return'—';try{const d=new Date(iso);return d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return'—'}}
function calcDuration(start,end){if(!start||!end)return'—';try{const ms=new Date(end)-new Date(start);if(ms<1000)return ms+'ms';if(ms<60000)return(ms/1000).toFixed(1)+'s';return Math.floor(ms/60000)+'m '+(Math.round((ms%60000)/1000))+'s'}catch(e){return'—'}}
function toggleTrace(id){const el=document.getElementById(id);if(!el)return;const btn=el.previousElementSibling?.querySelector('.trace-toggle');if(el.style.display==='none'){el.style.display='block';if(btn)btn.textContent='▾ Hide trace'}else{el.style.display='none';if(btn)btn.textContent='▸ Show trace'}}
function setRunsFilter(f,el){runsFilter=f;document.querySelectorAll('.rf-btn').forEach(b=>b.classList.remove('active'));el.classList.add('active');renderRuns();}

async function renderRuns(){
  if(!sel){document.getElementById('root').innerHTML='<div class="hint" style="padding:40px">Select an agent first.</div>';return;}
  const a=AGENTS.find(x=>x.id===sel);
  const r=await (await fetch('/api/agents/'+sel+'/runs')).json();
  let runs=r.runs||[];

  /* outcome counts for filter badges */
  const counts={ALL:runs.length,COMPLETED:0,ESCALATED:0,ERROR:0,WEBHOOK_PENDING:0};
  runs.forEach(r=>{if(counts[r.outcome]!==undefined)counts[r.outcome]++;});

  /* apply filter */
  const filtered=runsFilter==='ALL'?runs:runs.filter(r=>r.outcome===runsFilter);

  const filterBar=`<div style="display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap">
    ${['ALL','COMPLETED','ESCALATED','ERROR','WEBHOOK_PENDING'].map(f=>
      `<button class="rf-btn${runsFilter===f?' active':''}" onclick="setRunsFilter('${f}',this)"
        style="padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:${runsFilter===f?'var(--ink)':'var(--bg)'};color:${runsFilter===f?'#fff':'var(--faint)'};cursor:pointer;font-size:11px;font-family:'IBM Plex Mono'">${f==='ALL'?'All':f} <span style="opacity:.7">${counts[f]||0}</span></button>`
    ).join('')}
  </div>`;

  const rows=filtered.length?filtered.map((run,i)=>{
    const traceId='trace-'+i+'-'+Date.now();
    const traceHTML=(run.trace||[]).map(t=>{
      if(t.kind==='think') return `<div class="tr think"><span class="trk">think</span> ${esc(t.text||'')}</div>`;
      if(t.kind==='act') return `<div class="tr act"><span class="trk">act</span> <b>${esc(t.tool||'')}</b> ${esc(JSON.stringify(t.args||{})).substring(0,120)}</div>`;
      if(t.kind==='observe') return `<div class="tr obs"><span class="trk">observe</span> ${esc((t.result||'').substring(0,200))}</div>`;
      if(t.kind==='escalate') return `<div class="tr esc"><span class="trk">escalate</span> ${esc(t.reason||'')}</div>`;
      if(t.kind==='conclude') return `<div class="tr conc"><span class="trk">result</span> <b>${esc(t.verdict||'')}</b></div>`;
      if(t.kind==='rest_call') return `<div class="tr act"><span class="trk">rest</span> ${esc(t.url||'')} ${t.status||''}</div>`;
      if(t.kind==='info') return `<div class="tr"><span class="trk">info</span> ${esc(t.text||'')}</div>`;
      if(t.kind==='error') return `<div class="tr esc"><span class="trk">error</span> ${esc(t.text||'')}</div>`;
      if(t.kind==='gate') return `<div class="tr gatetr"><span class="trk">GATE</span><b>${esc(t.decision||'')}</b></div>`;
      return '';
    }).join('');

    const outcomeColors={COMPLETED:'#1a7',ESCALATED:'#d90',ERROR:'#c33',WEBHOOK_PENDING:'#888'};
    const dur=calcDuration(run.started_at,run.finished_at);

    return `<div class="runbox" style="margin-bottom:12px">
      <div class="runhead">
        <div style="display:flex;gap:10px;align-items:center">
          <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.5px;color:#fff;background:${outcomeColors[run.outcome]||'#888'}">${run.outcome}</span>
          <span style="font-family:'IBM Plex Mono';font-size:10px;color:var(--faint)">Run #${runs.length - runs.indexOf(run)}</span>
        </div>
        <div style="font-size:10px;color:var(--faint);font-family:'IBM Plex Mono';text-align:right">
          <div>${run.steps_used||0} steps · v${run.config_version} · ${dur!=='—'?'⏱ '+dur:''}</div>
          <div>${run.provider||''} ${run.model||''}</div>
        </div>
      </div>
      <div style="padding:6px 13px;background:#faf8f5;font-size:11px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:4px">
        <div><span style="color:var(--faint)">Started:</span> ${fmtTime(run.started_at)}</div>
        <div><span style="color:var(--faint)">Finished:</span> ${fmtTime(run.finished_at)}</div>
      </div>
      <div style="padding:8px 13px;background:#faf8f5;font-size:12px;border-bottom:1px solid var(--line)">
        <span class="k">Input:</span> <span class="v">${esc((run.claim||'').substring(0,200))}</span>
      </div>
      <div style="padding:6px 13px;border-bottom:1px solid var(--line)">
        <span class="trace-toggle" onclick="toggleTrace('${traceId}')" style="cursor:pointer;font-size:11px;color:var(--accent);user-select:none">▸ Show trace</span>
      </div>
      <div class="trace" id="${traceId}" style="display:none">${traceHTML||'<div style="padding:10px;color:var(--faint)">No trace</div>'}</div>
      ${run.detail?.summary?`<div class="concl"><span class="k">Output:</span> ${esc(run.detail.summary.substring(0,500))}</div>`:''}
    </div>`;
  }).join('') : `<div class="empty" style="padding:20px">${runsFilter==='ALL'?'No execution history yet. Run an agent from the Control tab.':'No '+runsFilter+' runs found.'}</div>`;

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h2 style="margin:0">${esc(a?.name||sel)} — Run History</h2>
        <span class="vtag">${runs.length} runs</span>
      </div>
      ${filterBar}
      ${rows}
    </div></div>`;
}

/* ═══════════════ INTEGRATIONS — Code Generation ═══════════════ */
const INT_META={
  python:{icon:'🐍',label:'Python',desc:'Ready-to-use Python snippet using the requests library.'},
  javascript:{icon:'⚡',label:'JavaScript',desc:'Fetch-based JavaScript snippet for browser or Node.js.'},
  curl:{icon:'📟',label:'cURL',desc:'Command-line cURL request you can run directly in a terminal.'},
  openapi:{icon:'📄',label:'OpenAPI',desc:'OpenAPI 3.0 spec for this agent endpoint — import into Postman, Swagger, etc.'},
  webhook:{icon:'🔗',label:'Webhook',desc:'Incoming webhook setup for event-driven integrations.'}
};
let intFmt='python';

async function renderIntegrations(){
  if(!sel){document.getElementById('root').innerHTML='<div class="hint" style="padding:40px">Select an agent first to generate integrations.</div>';return;}
  const a=AGENTS.find(x=>x.id===sel);

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead">
        <div><h3>Integration Code</h3><div class="acct">${esc(a?.name||sel)}</div></div>
      </div>
      <div style="padding:12px 20px;background:#faf8f5;border-bottom:1px solid var(--line);font-size:12px;display:flex;align-items:center;gap:8px">
        <span style="color:var(--faint)">Base URL:</span>
        <input id="int-base-url" type="text" value="${location.origin}" style="flex:1;padding:4px 8px;border:1px solid var(--line);border-radius:4px;font-family:'IBM Plex Mono';font-size:11px" readonly>
        <button onclick="navigator.clipboard.writeText(document.getElementById('int-base-url').value)" style="padding:4px 8px;border:1px solid var(--line);border-radius:4px;background:var(--bg);cursor:pointer;font-size:10px">Copy URL</button>
      </div>
      <div class="tab-row">
        ${Object.entries(INT_META).map(([k,v])=>
          `<button class="tab-btn${intFmt===k?' active':''}" onclick="loadIntCode('${k}',this)">${v.icon} ${v.label}</button>`
        ).join('')}
      </div>
      <div id="int-desc" style="padding:8px 20px;font-size:11px;color:var(--faint);border-bottom:1px solid var(--line)">${INT_META[intFmt].desc}</div>
      <div class="sect" id="int-code"><div class="hint">Loading...</div></div>
    </div></div>`;
  loadIntCode(intFmt);
}

async function loadIntCode(fmt,btn){
  intFmt=fmt;
  if(btn){
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  }
  const descEl=document.getElementById('int-desc');
  if(descEl&&INT_META[fmt]) descEl.textContent=INT_META[fmt].desc;
  const d=await(await fetch('/api/agents/'+sel+'/integration/'+fmt)).json();
  const box=document.getElementById('int-code');
  if(d.ok){
    const highlighted=highlightCode(d.code,fmt);
    box.innerHTML=`<div class="code-block"><button class="copy-btn" onclick="copyIntCode(this)" style="transition:background .2s">📋 Copy</button><pre>${highlighted}</pre></div>
      <div style="margin-top:12px;padding:10px 14px;background:#faf8f5;border:1px solid var(--line);border-radius:6px;font-size:11px;display:flex;gap:12px;align-items:center">
        <span style="font-size:16px">${INT_META[fmt]?.icon||'📄'}</span>
        <div><b>Agent:</b> ${esc(d.agent_id)} · <b>Format:</b> ${fmt} · <span style="color:var(--faint)">Customize the endpoint URL and auth for your environment.</span></div>
      </div>`;
  }else{
    box.innerHTML=`<div class="flag">${esc(d.detail||'Error generating code')}</div>`;
  }
}
function copyIntCode(btn){
  const pre=btn.parentElement.querySelector('pre');
  navigator.clipboard.writeText(pre.textContent);
  const orig=btn.innerHTML;btn.innerHTML='✅ Copied!';btn.style.background='rgba(26,170,100,.25)';
  setTimeout(()=>{btn.innerHTML=orig;btn.style.background=''},1500);
}
function highlightCode(code,fmt){
  let h=esc(code);
  if(fmt==='python'){
    h=h.replace(/\b(import|from|def|return|if|else|elif|try|except|as|with|for|in|not|and|or|True|False|None|print|raise)\b/g,'<span style="color:#ff7b72">$1</span>');
    h=h.replace(/(["'])(?:(?!\1).)*\1/g,'<span style="color:#a5d6ff">$&</span>');
    h=h.replace(/(#[^\n]*)/g,'<span style="color:#8b949e">$1</span>');
  }else if(fmt==='javascript'){
    h=h.replace(/\b(const|let|var|function|return|if|else|try|catch|await|async|throw|new|true|false|null|undefined|console)\b/g,'<span style="color:#ff7b72">$1</span>');
    h=h.replace(/(["'`])(?:(?!\1).)*\1/g,'<span style="color:#a5d6ff">$&</span>');
    h=h.replace(/(\/\/[^\n]*)/g,'<span style="color:#8b949e">$1</span>');
  }else if(fmt==='curl'){
    h=h.replace(/\b(curl)\b/g,'<span style="color:#ff7b72">$1</span>');
    h=h.replace(/(-[A-Za-z]+)/g,'<span style="color:#d2a8ff">$1</span>');
    h=h.replace(/(["'])(?:(?!\1).)*\1/g,'<span style="color:#a5d6ff">$&</span>');
  }
  return h;
}

/* ═══════════════ DEPLOY ═══════════════ */
async function renderDeploy(){
  if(!sel){document.getElementById('root').innerHTML='<div class="hint" style="padding:40px">Select an agent first.</div>';return;}
  const a=AGENTS.find(x=>x.id===sel);
  const cfg=(await(await fetch('/api/agents/'+sel)).json()).config||{};

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div>
      <h2>${esc(a?.name||sel)} — Deployment</h2>

      <div class="panel" style="margin-bottom:16px">
        <div class="phead"><h3>Environments</h3></div>
        <div class="sect">
          <div class="env-card" style="border-left:3px solid var(--seal)">
            <div style="display:flex;justify-content:space-between;align-items:start">
              <div>
                <div class="env-name">Development</div>
                <div class="env-url">http://localhost:3000</div>
              </div>
              <span class="pill running">active</span>
            </div>
            <div class="env-status">
              <div class="health-dot green"></div>
              <span>Running · v${a?.version||1} · ${(cfg.model||{}).provider||'anthropic'}</span>
            </div>
          </div>

          <div class="env-card" style="border-left:3px solid var(--faint)">
            <div style="display:flex;justify-content:space-between;align-items:start">
              <div>
                <div class="env-name">Staging</div>
                <div class="env-url" style="color:var(--faint)">Not configured</div>
              </div>
              <span class="pill stopped">inactive</span>
            </div>
            <div class="env-status"><div class="health-dot gray"></div><span>Not deployed</span></div>
          </div>

          <div class="env-card" style="border-left:3px solid var(--faint)">
            <div style="display:flex;justify-content:space-between;align-items:start">
              <div>
                <div class="env-name">Production</div>
                <div class="env-url" style="color:var(--faint)">Not configured</div>
              </div>
              <span class="pill stopped">inactive</span>
            </div>
            <div class="env-status"><div class="health-dot gray"></div><span>Not deployed</span></div>
          </div>
        </div>
      </div>

      <div class="panel" style="margin-bottom:16px">
        <div class="phead"><h3>Deploy Agent</h3></div>
        <div class="sect">
          <div class="form-group" style="margin-bottom:12px"><label>Target Environment</label>
            <select id="deploy-env" style="padding:8px 10px;border:1px solid var(--line);border-radius:3px;font-size:13px">
              <option value="staging">Staging</option>
              <option value="production">Production</option>
            </select>
          </div>
          <div class="form-group" style="margin-bottom:12px"><label>Endpoint URL</label>
            <input id="deploy-url" placeholder="https://api.yourcompany.com/agents" style="padding:8px 10px;border:1px solid var(--line);border-radius:3px;font-size:13px">
          </div>
          <div class="form-group" style="margin-bottom:12px"><label>API Key / Auth Token</label>
            <input id="deploy-key" type="password" placeholder="Bearer token or API key" style="padding:8px 10px;border:1px solid var(--line);border-radius:3px;font-size:13px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button class="btn accent" onclick="deployAgent()">Deploy v${a?.version||1}</button>
            <span id="deploy-msg" style="font-size:11px"></span>
          </div>
          <div class="hint">Pushes the current agent config and version to the target environment. The agent will be available at the configured endpoint.</div>
        </div>
      </div>

      <div class="panel">
        <div class="phead"><h3>Deployment Checklist</h3></div>
        <div class="sect" style="font-size:12.5px;line-height:2">
          <div><input type="checkbox"> API key configured for ${(cfg.model||{}).provider||'provider'}</div>
          <div><input type="checkbox"> Data sources authenticated and accessible</div>
          <div><input type="checkbox"> Rate limits set on all tools</div>
          <div><input type="checkbox"> Audit logging enabled</div>
          <div><input type="checkbox"> Escalation routing configured</div>
          <div><input type="checkbox"> Agent tested with sample inputs</div>
          <div><input type="checkbox"> Confirm-before-action set appropriately</div>
        </div>
      </div>
    </div></div>`;
}

async function deployAgent(){
  const env=document.getElementById('deploy-env').value;
  const url=document.getElementById('deploy-url').value.trim();
  const msg=document.getElementById('deploy-msg');
  if(!url){msg.innerHTML='<span style="color:var(--brick)">Endpoint URL required</span>';return;}
  msg.innerHTML='<span style="color:var(--accent)"><span class="spin">&#x25D4;</span> Deploying...</span>';
  // Simulated deploy — in production this would POST config to the target
  setTimeout(()=>{
    msg.innerHTML='<span style="color:var(--seal)">Deployed to '+esc(env)+'</span>';
  },1500);
}

/* ═══════════════ HISTORY ═══════════════ */
async function renderHistory(){
  if(!sel){document.getElementById('root').innerHTML='<div class="hint" style="padding:40px">Select an agent first.</div>';return;}
  const d=await (await fetch('/api/agents/'+sel+'/history')).json();
  const a=AGENTS.find(x=>x.id===sel);
  const rows=d.history.length? d.history.map(h=>`<div class="hrow">
      <div class="htop"><span class="hver">v${h.version} &rarr; v${h.version+1}</span><span class="hat">${new Date(h.at).toLocaleString()}</span></div>
      <div style="font-size:12px">${esc(h.note)}</div>
      <div class="hby">by ${esc(h.by)}${h.changes&&h.changes.length?' · '+h.changes.map(c=>c.field.split('.').pop()).join(', '):''}</div>
      <div style="margin-top:8px"><button class="btn ghost" onclick="revert(${h.version})">Revert to v${h.version}</button></div>
    </div>`).join('') : '<div class="empty">No changes yet.</div>';
  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead"><div><h3>${esc(a?.name||sel)}</h3><div class="acct">Version History</div></div><div class="vtag">current v${d.current_version}</div></div>
      <div class="sect"><div class="hist">${rows}</div></div>
    </div></div>`;
}
async function revert(v){ await fetch('/api/agents/'+sel+'/revert/'+v,{method:'POST'}); await boot(); renderHistory(); }

/* ═══════════════ AUTOMATION ═══════════════ */
async function renderAutomation(){
  if(!sel){document.getElementById('root').innerHTML='<div class="hint" style="padding:40px">Select an agent first.</div>';return;}
  const a=AGENTS.find(x=>x.id===sel);
  const auto=await (await fetch('/api/agents/'+sel+'/automation')).json();
  const schedOpts=['disabled','daily','daily:06:00','daily:09:00','every_2h','every_4h','every_6h'];
  const eventOpts=['data_update','webhook_received','schedule_trigger','error_threshold','manual'];

  let lastRunStr='Never';
  if(auto.last_run && auto.last_run.timestamp){
    lastRunStr=new Date(auto.last_run.timestamp).toLocaleString()+(auto.last_run.success?' ✓':' ✗');
  }
  let nextRunStr='Not scheduled';
  if(auto.next_run) nextRunStr=new Date(auto.next_run).toLocaleString();

  const eventChecks=eventOpts.map(e=>`<label style="display:inline-flex;align-items:center;gap:6px;margin-right:12px;margin-bottom:6px">
    <input type="checkbox" class="auto-event" value="${e}" ${auto.event_triggers&&auto.event_triggers.includes(e)?'checked':''}>
    <span style="font-size:12px">${e}</span>
  </label>`).join('');

  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead"><div><h3>${esc(a?.name||sel)}</h3><div class="acct">Automation</div></div></div>
      <div class="sect">
        <h4>Enable Automation</h4>
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="auto_enabled" ${auto.enabled?'checked':''}>
          <span>Enable automated execution for this agent</span>
        </label>
      </div>
      <div class="sect">
        <h4>Schedule</h4>
        <select id="auto_schedule" style="padding:6px 8px;border:1px solid var(--line);border-radius:3px;font-size:12px">
          ${schedOpts.map(s=>`<option value="${s}" ${auto.schedule===s?'selected':''}>${s}</option>`).join('')}
        </select>
      </div>
      <div class="sect">
        <h4>Event Triggers</h4>
        <div style="display:flex;flex-wrap:wrap">${eventChecks}</div>
      </div>
      <div class="sect">
        <h4>Status</h4>
        <div style="font-size:12px;color:var(--muted);line-height:1.8">
          <div><b>Last run:</b> ${lastRunStr}</div>
          <div><b>Next run:</b> ${nextRunStr}</div>
        </div>
      </div>
      <div class="sect">
        <button class="btn accent" onclick="saveAutomation()">Save</button>
        <span id="auto_msg" style="margin-left:12px;font-size:11px"></span>
      </div>
    </div></div>`;
}
async function saveAutomation(){
  const enabled=document.getElementById('auto_enabled').checked;
  const schedule=document.getElementById('auto_schedule').value;
  const eventTriggers=Array.from(document.querySelectorAll('.auto-event:checked')).map(cb=>cb.value);
  const d=await(await fetch('/api/agents/'+sel+'/automation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,schedule,event_triggers:eventTriggers})})).json();
  const msg=document.getElementById('auto_msg');
  msg.textContent=d.ok?'Saved':'Error';
  msg.style.color=d.ok?'var(--seal)':'var(--brick)';
}

/* ═══════════════ EVENT LOG ═══════════════ */
async function renderEventLog(){
  const e=await (await fetch('/api/events?limit=200')).json();
  const agentSet=new Set(e.events.map(ev=>ev.agent_id));
  const agents=Array.from(agentSet).sort();
  let filterAgent='all';
  try{filterAgent=localStorage.getItem('eventFilterAgent')||'all';}catch(ex){}
  const filtered=filterAgent==='all'?e.events:e.events.filter(ev=>ev.agent_id===filterAgent);

  const typeColors={'run.start':'var(--seal)','run.complete':'#16a34a','run.error':'var(--brick)','run.escalate':'var(--ochre)','agent.registered':'var(--accent)','agent.deleted':'var(--brick)','datasource.added':'var(--seal)','datasource.removed':'var(--ochre)'};

  const eventRows=filtered.map(ev=>{
    const time=new Date(ev.timestamp).toLocaleTimeString();
    const date=new Date(ev.timestamp).toLocaleDateString();
    const color=typeColors[ev.event_type]||'var(--muted)';
    return `<div style="display:grid;grid-template-columns:100px 80px 160px 1fr;gap:12px;padding:9px 13px;border-bottom:1px solid var(--line);align-items:center;font-size:11.5px">
      <div style="font-family:'IBM Plex Mono';color:var(--faint)">${date}</div>
      <div style="font-family:'IBM Plex Mono';font-weight:600">${time}</div>
      <div style="font-family:'IBM Plex Mono';color:${color};font-weight:600;font-size:10.5px">${esc(ev.event_type)}</div>
      <div style="color:var(--muted)">${esc(ev.agent_id)}${ev.data?.message?' · '+esc(ev.data.message):''}</div>
    </div>`;
  }).join('');

  document.getElementById('root').innerHTML=`<div style="max-width:960px">
    <h2>Event Log</h2>
    <div class="hint" style="margin-bottom:14px">Agent execution events. Use to monitor, debug, and audit.</div>
    <div style="display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap">
      <span style="font-size:10px;font-weight:600;text-transform:uppercase;color:var(--muted);letter-spacing:.05em">Filter:</span>
      <select onchange="try{localStorage.setItem('eventFilterAgent',this.value)}catch(e){};setView('events')" style="padding:5px 8px;border:1px solid var(--line);border-radius:3px;font-family:'IBM Plex Mono';font-size:11px;background:var(--card)">
        <option value="all">All (${e.events.length})</option>
        ${agents.map(a=>`<option value="${a}" ${a===filterAgent?'selected':''}>${a}</option>`).join('')}
      </select>
      <button class="btn ghost" onclick="window.open('/api/events?limit=500')" style="font-size:10px;padding:4px 10px">Export JSON</button>
    </div>
    <div style="background:var(--card);border:1px solid var(--line);border-radius:4px;overflow:hidden">
      <div style="display:grid;grid-template-columns:100px 80px 160px 1fr;gap:12px;padding:9px 13px;background:#faf5ef;border-bottom:1px solid var(--line);font-size:10px;font-weight:600;text-transform:uppercase;color:var(--muted);letter-spacing:.05em;font-family:'IBM Plex Mono'">
        <div>Date</div><div>Time</div><div>Event</div><div>Details</div>
      </div>
      ${eventRows||'<div style="padding:20px;text-align:center;color:var(--faint)">No events yet</div>'}
    </div>
  </div>`;
}

/* ═══════════════ SETTINGS ═══════════════ */
async function renderSettings(){
  const s=await (await fetch('/api/settings')).json();
  const providers=['anthropic','openai','gemini','xai','perplexity','mistral','cohere','meta'];
  const models={
    anthropic:['claude-fable-5','claude-opus-5','claude-sonnet-5','claude-haiku-4-5','claude-opus-4-8','claude-opus-4-7','claude-opus-4-6','claude-sonnet-4-6'],
    openai:['gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','gpt-5.5','gpt-5.4'],
    gemini:['gemini-3.1-pro','gemini-3.7-flash','gemini-3.6-flash','gemini-3.5-flash-lite','gemini-2.5-pro','gemini-2.5-flash'],
    xai:['grok-3','grok-3-mini','grok-2'],
    perplexity:['sonar-pro','sonar','sonar-deep-research','sonar-reasoning-pro','sonar-reasoning'],
    mistral:['mistral-large-latest','mistral-medium-latest','mistral-small-latest','codestral-latest','pixtral-large-latest'],
    cohere:['command-r-plus','command-r','command-a-03-2025'],
    meta:['meta-llama/Llama-4-Maverick-17B-128E-Instruct-Turbo','meta-llama/Llama-4-Scout-17B-16E-Instruct','meta-llama/Meta-Llama-3.3-70B-Instruct-Turbo']
  };

  const provOpts=providers.map(p=>{
    const prov=s.providers?.[p]||{configured:false,masked:'',model:''};
    return `<div style="margin:12px 0;padding:14px;background:#faf8f5;border:1px solid var(--line);border-radius:4px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:10px">
        <input type="radio" name="active_provider" value="${p}" ${s.active===p?'checked':''} onchange="selectProvider('${p}')">
        <span style="font-weight:600;font-size:14px">${esc(prov.label||p)}</span>
        ${prov.configured?'<span style="font-family:\'IBM Plex Mono\';font-size:10px;color:var(--seal);margin-left:auto">connected</span>':'<span style="font-family:\'IBM Plex Mono\';font-size:10px;color:var(--faint);margin-left:auto">not set</span>'}
      </label>
      <div style="margin:0 0 0 24px">
        <div class="form-group" style="margin-bottom:8px"><label>API Key</label>
          <div style="display:flex;gap:6px;align-items:center">
            <input type="password" id="key_${p}" placeholder="sk-..." style="flex:1;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font-family:'IBM Plex Mono';font-size:11px">
            <button class="btn ghost" style="padding:4px 10px;font-size:11px" onclick="testProvider('${p}')">Test</button>
            <span id="test_${p}" style="font-size:11px;min-width:60px">${prov.masked?'('+prov.masked+')':''}</span>
          </div>
        </div>
        <div class="form-group"><label>Model</label>
          <select id="model_${p}" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font-size:12px">
            ${models[p].map(mod=>`<option value="${mod}" ${prov.model===mod?'selected':''}>${mod}</option>`).join('')}
          </select>
        </div>
      </div>
    </div>`;
  }).join('');

  document.getElementById('root').innerHTML=`<div style="max-width:620px">
    <h2>Settings</h2>
    <div class="hint" style="margin-bottom:16px">Configure LLM providers and models. Keys are stored locally and never leave your instance.</div>
    ${provOpts}
    <div style="margin-top:16px">
      <button class="btn accent" onclick="saveSettings()">Save Settings</button>
      <span id="settings_msg" style="margin-left:12px;font-size:11px"></span>
    </div>
  </div>`;
}

async function selectProvider(p){
  await(await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:p})})).json();
}
async function testProvider(p){
  const key=document.getElementById('key_'+p).value;
  if(!key){document.getElementById('test_'+p).textContent='no key';document.getElementById('test_'+p).style.color='var(--brick)';return;}
  const span=document.getElementById('test_'+p);
  span.textContent='testing...';span.style.color='var(--muted)';
  const d=await(await fetch('/api/settings/test/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})})).json();
  span.textContent=d.ok?'connected':'failed';
  span.style.color=d.ok?'var(--seal)':'var(--brick)';
}
async function saveSettings(){
  const keys={},models={};
  ['anthropic','openai','gemini','xai','perplexity','mistral','cohere','meta'].forEach(p=>{
    const k=document.getElementById('key_'+p).value;
    const m=document.getElementById('model_'+p).value;
    if(k) keys[p]=k;
    if(m) models[p]=m;
  });
  await(await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys,models})})).json();
  const msg=document.getElementById('settings_msg');
  msg.textContent='Saved';msg.style.color='var(--seal)';
  await boot();
}

async function setView(v){ view=v; await render(); }
boot();
</script>
</body></html>"""

if __name__ == "__main__":
    print("CORTEX Agent Ops Hub — http://localhost:3000")
    print("model-assisted" if API_KEY else "rule-based (set ANTHROPIC_API_KEY for model translation)")
    uvicorn.run(app, host="0.0.0.0", port=3000)
