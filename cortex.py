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
import time
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response, Cookie, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

import db as db_mod
from db import (
    get_db, init_db, gen_id, utcnow,
    User, Agent as AgentModel, Run as RunModel,
    DataSource as DataSourceModel, Setting, AuditLog, OAuthState, ApiKey,
    Webhook, AgentTemplate, Notification, AgentRelease,
    Attestation, UserRole, ApprovalRequest, AgentAuthorityProfile,
    AuthorizationDecision,
    get_or_create_setting, set_setting, log_audit,
    SessionLocal,
)
import auth as auth_mod
from authorization import evaluate as evaluate_authority, validate_profile
from auth import (
    hash_password, verify_password, create_token, decode_token,
    get_google_auth_url, get_github_auth_url,
    exchange_google_code, exchange_github_code,
    oauth_providers_available, BASE_URL,
)

app = FastAPI(title="Cortex", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Startup: initialize database ────────────────────────────────────
@app.on_event("startup")
def on_startup():
    global SETTINGS
    init_db()
    # Seed sample agents if the database is empty
    _db = SessionLocal()
    try:
        if _db.query(AgentModel).count() == 0:
            try:
                _seed_sample_agents(_db)
            except Exception:
                _db.rollback()  # another worker may have seeded first
        # Ensure default settings exist
        get_or_create_setting(_db, "provider_keys", {})
        get_or_create_setting(_db, "provider_models", dict(providers_mod.DEFAULT_MODELS))
        get_or_create_setting(_db, "active_provider", {"active": "anthropic"})
    finally:
        _db.close()
    # Load settings from DB into the in-memory cache
    SETTINGS = _load_settings()

    # Configure and start the continuous agent daemon
    daemon_mod.configure(
        session_factory=SessionLocal,
        agent_model=AgentModel,
        run_saver=_save_run_to_db,
        get_key_fn=get_key,
        get_model_fn=get_model,
        settings_getter=_load_settings,
        providers_mod=providers_mod,
        log_event_fn=log_event,
        car_engine=_car_engine,
    )
    daemon_mod.start_daemon()

    # Wire the agent-to-agent message bus to the real executor
    message_bus.set_trigger_fn(_bus_trigger_agent)

    # Enable observability plugins by default and start the evaluator loop
    for _p in ("cortex-logger", "cortex-alerts", "cortex-cost-guard"):
        try:
            plugin_manager.enable(_p)
        except Exception:
            pass
    _start_obs_evaluator()


def _bus_trigger_agent(to_agent: str, instruction: str, triggered_by: str = None,
                       user_id: str = None) -> dict:
    """Executor used by the agent-to-agent message bus to run a target agent."""
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == to_agent).first()
        if not a:
            return {"ok": False, "error": "agent not found"}
        agent_dict = {"id": a.id, "name": a.name, "description": a.description or "",
                      "config": copy.deepcopy(a.config or {})}
    finally:
        db.close()
    # Inject the triggering instruction as this cycle's standing instruction
    agent_dict["config"]["standing_instruction"] = instruction
    try:
        rec = daemon_mod._execute_cycle(to_agent, agent_dict)
        if isinstance(rec, dict) and rec.get("outcome"):
            rec.setdefault("run_id", gen_id())
            rec["user_id"] = user_id
            _save_run_to_db(to_agent, rec)
        return {"ok": True, "outcome": rec.get("outcome", ""),
                "detail": rec.get("detail", {})}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_OBS_EVALUATOR_STARTED = False

def _start_obs_evaluator():
    """Background loop: evaluate alert rules, purge expired recycle-bin agents."""
    global _OBS_EVALUATOR_STARTED
    if _OBS_EVALUATOR_STARTED:
        return
    _OBS_EVALUATOR_STARTED = True

    def _loop():
        purge_counter = 0
        while True:
            try:
                fired = obs.check_alerts()
                for alert in fired:
                    stream_manager.emit_alert(
                        alert.get("rule_name", ""), alert.get("severity", "info"),
                        alert.get("message", ""))
                    obs.metrics.gauge("alerts.firing", obs.alerts.stats()["firing"])
                # Purge expired recycle-bin agents once an hour (~ every 120 ticks)
                purge_counter += 1
                if purge_counter >= 120:
                    purge_counter = 0
                    _db = SessionLocal()
                    try:
                        n = purge_expired_agents(_db)
                        if n:
                            obs.log("info", f"Purged {n} expired agents from recycle bin",
                                    source="recycle-bin")
                    finally:
                        _db.close()
            except Exception:
                pass
            time.sleep(30)

    t = threading.Thread(target=_loop, daemon=True, name="obs-evaluator")
    t.start()


# ── Auth helpers ────────────────────────────────────────────────────

def _get_session(request: Request) -> dict | None:
    """Extract user from JWT cookie, Authorization header, or API key."""
    token = request.cookies.get("cortex_session")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and not auth_header.startswith("Bearer ctx_"):
            token = auth_header[7:]
    if not token:
        # Fallback: try API key authentication
        api_sess = _authenticate_api_key(request)
        if api_sess:
            return api_sess
        return None
    payload = decode_token(token)
    if not payload:
        # Token invalid — still try API key as fallback
        api_sess = _authenticate_api_key(request)
        if api_sess:
            return api_sess
        return None
    user_id = payload.get("sub", "")
    # Verify user still exists in DB and look up admin status
    is_admin = False
    # role/org live on the user row, not in the token: create_token() only
    # signs sub/email/name, so reading them from the payload always yielded
    # the "FDE" default no matter what the user actually chose at signup.
    role, org = "FDE", ""
    try:
        db = SessionLocal()
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            db.close()
            return None  # User was deleted — treat as unauthenticated
        is_admin = user.is_admin or False
        od = user.oauth_data or {}
        role = od.get("role") or "FDE"
        org = od.get("org") or ""
        db.close()
    except Exception:
        pass
    return {"email": payload.get("email", ""), "name": payload.get("name", ""),
            "role": role, "org": org,
            "user_id": user_id, "is_admin": is_admin}


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
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == body.email.lower()).first()
        if existing:
            raise HTTPException(400, "Email already registered")
        # First user ever registered becomes admin automatically
        is_first_user = db.query(User).count() == 0
        user = User(
            id=gen_id(), email=body.email.lower(),
            password_hash=hash_password(body.password),
            name=body.name, is_active=True, is_admin=is_first_user,
            oauth_data={"role": body.role, "org": body.org},
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_token(user.id, user.email, user.name)
        role = (user.oauth_data or {}).get("role", "FDE")
        org = (user.oauth_data or {}).get("org", "")
        return {"ok": True, "token": token, "user": {"name": user.name, "email": user.email, "role": role, "org": org, "is_admin": user.is_admin or False}}
    finally:
        db.close()

@app.post("/api/auth/login")
def login(body: LoginBody):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == body.email.lower()).first()
        if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "Invalid email or password")
        user.last_login = utcnow()
        db.commit()
        token = create_token(user.id, user.email, user.name)
        role = (user.oauth_data or {}).get("role", "FDE")
        org = (user.oauth_data or {}).get("org", "")
        return {"ok": True, "token": token, "user": {"name": user.name, "email": user.email, "role": role, "org": org, "is_admin": user.is_admin or False}}
    finally:
        db.close()

@app.post("/api/auth/logout")
def logout(request: Request):
    # JWT is stateless — client just clears the cookie
    return {"ok": True}

@app.get("/api/auth/me")
def auth_me(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    # Verify the user still exists in the database
    if sess.get("user_id") and not sess.get("is_api_key"):
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == sess["user_id"]).first()
            if not user:
                raise HTTPException(401, "User no longer exists — please sign up again")
        finally:
            db.close()
    return {"user": sess}

# ── OAuth Routes ────────────────────────────────────────────────────

@app.get("/api/auth/providers")
def auth_providers():
    """Return which OAuth providers are configured."""
    return oauth_providers_available()

@app.get("/api/auth/login/google")
def login_google():
    if not oauth_providers_available().get("google"):
        raise HTTPException(400, "Google OAuth not configured")
    state = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        db.add(OAuthState(state=state, provider="google"))
        db.commit()
    finally:
        db.close()
    return RedirectResponse(get_google_auth_url(state))

@app.get("/api/auth/callback/google")
async def callback_google(code: str = "", state: str = ""):
    db = SessionLocal()
    try:
        oauth_state = db.query(OAuthState).filter(OAuthState.state == state).first()
        if not oauth_state:
            raise HTTPException(400, "Invalid OAuth state")
        db.delete(oauth_state)
        db.commit()

        info = await exchange_google_code(code)
        user = db.query(User).filter(User.email == info["email"].lower()).first()
        if not user:
            user = User(id=gen_id(), email=info["email"].lower(), name=info["name"],
                        avatar_url=info.get("avatar_url", ""),
                        oauth_provider="google", oauth_id=info["oauth_id"],
                        oauth_data={"role": "FDE", "org": ""})
            db.add(user)
        else:
            user.oauth_provider = user.oauth_provider or "google"
            user.oauth_id = user.oauth_id or info["oauth_id"]
            user.avatar_url = user.avatar_url or info.get("avatar_url", "")
        user.last_login = utcnow()
        db.commit()
        db.refresh(user)
        token = create_token(user.id, user.email, user.name)
        resp = RedirectResponse("/")
        resp.set_cookie("cortex_session", token, httponly=True, samesite="lax", max_age=72*3600)
        return resp
    finally:
        db.close()

@app.get("/api/auth/login/github")
def login_github():
    if not oauth_providers_available().get("github"):
        raise HTTPException(400, "GitHub OAuth not configured")
    state = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        db.add(OAuthState(state=state, provider="github"))
        db.commit()
    finally:
        db.close()
    return RedirectResponse(get_github_auth_url(state))

@app.get("/api/auth/callback/github")
async def callback_github(code: str = "", state: str = ""):
    db = SessionLocal()
    try:
        oauth_state = db.query(OAuthState).filter(OAuthState.state == state).first()
        if not oauth_state:
            raise HTTPException(400, "Invalid OAuth state")
        db.delete(oauth_state)
        db.commit()

        info = await exchange_github_code(code)
        user = db.query(User).filter(User.email == info["email"].lower()).first()
        if not user:
            user = User(id=gen_id(), email=info["email"].lower(), name=info["name"],
                        avatar_url=info.get("avatar_url", ""),
                        oauth_provider="github", oauth_id=info["oauth_id"],
                        oauth_data={"role": "FDE", "org": ""})
            db.add(user)
        else:
            user.oauth_provider = user.oauth_provider or "github"
            user.oauth_id = user.oauth_id or info["oauth_id"]
            user.avatar_url = user.avatar_url or info.get("avatar_url", "")
        user.last_login = utcnow()
        db.commit()
        db.refresh(user)
        token = create_token(user.id, user.email, user.name)
        resp = RedirectResponse("/")
        resp.set_cookie("cortex_session", token, httponly=True, samesite="lax", max_age=72*3600)
        return resp
    finally:
        db.close()

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")


# ── Admin API ──────────────────────────────────────────────────────

def _require_admin(request: Request) -> dict:
    """Verify the request is from an admin user."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    if not sess.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return sess


@app.get("/api/admin/users")
def admin_list_users(request: Request):
    _require_admin(request)
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.desc()).all()
        return {"users": [
            {"id": u.id, "email": u.email, "name": u.name or "",
             "is_admin": u.is_admin or False, "is_active": u.is_active if u.is_active is not None else True,
             "created_at": u.created_at.isoformat() if u.created_at else None,
             "last_login": u.last_login.isoformat() if u.last_login else None,
             "oauth_provider": u.oauth_provider or "",
             "role": (u.oauth_data or {}).get("role", "FDE"),
             "org": (u.oauth_data or {}).get("org", "")}
            for u in users
        ]}
    finally:
        db.close()


class AdminToggleBody(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


@app.post("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, body: AdminToggleBody, request: Request):
    admin = _require_admin(request)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        # Prevent admin from removing their own admin status
        if body.is_admin is not None:
            if user_id == admin["user_id"] and not body.is_admin:
                raise HTTPException(400, "Cannot remove your own admin status. Transfer admin to another user first.")
            user.is_admin = body.is_admin
        if body.is_active is not None:
            if user_id == admin["user_id"] and not body.is_active:
                raise HTTPException(400, "Cannot deactivate yourself")
            user.is_active = body.is_active
        db.commit()
        log_audit(db, admin["user_id"], "admin_update_user",
                  {"target_user": user_id, "changes": body.dict(exclude_none=True)})
        return {"ok": True, "user_id": user_id}
    finally:
        db.close()


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, request: Request):
    admin = _require_admin(request)
    if user_id == admin["user_id"]:
        raise HTTPException(400, "Cannot delete yourself")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        if user.is_admin:
            raise HTTPException(400, "Cannot delete an admin user. Remove admin status first.")
        db.delete(user)
        db.commit()
        log_audit(db, admin["user_id"], "admin_delete_user", {"target_user": user_id, "email": user.email})
        return {"ok": True}
    finally:
        db.close()


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    _require_admin(request)
    db = SessionLocal()
    try:
        from sqlalchemy import func
        total_users = db.query(User).count()
        active_users = db.query(User).filter(User.is_active == True).count()
        admin_users = db.query(User).filter(User.is_admin == True).count()
        total_agents = db.query(AgentModel).count()
        total_runs = db.query(RunModel).count()
        token_totals = db.query(
            func.coalesce(func.sum(RunModel.input_tokens), 0),
            func.coalesce(func.sum(RunModel.output_tokens), 0),
            func.coalesce(func.sum(RunModel.total_tokens), 0),
        ).first()
        return {"total_users": total_users, "active_users": active_users,
                "admin_users": admin_users, "total_agents": total_agents,
                "total_runs": total_runs,
                "total_input_tokens": token_totals[0],
                "total_output_tokens": token_totals[1],
                "total_tokens": token_totals[2]}
    finally:
        db.close()


# ── API Key Management ─────────────────────────────────────────────

class CreateApiKeyBody(BaseModel):
    name: str
    scopes: list = ["agents:read", "agents:run"]


@app.post("/api/keys")
def create_api_key(body: CreateApiKeyBody, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    # Generate a random API key
    raw_key = f"ctx_{secrets.token_hex(24)}"
    prefix = raw_key[:12]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    db = SessionLocal()
    try:
        api_key = ApiKey(
            id=gen_id(), user_id=sess["user_id"],
            name=body.name, key_hash=key_hash, prefix=prefix,
            scopes=body.scopes, is_active=True,
        )
        db.add(api_key)
        db.commit()
        log_audit(db, sess["user_id"], "api_key.created", {"name": body.name, "prefix": prefix})
        # Return the full key ONCE — it can't be retrieved again
        return {"ok": True, "key": raw_key, "prefix": prefix, "name": body.name, "id": api_key.id}
    finally:
        db.close()


@app.get("/api/keys")
def list_api_keys(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        keys = db.query(ApiKey).filter(ApiKey.user_id == sess["user_id"]).order_by(ApiKey.created_at.desc()).all()
        return {"keys": [
            {"id": k.id, "name": k.name, "prefix": k.prefix,
             "scopes": k.scopes or [], "is_active": k.is_active or False,
             "created_at": k.created_at.isoformat() if k.created_at else None,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
            for k in keys
        ]}
    finally:
        db.close()


@app.delete("/api/keys/{key_id}")
def revoke_api_key(key_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        key = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == sess["user_id"]).first()
        if not key:
            raise HTTPException(404, "API key not found")
        db.delete(key)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _authenticate_api_key(request: Request) -> dict | None:
    """Check for API key in Authorization header (Bearer ctx_...)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ctx_"):
        return None
    raw_key = auth[7:]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    db = SessionLocal()
    try:
        api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash, ApiKey.is_active == True).first()
        if not api_key:
            return None
        api_key.last_used_at = utcnow()
        user = db.query(User).filter(User.id == api_key.user_id).first()
        db.commit()
        return {"user_id": api_key.user_id, "email": user.email if user else "",
                "name": user.name if user else "", "scopes": api_key.scopes or [],
                "is_api_key": True, "is_admin": False}
    finally:
        db.close()


def _check_scope(sess: dict, required_scope: str) -> bool:
    """Check if session has a required scope. JWT users have all scopes; API keys check explicitly."""
    if not sess:
        return False
    if not sess.get("is_api_key"):
        return True  # JWT-authenticated users have full access
    return required_scope in (sess.get("scopes") or [])


# ─────────────────────────────────────────────── webhooks
import hmac
import threading
import urllib.request

def _fire_webhooks(agent_id: str, event: str, payload: dict):
    """Fire matching webhook subscriptions in a background thread."""
    def _send():
        db = SessionLocal()
        try:
            hooks = db.query(Webhook).filter(
                Webhook.is_active == True,
                Webhook.failure_count < 5,
            ).all()
            for hook in hooks:
                if hook.agent_id and hook.agent_id != agent_id:
                    continue
                if hook.events and event not in hook.events:
                    continue
                body = json.dumps({"event": event, "agent_id": agent_id, "data": payload, "timestamp": utcnow().isoformat()}).encode()
                headers = {"Content-Type": "application/json"}
                if hook.secret:
                    sig = hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
                    headers["X-Cortex-Signature"] = f"sha256={sig}"
                try:
                    req = urllib.request.Request(hook.url, data=body, headers=headers, method="POST")
                    urllib.request.urlopen(req, timeout=10)
                    hook.failure_count = 0
                    hook.last_triggered_at = utcnow()
                except Exception:
                    hook.failure_count = (hook.failure_count or 0) + 1
                    if hook.failure_count >= 5:
                        hook.is_active = False
            db.commit()
        except Exception:
            pass
        finally:
            db.close()
    threading.Thread(target=_send, daemon=True).start()


def _create_notification(user_id: str, agent_id: str, event: str, title: str, body: str = ""):
    """Create an in-app notification for a user."""
    db = SessionLocal()
    try:
        notif = Notification(id=gen_id(), user_id=user_id, agent_id=agent_id,
                             event=event, title=title, body=body)
        db.add(notif)
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def _create_attestation(agent_id: str, run_id: str = None, sess: dict = None,
                        action: str = "", action_input: str = "", action_result: str = "",
                        action_summary: str = "", provider: str = "", model: str = "",
                        data_sources: list = None, input_tokens: int = 0, output_tokens: int = 0,
                        human_approval_required: bool = False, human_approval_granted: bool = False,
                        human_approver_id: str = None, policy_checked: bool = False,
                        policy_passed: bool = True, policy_details: dict = None):
    """Create an immutable, hash-chained attestation record."""
    db = SessionLocal()
    try:
        # Look up agent info
        agent = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        agent_name = agent.name if agent else ""
        agent_version = agent.version if agent else 1

        # Get previous hash for chain integrity
        prev = db.query(Attestation).filter(
            Attestation.agent_id == agent_id
        ).order_by(Attestation.created_at.desc()).first()
        prev_hash = prev.record_hash if prev else "0" * 64

        # Build the record
        record = Attestation(
            id=gen_id(), run_id=run_id, agent_id=agent_id,
            agent_name=agent_name, agent_version=agent_version,
            authorized_by=sess.get("user_id", "") if sess else "",
            authorizer_email=sess.get("email", "") if sess else "",
            auth_method="api_key" if (sess and sess.get("is_api_key")) else "session",
            provider=provider, model=model,
            action=action, action_input=(action_input or "")[:500],
            action_result=action_result, action_summary=(action_summary or "")[:500],
            data_sources_accessed=data_sources or [],
            policy_checked=policy_checked, policy_passed=policy_passed,
            policy_details=policy_details or {},
            human_approval_required=human_approval_required,
            human_approval_granted=human_approval_granted,
            human_approver_id=human_approver_id,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            prev_hash=prev_hash,
        )
        # Compute record hash for tamper evidence
        hash_input = f"{record.id}|{record.agent_id}|{record.authorized_by}|{record.action}|{record.action_result}|{record.provider}|{record.model}|{prev_hash}"
        record.record_hash = hashlib.sha256(hash_input.encode()).hexdigest()
        db.add(record)
        db.commit()
        return record.id
    except Exception:
        return None
    finally:
        db.close()


# ─────────────────────────────────────────────── attestation API

@app.get("/api/attestations")
def list_attestations(request: Request, agent_id: str = "", limit: int = 50):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        q = db.query(Attestation)
        if agent_id:
            q = q.filter(Attestation.agent_id == agent_id)
        records = q.order_by(Attestation.created_at.desc()).limit(limit).all()
        return {"attestations": [
            {"id": r.id, "run_id": r.run_id, "agent_id": r.agent_id,
             "agent_name": r.agent_name, "agent_version": r.agent_version,
             "authorized_by": r.authorizer_email, "auth_method": r.auth_method,
             "provider": r.provider, "model": r.model,
             "action": r.action, "action_input": r.action_input,
             "action_result": r.action_result, "action_summary": r.action_summary,
             "data_sources_accessed": r.data_sources_accessed or [],
             "human_approval_required": r.human_approval_required or False,
             "human_approval_granted": r.human_approval_granted or False,
             "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
             "total_tokens": r.total_tokens,
             "record_hash": r.record_hash, "prev_hash": r.prev_hash,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in records
        ]}
    finally:
        db.close()

@app.get("/api/attestations/{attestation_id}")
def get_attestation(attestation_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        r = db.query(Attestation).filter(Attestation.id == attestation_id).first()
        if not r:
            raise HTTPException(404, "Attestation not found")
        return {"id": r.id, "run_id": r.run_id, "agent_id": r.agent_id,
                "agent_name": r.agent_name, "agent_version": r.agent_version,
                "authorized_by": r.authorizer_email, "auth_method": r.auth_method,
                "provider": r.provider, "model": r.model,
                "action": r.action, "action_input": r.action_input,
                "action_result": r.action_result, "action_summary": r.action_summary,
                "data_sources_accessed": r.data_sources_accessed or [],
                "policy_checked": r.policy_checked or False, "policy_passed": r.policy_passed,
                "policy_details": r.policy_details or {},
                "human_approval_required": r.human_approval_required or False,
                "human_approval_granted": r.human_approval_granted or False,
                "human_approver_id": r.human_approver_id or "",
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "total_tokens": r.total_tokens,
                "record_hash": r.record_hash, "prev_hash": r.prev_hash,
                "created_at": r.created_at.isoformat() if r.created_at else None}
    finally:
        db.close()

@app.get("/api/attestations/verify/{agent_id}")
def verify_attestation_chain(agent_id: str, request: Request):
    """Verify the hash chain integrity for an agent's attestation records."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        records = db.query(Attestation).filter(
            Attestation.agent_id == agent_id
        ).order_by(Attestation.created_at.asc()).all()
        if not records:
            return {"ok": True, "total": 0, "message": "No attestation records"}
        broken = []
        for i, r in enumerate(records):
            expected_prev = records[i-1].record_hash if i > 0 else "0" * 64
            if r.prev_hash != expected_prev:
                broken.append({"id": r.id, "index": i, "expected_prev": expected_prev, "actual_prev": r.prev_hash})
        return {"ok": len(broken) == 0, "total": len(records), "broken_links": broken,
                "message": "Chain intact" if not broken else f"{len(broken)} broken link(s) detected"}
    finally:
        db.close()

# ─────────────────────────────────────────────── RBAC

@app.get("/api/roles")
def list_user_roles(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    if not sess.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    db = SessionLocal()
    try:
        roles = db.query(UserRole).all()
        users = {u.id: u for u in db.query(User).all()}
        return {"roles": [
            {"id": r.id, "user_id": r.user_id,
             "user_email": users[r.user_id].email if r.user_id in users else "",
             "user_name": users[r.user_id].name if r.user_id in users else "",
             "role": r.role, "scope": r.scope,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in roles
        ]}
    finally:
        db.close()

class SetRoleBody(BaseModel):
    user_id: str
    role: str = "operator"  # viewer | operator | admin
    scope: str = "global"

@app.post("/api/roles")
def set_user_role(body: SetRoleBody, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    if not sess.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    if body.role not in ("viewer", "operator", "admin"):
        raise HTTPException(400, "Invalid role. Must be: viewer, operator, admin")
    db = SessionLocal()
    try:
        existing = db.query(UserRole).filter(
            UserRole.user_id == body.user_id, UserRole.scope == body.scope
        ).first()
        if existing:
            existing.role = body.role
        else:
            db.add(UserRole(id=gen_id(), user_id=body.user_id, role=body.role,
                            scope=body.scope, granted_by=sess["user_id"]))
        db.commit()
        return {"ok": True}
    finally:
        db.close()

def _get_user_role(user_id: str, agent_id: str = None) -> str:
    """Get effective role for a user. Agent-scoped role overrides global."""
    db = SessionLocal()
    try:
        # Check admin flag first
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.is_admin:
            return "admin"
        # Check agent-specific role
        if agent_id:
            agent_role = db.query(UserRole).filter(
                UserRole.user_id == user_id, UserRole.scope == f"agent:{agent_id}"
            ).first()
            if agent_role:
                return agent_role.role
        # Check global role
        global_role = db.query(UserRole).filter(
            UserRole.user_id == user_id, UserRole.scope == "global"
        ).first()
        return global_role.role if global_role else "operator"  # default to operator
    finally:
        db.close()

# ─────────────────────────────────────────────── approval workflows

@app.get("/api/approvals")
def list_approvals(request: Request, status: str = "pending"):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        q = db.query(ApprovalRequest).filter(ApprovalRequest.status == status)
        approvals = q.order_by(ApprovalRequest.created_at.desc()).limit(50).all()
        return {"approvals": [
            {"id": a.id, "agent_id": a.agent_id, "run_id": a.run_id,
             "action": a.action, "context": a.context or {},
             "status": a.status, "requested_by": a.requested_by,
             "decided_by": a.decided_by, "decision_note": a.decision_note,
             "created_at": a.created_at.isoformat() if a.created_at else None,
             "expires_at": a.expires_at.isoformat() if a.expires_at else None}
            for a in approvals
        ]}
    finally:
        db.close()

class ApprovalDecisionBody(BaseModel):
    decision: str  # approved | rejected
    note: str = ""

@app.post("/api/approvals/{approval_id}")
def decide_approval(approval_id: str, body: ApprovalDecisionBody, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "Decision must be 'approved' or 'rejected'")
    # Check role — viewers can't approve
    role = _get_user_role(sess["user_id"])
    if role == "viewer":
        raise HTTPException(403, "Viewers cannot approve actions")
    db = SessionLocal()
    try:
        approval = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()
        if not approval:
            raise HTTPException(404, "Approval request not found")
        if approval.status != "pending":
            raise HTTPException(400, f"Already {approval.status}")
        approval.status = body.decision
        approval.decided_by = sess["user_id"]
        approval.decided_at = utcnow()
        approval.decision_note = body.note
        db.commit()
        # Notify the requester
        if approval.requested_by:
            _create_notification(approval.requested_by, approval.agent_id,
                                 f"approval.{body.decision}",
                                 f"Action {'approved' if body.decision == 'approved' else 'rejected'}: {approval.action}",
                                 body.note or "")
        return {"ok": True, "status": body.decision}
    finally:
        db.close()


# ─────────────────────────────────────────────── agent authority

def _authority_to_dict(row: AgentAuthorityProfile) -> dict:
    profile = dict(row.profile or {})
    profile.update({
        "id": row.id, "agent_id": row.agent_id, "version": row.version,
        "status": row.status, "default_decision": row.default_decision,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    })
    return profile


def _decision_to_dict(row: AuthorizationDecision) -> dict:
    return {
        "decision_id": row.id, "request_id": row.request_id,
        "agent_id": row.agent_id, "profile_version": row.profile_version,
        "action": row.action, "target_system": row.target_system,
        "decision": row.decision, "reasons": row.reasons or [],
        "obligations": row.obligations or [], "request_hash": row.request_hash,
        "approval_id": row.approval_id, "attestation_id": row.attestation_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _authorize_action(agent_id: str, action_request: dict, sess: dict = None,
                      create_approval: bool = True) -> dict:
    """Evaluate and persist a decision; return unconfigured for legacy agents."""
    db = SessionLocal()
    try:
        profile_row = db.query(AgentAuthorityProfile).filter(
            AgentAuthorityProfile.agent_id == agent_id,
            AgentAuthorityProfile.status == "active",
        ).first()
        if not profile_row:
            return {"configured": False, "decision": "ALLOW",
                    "reasons": ["No active authority profile; legacy compatibility mode"],
                    "obligations": []}
        request_data = dict(action_request)
        request_data["action"] = request_data.get("action") or ""
        approval = None
        approval_id = request_data.get("approval_id")
        if approval_id:
            approval = db.query(ApprovalRequest).filter(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.agent_id == agent_id,
                ApprovalRequest.action == request_data["action"],
            ).first()
            if approval:
                # Bind approval to the exact request that originally created it.
                # Callers may add approval_id, but may not alter the approved case.
                comparable = {key: value for key, value in request_data.items()
                              if key not in ("approval", "approval_id")}
                original = {key: value for key, value in (approval.context or {}).items()
                            if key not in ("approval", "approval_id")}
                if comparable == original:
                    request_data["approval"] = {"status": approval.status,
                                                "decided_by": approval.decided_by}
                else:
                    approval = None
                    approval_id = None
        profile = dict(profile_row.profile or {})
        profile.update({"status": profile_row.status,
                        "default_decision": profile_row.default_decision})
        result = evaluate_authority(profile, request_data)
        if result["decision"] == "HUMAN_REVIEW" and create_approval and not approval:
            approval_context = {key: value for key, value in request_data.items()
                                if key not in ("approval", "approval_id")}
            approval = ApprovalRequest(
                id=gen_id(), agent_id=agent_id,
                requested_by=(sess or {}).get("user_id"), action=request_data["action"],
                context=approval_context,
            )
            db.add(approval)
            db.flush()
            approval_id = approval.id
        canonical = json.dumps(request_data, sort_keys=True, separators=(",", ":"), default=str)
        row = AuthorizationDecision(
            id=gen_id(), request_id=request_data.get("request_id") or gen_id(),
            agent_id=agent_id, profile_version=profile_row.version,
            action=request_data["action"], target_system=request_data.get("target_system", ""),
            decision=result["decision"], reasons=result["reasons"],
            obligations=result["obligations"],
            request_hash=hashlib.sha256(canonical.encode()).hexdigest(), approval_id=approval_id,
        )
        db.add(row)
        db.commit()
        result.update({"configured": True, "decision_id": row.id,
                       "request_id": row.request_id, "profile_version": row.profile_version,
                       "approval_id": approval_id})
        return result
    finally:
        db.close()


class AuthorityProfileIn(BaseModel):
    status: str = "draft"
    default_decision: str = "BLOCK"
    credentials: list = []
    privileges: list = []
    metadata: dict = {}


@app.get("/api/agents/{agent_id}/authority")
def get_authority_profile(agent_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        row = db.query(AgentAuthorityProfile).filter(AgentAuthorityProfile.agent_id == agent_id).first()
        if not row:
            raise HTTPException(404, "authority profile not found")
        return _authority_to_dict(row)
    finally:
        db.close()


@app.put("/api/agents/{agent_id}/authority")
def put_authority_profile(agent_id: str, body: AuthorityProfileIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    if body.status not in ("draft", "active", "suspended"):
        raise HTTPException(400, "status must be draft, active, or suspended")
    if body.status == "active" and _get_user_role(sess["user_id"], agent_id) != "admin":
        raise HTTPException(403, "Admin authority is required to activate a profile")
    proposed = body.model_dump()
    errors = validate_profile(proposed)
    if errors:
        raise HTTPException(400, {"message": "Invalid authority profile", "errors": errors})
    db = SessionLocal()
    try:
        if not db.query(AgentModel).filter(AgentModel.id == agent_id).first():
            raise HTTPException(404, "agent not found")
        row = db.query(AgentAuthorityProfile).filter(AgentAuthorityProfile.agent_id == agent_id).first()
        if not row:
            row = AgentAuthorityProfile(id=gen_id(), agent_id=agent_id)
            db.add(row)
        else:
            row.version = (row.version or 0) + 1
        row.status = body.status
        row.default_decision = body.default_decision
        row.profile = {"credentials": body.credentials, "privileges": body.privileges,
                       "metadata": body.metadata}
        if body.status == "active":
            row.approved_by = sess["user_id"]
            row.approved_at = utcnow()
        db.commit()
        db.refresh(row)
        log_audit(db, agent_id=agent_id, user_id=sess["user_id"],
                  event="authority.profile.updated",
                  data={"version": row.version, "status": row.status})
        return {"ok": True, "profile": _authority_to_dict(row)}
    finally:
        db.close()


class AuthorizationIn(BaseModel):
    request_id: str = ""
    action: str
    environment: str = "production"
    data_scope: str = ""
    target_system: str = ""
    evidence: list = []
    financial_impact: float | None = None
    actions_last_hour: int = 0
    approval_id: str | None = None
    context: dict = {}


@app.post("/api/agents/{agent_id}/authorize")
def authorize_agent_action(agent_id: str, body: AuthorizationIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _authorize_action(agent_id, body.model_dump(), sess)


@app.get("/api/agents/{agent_id}/decisions")
def list_authorization_decisions(agent_id: str, request: Request, limit: int = 50):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        rows = db.query(AuthorizationDecision).filter(
            AuthorizationDecision.agent_id == agent_id
        ).order_by(AuthorizationDecision.created_at.desc()).limit(min(limit, 200)).all()
        return {"decisions": [_decision_to_dict(row) for row in rows]}
    finally:
        db.close()


class WebhookBody(BaseModel):
    url: str
    events: list = ["run.completed", "run.error"]
    agent_id: str = ""
    secret: str = ""

@app.post("/api/webhooks")
def create_webhook(body: WebhookBody, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        hook = Webhook(id=gen_id(), user_id=sess["user_id"], url=body.url,
                       events=body.events, agent_id=body.agent_id or None,
                       secret=body.secret or secrets.token_hex(16))
        db.add(hook)
        db.commit()
        return {"ok": True, "id": hook.id, "secret": hook.secret}
    finally:
        db.close()

@app.get("/api/webhooks")
def list_webhooks(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        hooks = db.query(Webhook).filter(Webhook.user_id == sess["user_id"]).order_by(Webhook.created_at.desc()).all()
        return {"webhooks": [
            {"id": h.id, "url": h.url, "events": h.events or [], "agent_id": h.agent_id or "",
             "is_active": h.is_active or False, "failure_count": h.failure_count or 0,
             "last_triggered_at": h.last_triggered_at.isoformat() if h.last_triggered_at else None,
             "created_at": h.created_at.isoformat() if h.created_at else None}
            for h in hooks
        ]}
    finally:
        db.close()

@app.delete("/api/webhooks/{hook_id}")
def delete_webhook(hook_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        hook = db.query(Webhook).filter(Webhook.id == hook_id, Webhook.user_id == sess["user_id"]).first()
        if not hook:
            raise HTTPException(404, "Webhook not found")
        db.delete(hook)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

@app.post("/api/webhooks/{hook_id}/test")
def test_webhook(hook_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        hook = db.query(Webhook).filter(Webhook.id == hook_id, Webhook.user_id == sess["user_id"]).first()
        if not hook:
            raise HTTPException(404, "Webhook not found")
        body = json.dumps({"event": "webhook.test", "agent_id": "test", "data": {"message": "Test from Cortex"}, "timestamp": utcnow().isoformat()}).encode()
        headers = {"Content-Type": "application/json"}
        if hook.secret:
            sig = hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Cortex-Signature"] = f"sha256={sig}"
        try:
            req = urllib.request.Request(hook.url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=10)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    finally:
        db.close()

# ─────────────────────────────────────────────── agent templates

class TemplateBody(BaseModel):
    name: str
    description: str = ""
    category: str = "custom"
    icon: str = ""

@app.post("/api/templates")
def create_template(body: TemplateBody, request: Request):
    """Create a new empty template or save from an existing agent (use /api/templates/from-agent)."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        tmpl = AgentTemplate(id=gen_id(), created_by=sess["user_id"], name=body.name,
                             description=body.description, category=body.category,
                             icon=body.icon, config={})
        db.add(tmpl)
        db.commit()
        return {"ok": True, "id": tmpl.id}
    finally:
        db.close()

class TemplateFromAgentBody(BaseModel):
    agent_id: str
    name: str
    description: str = ""
    category: str = "custom"
    icon: str = ""

@app.post("/api/templates/from-agent")
def create_template_from_agent(body: TemplateFromAgentBody, request: Request):
    """Snapshot an agent's current config as a reusable template."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        agent = db.query(AgentModel).filter(AgentModel.id == body.agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found")
        config_snapshot = {
            "config": agent.config or {},
            "endpoint": agent.endpoint or {},
            "description": agent.description or "",
            "agent_type": agent.agent_type or "custom",
        }
        tmpl = AgentTemplate(id=gen_id(), created_by=sess["user_id"], name=body.name,
                             description=body.description or agent.description,
                             category=body.category, icon=body.icon,
                             config=config_snapshot)
        db.add(tmpl)
        db.commit()
        return {"ok": True, "id": tmpl.id}
    finally:
        db.close()

@app.get("/api/templates")
def list_templates(request: Request):
    db = SessionLocal()
    try:
        tmpls = db.query(AgentTemplate).order_by(AgentTemplate.use_count.desc()).all()
        return {"templates": [
            {"id": t.id, "name": t.name, "description": t.description,
             "category": t.category, "icon": t.icon or "🤖",
             "is_public": t.is_public or False, "use_count": t.use_count or 0,
             "created_at": t.created_at.isoformat() if t.created_at else None}
            for t in tmpls
        ]}
    finally:
        db.close()

@app.post("/api/templates/{template_id}/clone")
def clone_from_template(template_id: str, request: Request):
    """Create a new agent from a template."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        tmpl = db.query(AgentTemplate).filter(AgentTemplate.id == template_id).first()
        if not tmpl:
            raise HTTPException(404, "Template not found")
        cfg = tmpl.config or {}
        slug = f"{tmpl.name.lower().replace(' ','-')}-{gen_id()[:6]}"
        agent = AgentModel(
            id=gen_id(), owner_id=sess["user_id"], slug=slug,
            name=f"{tmpl.name} (copy)", description=cfg.get("description", tmpl.description),
            agent_type=cfg.get("agent_type", "custom"),
            config=cfg.get("config", {}), endpoint=cfg.get("endpoint", {}),
            status="stopped", live=False, version=1,
        )
        db.add(agent)
        tmpl.use_count = (tmpl.use_count or 0) + 1
        db.commit()
        _baseline_snapshot(db, agent)
        return {"ok": True, "agent_id": agent.id, "slug": agent.slug}
    finally:
        db.close()

@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        tmpl = db.query(AgentTemplate).filter(AgentTemplate.id == template_id).first()
        if not tmpl:
            raise HTTPException(404, "Template not found")
        if tmpl.created_by != sess["user_id"] and not sess.get("is_admin"):
            raise HTTPException(403, "Not authorized")
        db.delete(tmpl)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

# ─────────────────────────────────────────────── notifications

@app.get("/api/notifications")
def list_notifications(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        notifs = db.query(Notification).filter(
            Notification.user_id == sess["user_id"]
        ).order_by(Notification.created_at.desc()).limit(50).all()
        unread = sum(1 for n in notifs if not n.is_read)
        return {"notifications": [
            {"id": n.id, "event": n.event, "title": n.title, "body": n.body,
             "agent_id": n.agent_id or "", "is_read": n.is_read or False,
             "created_at": n.created_at.isoformat() if n.created_at else None}
            for n in notifs
        ], "unread": unread}
    finally:
        db.close()

@app.post("/api/notifications/read")
def mark_notifications_read(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        db.query(Notification).filter(
            Notification.user_id == sess["user_id"], Notification.is_read == False
        ).update({"is_read": True})
        db.commit()
        return {"ok": True}
    finally:
        db.close()

@app.delete("/api/notifications/{notif_id}")
def dismiss_notification(notif_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        notif = db.query(Notification).filter(
            Notification.id == notif_id, Notification.user_id == sess["user_id"]
        ).first()
        if notif:
            db.delete(notif)
            db.commit()
        return {"ok": True}
    finally:
        db.close()


# ─────────────────────────────────────────────── AI assistant chat

class ChatBody(BaseModel):
    message: str
    history: list = []

@app.post("/api/assistant/chat")
def assistant_chat(body: ChatBody, request: Request):
    """Embedded AI assistant — answers questions about the user's agents using live data as context."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "Not authenticated")
    db = SessionLocal()
    try:
        # Gather context about the user's agents
        agents = db.query(AgentModel).all()
        recent_runs = db.query(RunModel).order_by(RunModel.started_at.desc()).limit(20).all()
        agent_summary = []
        for a in agents:
            runs = [r for r in recent_runs if r.agent_id == a.id]
            errors = sum(1 for r in runs if r.outcome == "ERROR")
            completed = sum(1 for r in runs if r.outcome == "COMPLETED")
            agent_summary.append(
                f"- {a.name} (id:{a.id}, status:{a.status}, live:{a.live}, type:{a.agent_type}, "
                f"success_rate:{a.containment}%, recent_runs:{len(runs)}, errors:{errors}, completed:{completed})"
            )
        context = f"""You are Cortex Assistant, an AI helper embedded in the Cortex agent operations platform.
You help users understand and manage their agents. Answer concisely and specifically.

Current agent fleet ({len(agents)} agents):
{chr(10).join(agent_summary) if agent_summary else 'No agents registered.'}

Recent run activity ({len(recent_runs)} recent runs):
{chr(10).join(f'- Agent {r.agent_id}: {r.outcome}, {r.total_tokens} tokens, {r.started_at}' for r in recent_runs[:10])}
"""
    finally:
        db.close()

    # Use the active provider to generate a response
    try:
        settings = _load_settings()
        active = settings.get("active", "anthropic")
        api_key = settings.get("keys", {}).get(active, "") or os.environ.get(_ENV_KEYS.get(active, ""), "")
        if not api_key:
            return {"reply": "No LLM provider configured. Go to Settings to add an API key, then I can help you here."}

        models_cfg = settings.get("models", {})
        model = models_cfg.get(active, "")

        # Build messages
        messages = [{"role": "user", "content": body.message}]

        result = providers_mod.run_tool_loop(
            provider=active, api_key=api_key, model=model,
            system=context, user_message=body.message,
            tools=[], max_steps=1
        )
        return {"reply": result.get("final_text", "I couldn't generate a response.")}
    except Exception as e:
        return {"reply": f"Assistant error: {str(e)[:200]}"}


# ─────────────────────────────────────────────── provider settings (Settings tab)
import providers as providers_mod
import automation as automation_mod
from car import CAR as CAREngine
import daemon as daemon_mod

# ── Enterprise modules (observability, usage, teams, integrations, comms, plugins, streaming) ──
from observability import obs
from usage import usage_tracker
from teams import team_manager, ROLES as TEAM_ROLES, role_can
from integrations import integration_manager
from agent_comm import message_bus
from ws_streaming import stream_manager
from plugins import plugin_manager
import phase2
from phase2 import discovery as p2_discovery, recorder as p2_recorder, patterns as p2_patterns
import db as _db_recovery  # recovery helpers live on the db module
from db import (
    AgentVersion, snapshot_agent_version, list_agent_versions, verify_version_chain,
    Secret, store_secret, resolve_secret, scrub_config_secrets,
    soft_delete_agent, restore_agent, restore_agent_version, list_recycle_bin,
    purge_expired_agents, RECYCLE_BIN_RETENTION_DAYS,
)

# ─────────────────────────────────────────────── CAR engine (singleton)
_car_engine = CAREngine()

# ─────────────────────────────────────────────── event log (in-memory rolling buffer)
EVENT_LOG = []  # list of {timestamp, agent_id, event_type, data}
MAX_EVENTS = 500

def log_event(agent_id: str, event_type: str, data: dict = None, persist: bool = True):
    """Log an event to the in-memory event log AND the database audit trail.

    Pass persist=False when the caller has already written its own richer
    log_audit row (with a user_id, say) — otherwise the same event lands in
    audit_log twice, and the second copy is the poorer of the two.
    """
    global EVENT_LOG
    EVENT_LOG.insert(0, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "event_type": event_type,
        "data": data or {}
    })
    if len(EVENT_LOG) > MAX_EVENTS:
        EVENT_LOG = EVENT_LOG[:MAX_EVENTS]
    # Also persist to the audit_log table
    if not persist:
        return
    try:
        _db = SessionLocal()
        log_audit(_db, agent_id=agent_id, event=event_type, data=data or {})
        _db.close()
    except Exception:
        pass

def diagnose_agent(agent_id: str):
    """Analyze agent runs to classify as build vs training problem."""
    db = SessionLocal()
    try:
        runs_q = db.query(RunModel).filter(RunModel.agent_id == agent_id).order_by(RunModel.started_at.desc()).limit(10).all()
        if not runs_q:
            return {"type": "unknown", "confidence": 0, "checklist": [], "evidence": "No runs yet"}
        runs = runs_q
        escalations = sum(1 for r in runs if r.outcome == "ESCALATED")
        errors = sum(1 for r in runs if r.outcome == "ERROR")
        escalation_rate = escalations / len(runs)
        error_rate = errors / len(runs)
        if error_rate > 0.3:
            return {
                "type": "build", "confidence": min(0.95, 0.6 + error_rate),
                "evidence": f"{error_rate*100:.0f}% error rate across runs",
                "checklist": ["Check agent timeout and retry settings", "Verify API connections and rate limits",
                              "Review error logs for infrastructure issues", "Test with minimal config to isolate problem",
                              "Check model availability and fallbacks"]
            }
        elif escalation_rate > 0.4:
            return {
                "type": "training", "confidence": min(0.95, 0.5 + escalation_rate),
                "evidence": f"{escalation_rate*100:.0f}% escalation rate",
                "checklist": ["Review escalated cases for decision patterns", "Adjust confidence threshold if too conservative",
                              "Enhance agent prompt with better context/examples", "Fine-tune model selection or parameters",
                              "Add more tool context or clarifying instructions"]
            }
        else:
            return {
                "type": "healthy", "confidence": 0.9,
                "evidence": f"Low error rate ({error_rate*100:.0f}%), low escalation ({escalation_rate*100:.0f}%)",
                "checklist": []
            }
    finally:
        db.close()

# ─────────────────────────────────────────────── settings helpers (DB-backed)

def _load_settings():
    """Load settings from the database, falling back to defaults."""
    db = SessionLocal()
    try:
        keys = get_or_create_setting(db, "provider_keys", {})
        models = get_or_create_setting(db, "provider_models", dict(providers_mod.DEFAULT_MODELS))
        active_row = get_or_create_setting(db, "active_provider", {"active": "anthropic"})
        active = active_row.get("active", "anthropic") if isinstance(active_row, dict) else "anthropic"
        return {"active": active, "keys": keys, "models": models}
    except Exception:
        return {"active": "anthropic", "keys": {}, "models": dict(providers_mod.DEFAULT_MODELS)}
    finally:
        db.close()

def _save_settings(s):
    """Persist settings to the database."""
    db = SessionLocal()
    try:
        set_setting(db, "provider_keys", s.get("keys", {}))
        set_setting(db, "provider_models", s.get("models", {}))
        set_setting(db, "active_provider", {"active": s.get("active", "anthropic")})
    finally:
        db.close()

# Settings loaded once at startup, refreshed on writes
SETTINGS = {"active": "anthropic", "keys": {}, "models": dict(providers_mod.DEFAULT_MODELS)}

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


# ---------------------------------------------------------------- agent helpers

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

def _agent_to_dict(a):
    """Convert a DB AgentModel row to the dict format the frontend expects."""
    cfg = a.config or {}
    return {
        "name": a.name, "description": a.description or "", "account": a.account or "",
        "status": a.status or "stopped", "version": a.version or 1,
        "live": a.live if a.live is not None else False,
        "type": a.agent_type or "custom",
        "endpoint": a.endpoint or {},
        "config": cfg,
        "containment": a.containment or 0, "resolution": a.resolution or 0,
        "escalation": a.escalation or 0, "clinical_flags": 0,
        "owner_id": a.owner_id or "",
        "lifecycle": a.lifecycle or "active",
        "lifecycle_note": a.lifecycle_note or "",
        "contact": a.contact or "",
    }

def _seed_sample_agents(db):
    """Insert sample agents into a fresh database."""
    samples = [
        ("sample-research", "Research Agent", "General-purpose research agent that searches and summarizes information",
         "Sample", "running", "sample", {"type": "embedded", "url": ""},
         {"model": {"provider": "anthropic", "model_name": "claude-sonnet-5", "temperature": 0.7, "max_tokens": 4096},
          "execution": {"timeout_seconds": 300, "max_retries": 3, "retry_delay_seconds": 60},
          "behavior": {"confidence_threshold": 0.75, "escalation_threshold": "high", "auto_escalate_on_error": True, "confirm_before_action": True},
          "standing_instruction": "Search for the latest developments in AI agent frameworks and orchestration platforms. Summarize key trends, new releases, and competitive landscape changes. Report findings concisely.",
          "run_interval_seconds": 300,
          "data_sources": [],
          "tools": [{"name": "web_search", "description": "Search the web", "parameters": ["query"], "rate_limit": 100},
                    {"name": "fetch_url", "description": "Fetch and read a URL", "parameters": ["url"], "rate_limit": 50},
                    {"name": "summarize", "description": "Summarize text content", "parameters": ["text"], "rate_limit": 100}],
          "integrations": [],
          "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": True}}),
        ("sample-router", "Router Agent", "Routes incoming requests to the appropriate handler based on intent",
         "Sample", "stopped", "sample", {"type": "embedded", "url": ""},
         {"model": {"provider": "anthropic", "model_name": "claude-sonnet-5", "temperature": 0.3, "max_tokens": 1024},
          "execution": {"timeout_seconds": 30, "max_retries": 2, "retry_delay_seconds": 5},
          "behavior": {"confidence_threshold": 0.8, "escalation_threshold": "moderate", "auto_escalate_on_error": True, "confirm_before_action": False},
          "standing_instruction": "Monitor incoming message queue. Classify each message by intent (support, billing, technical, feedback, spam) and route to the appropriate handler agent. Log classification confidence scores.",
          "run_interval_seconds": 30,
          "data_sources": [],
          "tools": [{"name": "classify_intent", "description": "Classify the intent of a message", "parameters": ["message", "categories"], "rate_limit": 200},
                    {"name": "route_request", "description": "Route to appropriate handler", "parameters": ["intent", "payload"], "rate_limit": 200}],
          "integrations": [],
          "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": False}}),
        ("sample-action", "Action Agent", "Executes actions in external systems based on instructions",
         "Sample", "stopped", "sample", {"type": "rest", "url": "https://your-api.example.com/agent"},
         {"model": {"provider": "openai", "model_name": "gpt-5.6-terra", "temperature": 0.5, "max_tokens": 2048},
          "execution": {"timeout_seconds": 120, "max_retries": 3, "retry_delay_seconds": 30},
          "behavior": {"confidence_threshold": 0.85, "escalation_threshold": "low", "auto_escalate_on_error": True, "confirm_before_action": True},
          "standing_instruction": "Check for pending action items in the task queue. Execute approved actions in external systems (CRM updates, notifications, record creation). Report results and any failures requiring human review.",
          "run_interval_seconds": 120,
          "data_sources": [{"name": "example_crm", "type": "api", "endpoint": "https://crm.example.com/api", "auth_type": "api_key", "auth_value": ""}],
          "tools": [{"name": "create_record", "description": "Create a new record", "parameters": ["type", "data"], "rate_limit": 50},
                    {"name": "update_record", "description": "Update an existing record", "parameters": ["id", "data"], "rate_limit": 50},
                    {"name": "send_notification", "description": "Send a notification", "parameters": ["recipient", "message"], "rate_limit": 100}],
          "integrations": [],
          "audit": {"log_all_calls": True, "log_data_access": True, "track_modifications": True}}),
    ]
    for slug, name, desc, acct, status, atype, endpoint, config in samples:
        agent = AgentModel(id=slug, slug=slug, name=name, description=desc, account=acct,
                           status=status, agent_type=atype, live=True, version=1,
                           endpoint=endpoint, config=config)
        db.add(agent)
    db.commit()
    # Snapshot every seeded sample, not just the last one the loop bound.
    for slug, *_ in samples:
        a = db.query(AgentModel).filter(AgentModel.id == slug).first()
        if a:
            _baseline_snapshot(db, a)

def _baseline_snapshot(db, agent, changed_by=None, changer_email=""):
    """Record v1 for a newly created agent.

    Without this an agent carries version=1 with no AgentVersion row, so the
    FIRST config change writes v1 (describing the new config) and leaves
    agent.version at 1 — meaning runs from before and after that change both
    record config_version=1 and pool into one bucket. Snapshotting at birth
    makes the first change v2, which is what the version history and the
    per-version comparison both assume.
    """
    try:
        snapshot_agent_version(
            db, agent, changed_by=changed_by, changer_email=changer_email,
            change_type="create", prev_config={},
            change_summary="Initial configuration",
        )
    except Exception as e:
        print(f"[cortex] baseline snapshot failed for {getattr(agent,'id','?')}: {e}")


# In-memory caches for transient state (version history + pending proposals)
HISTORY = {}  # agent_id -> list of {version, at, by, note, config}
PENDING = {}  # token -> {agent_id, request, diff, before, after}

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
def list_agents(request: Request):
    db = SessionLocal()
    try:
        # Exclude soft-deleted agents (they live in the recycle bin)
        agents = db.query(AgentModel).filter(
            (AgentModel.is_deleted == False) | (AgentModel.is_deleted == None)  # noqa: E711,E712
        ).all()
        # Build owner name lookup
        owner_ids = {a.owner_id for a in agents if a.owner_id}
        owner_names = {}
        if owner_ids:
            owners = db.query(User).filter(User.id.in_(owner_ids)).all()
            owner_names = {u.id: u.name or u.email for u in owners}
        running = [a for a in agents if a.status == "running"]
        return {
            "agents": [
                {"id": a.id,
                 "name": a.name,
                 "description": a.description or "",
                 "account": a.account or "",
                 "status": a.status or "stopped",
                 "version": a.version or 1,
                 "live": a.live if a.live is not None else False,
                 "type": a.agent_type or "custom",
                 "endpoint": a.endpoint or {},
                 "containment": a.containment or 0,
                 "resolution": a.resolution or 0,
                 "escalation": a.escalation or 0,
                 "clinical_flags": 0,
                 "data_sources_count": len((a.config or {}).get("data_sources", [])),
                 "tools_count": len((a.config or {}).get("tools", [])),
                 "posture": (a.config or {}).get("posture", a.agent_type or "custom"),
                 "owner_id": a.owner_id or "",
                 "owner_name": owner_names.get(a.owner_id, "") if a.owner_id else "",
                 "lifecycle": a.lifecycle or "active",
                 "contact": a.contact or "",
                 }
                for a in agents
            ],
            "total": len(agents),
            "running": len(running),
            "error": sum(1 for a in agents if a.status == "error"),
            "stopped": sum(1 for a in agents if a.status == "stopped"),
            "avg_containment": round(sum(a.containment or 0 for a in running) / len(running), 1) if running else 0,
            "llm": bool(get_key(SETTINGS["active"])),
        }
    finally:
        db.close()

@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        _ensure_history(agent_id)
        return {"id": agent_id, **_agent_to_dict(a), "history_count": len(HISTORY.get(agent_id, []))}
    finally:
        db.close()

@app.post("/api/agents/{agent_id}/propose")
def propose(agent_id: str, body: ProposeIn):
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        _ensure_history(agent_id)
        before = a.config or {}
        try:
            if get_key("anthropic"):
                after, notes = llm_translate(before, body.request)
            else:
                after, notes = deterministic_translate(before, body.request)
        except Exception:
            after, notes = deterministic_translate(before, body.request)
            notes.append("(model unavailable, used rule-based parse)")

        changes = _diff(before, after)
        if not changes:
            return {"ok": False, "message": "No change detected. Try naming a specific field — timing, retries, channel, call window, confidence threshold, escalation severity, or routing."}

        token = hashlib.sha256(f"{agent_id}{body.request}{datetime.now()}".encode()).hexdigest()[:12]
        PENDING[token] = {"agent_id": agent_id, "request": body.request, "changes": changes,
                          "before": copy.deepcopy(before), "after": after, "notes": notes,
                          "flags": _safety_flags(before, after)}
        return {"ok": True, "token": token, "request": body.request, "changes": changes,
                "notes": notes, "flags": PENDING[token]["flags"]}
    finally:
        db.close()

@app.post("/api/agents/{agent_id}/apply")
def apply(agent_id: str, body: ApplyIn, request: Request):
    p = PENDING.get(body.token)
    if not p or p["agent_id"] != agent_id:
        raise HTTPException(404, "proposal not found or expired")
    sess = _get_session(request)
    uid = sess.get("user_id") if sess else None
    uemail = sess.get("email", "") if sess else ""
    who = body.approved_by or uemail or "unknown"
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        _ensure_history(agent_id)
        prev_config = copy.deepcopy(a.config or {})
        HISTORY[agent_id].append({
            "version": a.version, "at": datetime.now(timezone.utc).isoformat(),
            "by": who, "note": p["request"],
            "config": prev_config, "changes": p["changes"],
        })
        a.config = p["after"]
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(a, "config")
        # Immutable, hash-chained version snapshot with user attribution
        snapshot_agent_version(
            db, a, changed_by=uid, changer_email=who, change_type="update",
            change_summary=p["request"][:500], prev_config=prev_config,
        )
        db.commit()
        del PENDING[body.token]
        return {"ok": True, "agent_id": agent_id, "new_version": a.version,
                "applied": p["changes"], "changed_by": who}
    finally:
        db.close()

@app.get("/api/agents/{agent_id}/history")
def history(agent_id: str):
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        _ensure_history(agent_id)
        return {"agent_id": agent_id, "current_version": a.version,
                "history": list(reversed(HISTORY.get(agent_id, [])))}
    finally:
        db.close()

@app.post("/api/agents/{agent_id}/revert/{version}")
def revert(agent_id: str, version: int):
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        _ensure_history(agent_id)
        entry = next((h for h in HISTORY.get(agent_id, []) if h["version"] == version), None)
        if not entry:
            raise HTTPException(404, "version not found in history")
        HISTORY[agent_id].append({
            "version": a.version, "at": datetime.now(timezone.utc).isoformat(),
            "by": "revert", "note": f"revert to v{version}",
            "config": copy.deepcopy(a.config or {}), "changes": [],
        })
        a.config = copy.deepcopy(entry["config"])
        a.version = (a.version or 1) + 1
        db.commit()
        return {"ok": True, "reverted_to": version, "new_version": a.version}
    finally:
        db.close()

@app.post("/api/agents/{agent_id}/control")
def control(agent_id: str, action: str = "start"):
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        a.status = {"start": "running", "restart": "running", "stop": "stopped",
                     "pause": a.status, "resume": a.status}.get(action, a.status)
        db.commit()
        # Daemon-level pause/resume (keeps agent "running" in DB but halts cycles)
        if action == "pause":
            daemon_mod.pause_agent(agent_id)
        elif action == "resume":
            daemon_mod.resume_agent(agent_id)
        return {"ok": True, "status": a.status}
    finally:
        db.close()


class StandingInstructionBody(BaseModel):
    standing_instruction: str = ""
    run_interval_seconds: int = 60


@app.post("/api/agents/{agent_id}/standing-instruction")
def update_standing_instruction(agent_id: str, body: StandingInstructionBody, request: Request):
    """Update an agent's standing instruction and run interval for continuous mode."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        prev_config = dict(a.config or {})
        cfg = dict(prev_config)
        cfg["standing_instruction"] = body.standing_instruction
        cfg["run_interval_seconds"] = max(30, body.run_interval_seconds)
        if cfg == prev_config:
            return {"ok": True, "unchanged": True, "config": cfg,
                    "version": a.version or 1}
        a.config = cfg
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(a, "config")
        db.commit()
        # The standing instruction is what a continuous agent actually does on
        # every cycle, so a change to it is a config change like any other and
        # has to be attributable — otherwise runs before and after it pool into
        # the same version bucket and the comparison silently lies.
        snap = snapshot_agent_version(
            db, a, changed_by=sess.get("user_id"),
            changer_email=sess.get("email", ""),
            change_type="update", prev_config=prev_config,
            change_summary="Standing instruction / run interval updated",
        )
        return {"ok": True, "unchanged": False, "config": cfg,
                "version": snap.version}
    finally:
        db.close()


@app.get("/api/daemon/status")
def daemon_status():
    """Get daemon status and all agent daemon states."""
    return {
        "running": daemon_mod.is_running(),
        "agents": daemon_mod.get_all_daemon_states(),
    }


@app.get("/api/agents/{agent_id}/daemon")
def agent_daemon_state(agent_id: str):
    """Get daemon state for a specific agent."""
    return daemon_mod.get_agent_daemon_state(agent_id)


class UpdateConfigBody(BaseModel):
    config: dict


@app.put("/api/agents/{agent_id}/config")
def update_agent_config(agent_id: str, body: UpdateConfigBody, request: Request):
    """Directly update an agent's config JSON (for integrations, etc)."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        a.config = body.config
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(a, "config")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ──────────────────────────────────────────────── agent registration & management

class RegisterAgentIn(BaseModel):
    name: str
    description: str = ""
    account: str = ""
    endpoint_type: str = "rest"  # rest | webhook | embedded
    endpoint_url: str = ""
    config: dict = {}

@app.post("/api/agents/register")
def register_agent(body: RegisterAgentIn, request: Request):
    """Register a new agent."""
    sess = _get_session(request)
    agent_id = _slugify(body.name)
    if not agent_id:
        raise HTTPException(400, "name must produce a valid slug")
    db = SessionLocal()
    try:
        # Ensure unique slug
        base_id = agent_id
        counter = 2
        while db.query(AgentModel).filter(AgentModel.id == agent_id).first():
            agent_id = f"{base_id}-{counter}"
            counter += 1

        cfg = _default_config()
        if body.config:
            for section in ("model", "execution", "behavior", "audit"):
                if section in body.config:
                    cfg[section].update(body.config[section])
            if "data_sources" in body.config:
                cfg["data_sources"] = body.config["data_sources"]
            if "tools" in body.config:
                cfg["tools"] = body.config["tools"]

        agent = AgentModel(
            id=agent_id, slug=agent_id, name=body.name,
            description=body.description, account=body.account or "Custom",
            status="stopped", agent_type="custom", live=True, version=1,
            endpoint={"type": body.endpoint_type, "url": body.endpoint_url},
            config=cfg,
            owner_id=sess["user_id"] if sess else None,
        )
        db.add(agent)
        db.commit()
        _baseline_snapshot(db, agent)
        db.refresh(agent)
        _ensure_history(agent_id)
        log_event(agent_id, "agent.registered", {"name": body.name})
        return {"ok": True, "agent_id": agent_id, "agent": {"id": agent_id, **_agent_to_dict(agent)}}
    finally:
        db.close()


# ── Auscult: clinical transcript intake ─────────────────────────────
class AuscultTranscriptIn(BaseModel):
    patient_id: str
    transcript: str
    provider: str = ""
    encounter_type: str = "office_visit"

@app.post("/api/auscult/intake")
def auscult_transcript_intake(body: AuscultTranscriptIn, request: Request):
    """
    Auscult transcript intake endpoint.
    Accepts a clinical encounter transcript, parses it into structured
    proposals, runs deterministic safety checks, and creates ApprovalRequest
    records for physician attestation. Nothing writes to the chart until approved.
    """
    sess = _get_session(request)
    db = SessionLocal()
    try:
        # Verify the Auscult agent is registered
        agent = db.query(AgentModel).filter(
            AgentModel.id == "auscult",
            AgentModel.is_deleted == False
        ).first()
        if not agent:
            raise HTTPException(404, "Auscult agent not registered. POST /api/agents/register first.")

        # Create a run record
        run = RunModel(
            agent_id="auscult",
            claim=f"Auscult intake: {body.patient_id} ({body.encounter_type})",
            outcome="running",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        # In production, this invokes the AuscultAgent from cortex_agents_framework.py.
        # For the proof-of-concept, we run the deterministic safety pipeline inline
        # so the endpoint works without an Anthropic API key.

        # ── Mock parsed proposals (in prod: AuscultAgent.parse_transcript) ──
        proposals = [
            {"proposal_id": "RX-001", "type": "medication", "action": "modify",
             "medication": "lisinopril", "dose": "20mg daily",
             "details": "Increase for persistent HTN — BP 148/92"},
            {"proposal_id": "LAB-001", "type": "lab_order", "action": "order",
             "details": "BMP — recheck K+ and renal function post dose change"},
            {"proposal_id": "LAB-002", "type": "lab_order", "action": "order",
             "details": "Lipid panel — overdue > 12 months"},
            {"proposal_id": "REF-001", "type": "referral", "action": "order",
             "details": "Cardiology — persistent uncontrolled HTN"},
            {"proposal_id": "RX-002", "type": "medication", "action": "new",
             "medication": "amoxicillin", "dose": "500mg TID x 10 days",
             "details": "Dental abscess"},
        ]

        # ── Mock chart context ──
        chart = {
            "allergies": ["amoxicillin (anaphylaxis/severe)", "sulfa (rash/moderate)"],
            "active_meds": ["lisinopril 10mg daily", "atorvastatin 40mg daily", "metformin 500mg BID"],
        }

        # ── Deterministic safety checks ──
        checked = []
        for p in proposals:
            status = "SAFE"
            notes = ""
            alternative = None
            med = (p.get("medication") or "").lower()

            if med == "amoxicillin":
                status = "BLOCKED"
                notes = "ALLERGY CONFLICT — amoxicillin: anaphylaxis (severe)"
                alternative = "clindamycin 300mg TID x 10 days"
            elif med == "lisinopril" and "20mg" in p.get("dose", ""):
                notes = "Dose in range (2.5–40mg). Recheck K+ recommended."

            checked.append({**p, "safety_status": status, "safety_notes": notes, "alternative": alternative})

        # ── Create ApprovalRequest per proposal ──
        approval_ids = []
        for p in checked:
            ar = ApprovalRequest(
                agent_id="auscult",
                run_id=run.id,
                requested_by=body.provider or "Auscult Agent",
                action=f"{p['type']}: {p['action']} — {p.get('medication') or p['details'][:40]}",
                context={
                    "patient_id": body.patient_id,
                    "proposal": p,
                    "safety_status": p["safety_status"],
                    "safety_notes": p["safety_notes"],
                    "alternative": p.get("alternative"),
                    "encounter_type": body.encounter_type,
                },
                status="pending",
            )
            db.add(ar)
            db.commit()
            db.refresh(ar)
            approval_ids.append(ar.id)

        # Update run
        run.outcome = "awaiting_approval"
        run.detail = json.dumps({
            "proposals": len(checked),
            "safe": sum(1 for c in checked if c["safety_status"] == "SAFE"),
            "blocked": sum(1 for c in checked if c["safety_status"] == "BLOCKED"),
            "approval_ids": approval_ids,
        })
        db.commit()

        log_event("auscult", "auscult.intake", {
            "patient_id": body.patient_id,
            "proposals": len(checked),
            "blocked": sum(1 for c in checked if c["safety_status"] == "BLOCKED"),
            "run_id": run.id,
        })

        return {
            "ok": True,
            "run_id": run.id,
            "patient_id": body.patient_id,
            "proposals_parsed": len(checked),
            "safety_summary": {
                "safe": sum(1 for c in checked if c["safety_status"] == "SAFE"),
                "warning": sum(1 for c in checked if c["safety_status"] == "WARNING"),
                "blocked": sum(1 for c in checked if c["safety_status"] == "BLOCKED"),
            },
            "checked_proposals": checked,
            "approval_ids": approval_ids,
            "chart_context": chart,
            "note": "All proposals routed to Cortex approval gate. Attest via POST /api/approvals/{id}",
        }
    finally:
        db.close()

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
    db = SessionLocal()
    try:
        base_id = agent_id
        counter = 2
        while db.query(AgentModel).filter(AgentModel.id == agent_id).first():
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

        agent = AgentModel(
            id=agent_id, slug=agent_id, name=normalized["name"],
            description=normalized["description"],
            account=f"Imported ({normalized.get('source_format', 'auto')})",
            status="stopped", agent_type="imported", live=True, version=1,
            endpoint={"type": normalized.get("endpoint_type", "embedded"), "url": normalized.get("endpoint_url", "")},
            config=cfg,
        )
        db.add(agent)
        db.commit()
        _baseline_snapshot(db, agent)
        db.refresh(agent)
        _ensure_history(agent_id)
        log_event(agent_id, "agent.imported", {"name": normalized["name"], "source_format": normalized.get("source_format")})
        return {"ok": True, "agent_id": agent_id, "detected_format": normalized.get("source_format", "raw"),
                "agent": {"id": agent_id, **_agent_to_dict(agent)}}
    finally:
        db.close()

@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request, purge: bool = False):
    """Move an agent to the recycle bin (recoverable). ?purge=true hard-deletes (admin only)."""
    sess = _get_session(request)
    uid = sess.get("user_id") if sess else None
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        name = a.name
        if purge:
            # Permanent deletion — restricted to admins
            if not (sess and sess.get("is_admin")):
                raise HTTPException(403, "permanent deletion requires admin")
            db.delete(a)
            db.commit()
            HISTORY.pop(agent_id, None)
            log_event(agent_id, "agent.purged", {"name": name})
            return {"ok": True, "purged": agent_id}
        # Soft-delete → recycle bin, fully recoverable
        result = soft_delete_agent(db, a, deleted_by=uid)
        try:
            daemon_mod.pause_agent(agent_id)
        except Exception:
            pass
        log_event(agent_id, "agent.deleted", {"name": name, "recoverable": True})
        return {"ok": True, "deleted": agent_id, "recoverable": True,
                "recoverable_until": result.get("recoverable_until"),
                "retention_days": RECYCLE_BIN_RETENTION_DAYS}
    finally:
        db.close()


# ──────────────────────────────────────────────── data sources

class DataSourceIn(BaseModel):
    name: str
    type: str = "api"  # api | database | file | webhook | graphql | grpc | custom
    endpoint: str = ""
    auth_type: str = "api_key"  # api_key | oauth2 | bearer | basic | connection_string | iam | none
    auth_value: str = ""
    refresh: str = "manual"  # realtime | 5m | 1h | 1d | manual

@app.post("/api/agents/{agent_id}/data-sources")
def add_data_source(agent_id: str, body: DataSourceIn, request: Request):
    """Add a data source to an agent's config.

    The credential does not go into config. It is encrypted into the secrets
    table and config keeps only auth_ref plus a hint, so the value is never
    snapshotted into version history, never diffed, and never served back.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        ds = {"name": body.name, "type": body.type, "endpoint": body.endpoint,
              "auth_type": body.auth_type, "refresh": body.refresh,
              "auth_ref": "", "auth_hint": ""}
        if body.auth_value:
            row = store_secret(db, body.auth_value, agent_id=agent_id,
                               label=body.name, created_by=sess.get("user_id"))
            ds["auth_ref"] = row.id
            ds["auth_hint"] = row.hint
        cfg = copy.deepcopy(a.config or {})
        cfg.setdefault("data_sources", []).append(ds)
        a.config = cfg
        db.commit()
        # The hint, never the value — this response is rendered in the UI.
        log_event(agent_id, "datasource.added", {"name": body.name})
        return {"ok": True, "data_source": ds, "total": len(cfg["data_sources"])}
    finally:
        db.close()

@app.delete("/api/agents/{agent_id}/data-sources/{source_name}")
def remove_data_source(agent_id: str, source_name: str, request: Request):
    """Remove a data source from an agent's config, and its stored credential."""
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        cfg = copy.deepcopy(a.config or {})
        sources = cfg.get("data_sources", [])
        before_len = len(sources)
        going = [s for s in sources if s.get("name") == source_name]
        cfg["data_sources"] = [s for s in sources if s.get("name") != source_name]
        if len(cfg["data_sources"]) == before_len:
            raise HTTPException(404, "data source not found")
        # Removing the source removes its credential. Leaving the row behind
        # would accumulate decryptable secrets nothing references any more.
        for s_ in going:
            ref = s_.get("auth_ref")
            if ref:
                db.query(Secret).filter(Secret.id == ref).delete()
        a.config = cfg
        db.commit()
        log_event(agent_id, "datasource.removed", {"name": source_name})
        return {"ok": True, "removed": source_name, "remaining": len(cfg["data_sources"])}
    finally:
        db.close()


# ──────────────────────────────────────────────── integration code generation

@app.get("/api/agents/{agent_id}/integration/{fmt}")
def generate_integration(agent_id: str, fmt: str):
    """Generate client integration code for an agent."""
    db = SessionLocal()
    try:
        a_row = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a_row:
            raise HTTPException(404, "agent not found")
        a = _agent_to_dict(a_row)
    finally:
        db.close()

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


def _save_run_to_db(agent_id: str, rec: dict):
    """Persist a run record to the database, fire webhooks, and create notifications."""
    try:
        db = SessionLocal()
        run_id = gen_id()
        rec["id"] = run_id          # so callers can reference the run they just caused
        run = RunModel(
            id=run_id, agent_id=agent_id,
            claim=rec.get("claim", ""),
            outcome=rec.get("outcome", ""),
            published=rec.get("published", False),
            steps_used=rec.get("steps_used", 0),
            config_version=rec.get("config_version", 1),
            provider=rec.get("provider", ""),
            model=rec.get("model", ""),
            trace=rec.get("trace", []),
            detail=rec.get("detail", {}),
            input_tokens=rec.get("input_tokens", 0),
            output_tokens=rec.get("output_tokens", 0),
            total_tokens=rec.get("total_tokens", 0),
            user_id=rec.get("user_id"),
            started_at=datetime.fromisoformat(rec["started_at"]) if rec.get("started_at") else utcnow(),
            finished_at=datetime.fromisoformat(rec["finished_at"]) if rec.get("finished_at") else utcnow(),
        )
        db.add(run)
        db.commit()
        # Look up agent name for notifications
        agent_name = agent_id
        agent_row = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if agent_row:
            agent_name = agent_row.name or agent_id
        db.close()
    except Exception:
        agent_name = agent_id

    # Feed run data into the Adaptive Runtime engine
    try:
        started = datetime.fromisoformat(rec["started_at"]) if rec.get("started_at") else utcnow()
        finished = datetime.fromisoformat(rec["finished_at"]) if rec.get("finished_at") else utcnow()
        latency_ms = (finished - started).total_seconds() * 1000 if started and finished else 0
        _car_engine.record_run(agent_id, {
            "provider": rec.get("provider", "unknown"),
            "model": rec.get("model", "unknown"),
            "task_type": rec.get("task_type", "general"),
            "tokens": rec.get("total_tokens", 0),
            "latency_ms": latency_ms,
            "cost": rec.get("cost", 0.0),
            "success": rec.get("outcome", "") == "COMPLETED",
        })
    except Exception:
        pass  # CAR is best-effort; never block the run pipeline

    # Feed the observability engine (metrics, SLOs, logs, traces) — best-effort
    try:
        started = datetime.fromisoformat(rec["started_at"]) if rec.get("started_at") else utcnow()
        finished = datetime.fromisoformat(rec["finished_at"]) if rec.get("finished_at") else utcnow()
        duration_s = (finished - started).total_seconds() if started and finished else 0.0
        outcome_norm = "success" if rec.get("outcome", "") == "COMPLETED" else rec.get("outcome", "error").lower()
        obs.record_run(
            agent_id=agent_id, run_id=rec.get("run_id", ""),
            duration_seconds=duration_s, outcome=outcome_norm,
            tokens_used=rec.get("total_tokens", 0), user_id=rec.get("user_id"),
        )
    except Exception:
        pass

    # Meter usage and attribute cost — best-effort
    try:
        usage_tracker.record(
            agent_id=agent_id, user_id=rec.get("user_id") or "system",
            provider=rec.get("provider", ""), model=rec.get("model", ""),
            input_tokens=rec.get("input_tokens", 0),
            output_tokens=rec.get("output_tokens", 0),
            run_id=rec.get("run_id"),
        )
    except Exception:
        pass

    # Stream the run completion to any live WebSocket subscribers — best-effort
    try:
        stream_manager.emit_run_complete(
            agent_id, rec.get("run_id", ""), rec.get("outcome", ""),
            (rec.get("detail") or {}).get("summary", ""),
        )
    except Exception:
        pass

    # Fire webhooks based on outcome
    outcome = rec.get("outcome", "")
    event_map = {"COMPLETED": "run.completed", "ESCALATED": "run.escalated", "ERROR": "run.error"}
    event = event_map.get(outcome)
    if event:
        _fire_webhooks(agent_id, event, {
            "outcome": outcome, "claim": rec.get("claim", "")[:200],
            "steps_used": rec.get("steps_used", 0),
            "total_tokens": rec.get("total_tokens", 0),
            "summary": (rec.get("detail") or {}).get("summary", "")[:300],
        })

    # Create in-app notification for the agent owner
    user_id = rec.get("user_id")
    if user_id:
        if outcome == "ERROR":
            _create_notification(user_id, agent_id, "run.error",
                                 f"Agent '{agent_name}' encountered an error",
                                 (rec.get("detail") or {}).get("reason", "")[:200])
        elif outcome == "ESCALATED":
            _create_notification(user_id, agent_id, "run.escalated",
                                 f"Agent '{agent_name}' escalated a case",
                                 rec.get("claim", "")[:200])

    # Create attestation record
    _create_attestation(
        agent_id=agent_id, run_id=rec.get("run_id"),
        sess={"user_id": rec.get("user_id", ""), "email": rec.get("user_email", "")},
        action="run.execute", action_input=rec.get("claim", ""),
        action_result=outcome,
        action_summary=(rec.get("detail") or {}).get("summary", ""),
        provider=rec.get("provider", ""), model=rec.get("model", ""),
        input_tokens=rec.get("input_tokens", 0),
        output_tokens=rec.get("output_tokens", 0),
    )


# The escalate tool every agent gets.
#
# ESCALATED is a first-class outcome — it is in the runs enum, on the Runs
# filter tabs, and it drives the escalation metric on every agent card. But
# providers.run_tool_loop only sets escalated=True when a tool whose NAME
# contains "escalate" is called, and no agent config defines one. So the flag
# could never fire, every non-crashing run recorded COMPLETED, and the outcome
# column measured uptime rather than judgement.
#
# Giving every agent this tool is what makes "I can't do this confidently" a
# recordable result instead of a sentence buried in a summary nobody parses.
ESCALATE_TOOL = {
    "name": "escalate",
    "description": (
        "Escalate this task to a human instead of answering. Use when you "
        "cannot complete the task confidently, the request is ambiguous or "
        "out of scope, a required tool or data source is unavailable, or "
        "acting would be risky without review. Escalating is a valid outcome "
        "and is preferred over guessing."
    ),
    "parameters": ["reason"],
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string",
                                  "description": "Why this needs a human."}},
        "required": ["reason"],
    },
}


def _tools_for(tools_cfg: list) -> list:
    """Agent-configured tools plus the implicit escalate tool.

    An agent that already defines its own escalate-style tool keeps it; we do
    not shadow a real handler with this one.
    """
    tools = [{
        "name": t["name"],
        "description": t.get("description", ""),
        "input_schema": {"type": "object", "properties": {
            p: {"type": "string"} for p in t.get("parameters", [])}},
    } for t in (tools_cfg or [])]

    if not any("escalate" in (t.get("name") or "").lower() for t in tools):
        tools.append({k: ESCALATE_TOOL[k]
                      for k in ("name", "description", "input_schema")})
    return tools


def _run_tool(name: str, input_data: dict) -> str:
    """Execute a tool call.

    escalate is genuinely implemented — calling it is the signal, and
    run_tool_loop has already recorded it by the time this returns. Everything
    else has no handler wired up yet and says so plainly, so the model can
    reason about the failure rather than treating a placeholder as data.
    """
    if "escalate" in (name or "").lower():
        reason = (input_data or {}).get("reason", "no reason given")
        return f"Escalated to a human. Reason recorded: {reason}"
    return (f"[Tool '{name}' called with {json.dumps(input_data)}. "
            f"No handler is registered for this tool, so no real action was "
            f"taken and no data was returned.]")


def build_system_prompt(agent: dict, continuous: bool = False) -> str:
    """Assemble the system prompt an agent is actually run with.

    One implementation, used by both the single-run path and the continuous
    daemon, so what the Prompt tab previews is character-for-character what
    the model receives. Order matters:

        identity  →  the author's system prompt  →  live operating config

    The operating config goes last on purpose. Cortex is the source of truth
    for how an agent behaves, so a prompt that says "escalate nothing" cannot
    quietly override an escalation threshold set in config.
    """
    cfg = agent.get("config") or {}
    behavior = cfg.get("behavior", {})
    tools_cfg = cfg.get("tools", [])

    parts = [f"You are {agent.get('name') or agent.get('id', 'this agent')}."]
    if agent.get("description"):
        parts.append(agent["description"])

    authored = (cfg.get("system_prompt") or "").strip()
    if authored:
        parts.append("\n" + authored)

    parts.append("\nOPERATING CONFIG (live from Cortex — obey it):")
    if continuous:
        parts.append("- mode: continuous (always-on)")
    parts.append(f"- confidence threshold: {behavior.get('confidence_threshold', 0.75)}")
    parts.append(f"- escalation threshold: {behavior.get('escalation_threshold', 'high')}")
    if behavior.get("confirm_before_action"):
        parts.append("- confirm before action: state what you will do before doing it.")
    if behavior.get("auto_escalate_on_error"):
        parts.append("- auto-escalate on error: escalate to a human if an error occurs.")
    if tools_cfg:
        parts.append(f"- available tools: {', '.join(t['name'] for t in tools_cfg)}")
    return "\n".join(parts)


def _execute_agent(agent_id: str, claim: str) -> dict:
    """Execute an agent using its CURRENT Cortex config, and record the run.

    Shared by the /api/agents/{id}/run route and the /webhooks/{id}/{event}
    trigger so both paths execute identically — one implementation, one set of
    outcomes, one place a run gets written. Behaviour depends on the agent's
    endpoint type:
        embedded  run through the active LLM provider with the agent's tools
        rest      POST to the agent's configured endpoint URL
        webhook   return instructions for the caller to send data
    """
    db = SessionLocal()
    try:
        a_row = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a_row:
            raise HTTPException(404, "agent not found")
        if not a_row.live:
            raise HTTPException(400, "this agent is not marked as live")
        a = _agent_to_dict(a_row)
        a_version = a_row.version or 1
    finally:
        db.close()

    run_start = datetime.now(timezone.utc).isoformat()
    log_event(agent_id, "run.start", {"input": claim[:200], "config_version": a_version})

    cfg = a["config"]
    endpoint = a.get("endpoint", {})
    ep_type = endpoint.get("type", "embedded")

    # ── Webhook endpoint: return instructions ──
    if ep_type == "webhook":
        log_event(agent_id, "run.webhook_info", {"endpoint": endpoint.get("url", "")})
        webhook_now = datetime.now(timezone.utc).isoformat()
        rec = {
            "claim": claim, "outcome": "WEBHOOK_PENDING", "published": False,
            "steps_used": 0, "config_version": a_version,
            "provider": "webhook", "model": "",
            "trace": [{"kind": "info", "text": f"Send data to webhook: {endpoint.get('url', 'not configured')}"}],
            "started_at": run_start, "finished_at": webhook_now,
            "detail": {"summary": f"Webhook agent — POST your data to the configured endpoint or use /webhooks/{agent_id}/{{event_type}}", "reason": "", "citations": [], "route_to": None}
        }
        _save_run_to_db(agent_id, rec)
        return {"ok": True, "run": rec}

    # ── REST endpoint: proxy to external URL ──
    if ep_type == "rest" and endpoint.get("url"):
        import httpx
        try:
            with httpx.Client(timeout=cfg.get("execution", {}).get("timeout_seconds", 120)) as client:
                resp = client.post(endpoint["url"], json={"claim": claim, "config": cfg})
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            log_event(agent_id, "run.error", {"message": str(e)})
            rest_err_end = datetime.now(timezone.utc).isoformat()
            rec = {
                "claim": claim, "outcome": "ERROR", "published": False,
                "steps_used": 0, "config_version": a_version,
                "provider": "rest", "model": "",
                "trace": [{"kind": "error", "text": str(e)}],
                "started_at": run_start, "finished_at": rest_err_end,
                "detail": {"summary": "", "reason": str(e), "citations": [], "route_to": None}
            }
            _save_run_to_db(agent_id, rec)
            return {"ok": False, "error": str(e), "run": rec}

        rest_end = datetime.now(timezone.utc).isoformat()
        rec = {
            "claim": claim, "outcome": "COMPLETED", "published": True,
            "steps_used": 1, "config_version": a_version,
            "provider": "rest", "model": "",
            "trace": [{"kind": "rest_call", "url": endpoint["url"], "status": "ok"}],
            "started_at": run_start, "finished_at": rest_end,
            "detail": {"summary": json.dumps(result)[:1200], "reason": "", "citations": [], "route_to": None}
        }
        _save_run_to_db(agent_id, rec)
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

    system = build_system_prompt(a)

    tools = _tools_for(tools_cfg)

    res = providers_mod.run_tool_loop(
        provider=provider, api_key=key,
        model=model_cfg.get("model_name") or get_model(provider),
        system=system, tools=tools, user_message=claim,
        process_tool_call=_run_tool,
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
        "claim": claim, "outcome": outcome, "published": published,
        "steps_used": res["steps_used"], "config_version": a_version,
        "provider": provider,
        "model": model_cfg.get("model_name") or get_model(provider),
        "trace": trace, "started_at": run_start, "finished_at": run_end,
        "input_tokens": res.get("input_tokens", 0),
        "output_tokens": res.get("output_tokens", 0),
        "total_tokens": res.get("total_tokens", 0),
        "detail": {"summary": (res.get("final_text") or "")[:1200], "reason": res.get("error", ""), "citations": [], "route_to": None}
    }
    _save_run_to_db(agent_id, rec)

    # Update agent metrics
    db = SessionLocal()
    try:
        a_row = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if a_row:
            all_runs = db.query(RunModel).filter(RunModel.agent_id == agent_id).order_by(RunModel.started_at.desc()).limit(12).all()
            done = [r for r in all_runs if r.outcome != "ERROR"]
            if done:
                a_row.containment = round(100 * sum(1 for r in done if r.published) / len(done))
                a_row.escalation = round(100 * sum(1 for r in done if r.outcome == "ESCALATED") / len(done))
                a_row.resolution = round(100 * len(done) / len(all_runs)) if all_runs else 0
            db.commit()
    finally:
        db.close()

    if res["ok"]:
        return {"ok": True, "run": rec}
    return {"ok": False, "error": res.get("error", "run failed"), "run": rec}


@app.post("/api/agents/{agent_id}/run")
def run_live_agent(agent_id: str, body: RunIn, request: Request):
    """Run an agent once with the supplied input."""
    sess = _get_session(request)
    if sess and not _check_scope(sess, "agents:run"):
        raise HTTPException(403, "API key missing required scope: agents:run")
    return _execute_agent(agent_id, body.claim)


class ReleaseIn(BaseModel):
    environment: str
    version: int | None = None      # defaults to the agent's current version
    note: str = ""


@app.get("/api/agents/{agent_id}/releases")
def list_releases(agent_id: str, request: Request):
    """Which config version each of the client's environments is running."""
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        rows = db.query(AgentRelease).filter(AgentRelease.agent_id == agent_id).all()
        return {"agent_id": agent_id, "releases": [r.to_dict() for r in rows]}
    finally:
        db.close()


@app.post("/api/agents/{agent_id}/release")
def release_agent(agent_id: str, body: ReleaseIn, request: Request):
    """Mark a config version live in one of the client's environments.

    This does not push anything. It records the pointer; the environment picks
    it up on its next fetch of /api/agents/{id}/config.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    env = (body.environment or "").strip().lower()
    if not env:
        raise HTTPException(400, "environment is required")

    db = SessionLocal()
    try:
        a_row = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a_row:
            raise HTTPException(404, "agent not found")

        version = body.version or a_row.version or 1
        # Only release a version that actually exists — releasing a version
        # with no snapshot behind it would leave the environment fetching a
        # config nobody can reconstruct.
        snap = (db.query(AgentVersion)
                .filter(AgentVersion.agent_id == agent_id,
                        AgentVersion.version == version).first())
        if not snap:
            raise HTTPException(400, f"agent has no recorded v{version} to release")

        rec = (db.query(AgentRelease)
               .filter(AgentRelease.agent_id == agent_id,
                       AgentRelease.environment == env).first())
        previous = rec.active_version if rec else None
        if not rec:
            rec = AgentRelease(agent_id=agent_id, owner_id=sess.get("user_id"),
                               environment=env, active_version=version)
            db.add(rec)
        else:
            rec.active_version = version
        rec.released_by = sess.get("user_id")
        rec.released_by_email = sess.get("email", "")
        rec.note = (body.note or "")[:512]
        rec.updated_at = utcnow()
        db.commit()

        log_event(agent_id, "release", {"environment": env, "version": version,
                                        "previous": previous})
        return {"ok": True, "environment": env, "active_version": version,
                "previous_version": previous, "release": rec.to_dict()}
    finally:
        db.close()


@app.get("/api/agents/{agent_id}/config")
def fetch_released_config(agent_id: str, request: Request, env: str = "production"):
    """The config an environment should be running. Called by the client's agent.

    Authenticate with an API key carrying agents:read. Returns the snapshot at
    the released version, not the agent's current working config — that is the
    whole point of releasing.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    if not _check_scope(sess, "agents:read"):
        raise HTTPException(403, "API key missing required scope: agents:read")

    env = (env or "production").strip().lower()
    db = SessionLocal()
    try:
        rec = (db.query(AgentRelease)
               .filter(AgentRelease.agent_id == agent_id,
                       AgentRelease.environment == env).first())
        if not rec:
            raise HTTPException(404, f"no version released to '{env}' for this agent")

        snap = (db.query(AgentVersion)
                .filter(AgentVersion.agent_id == agent_id,
                        AgentVersion.version == rec.active_version).first())
        if not snap:
            raise HTTPException(500,
                f"v{rec.active_version} is released to '{env}' but its snapshot is missing")

        # Record the pickup so "released" and "running" stay distinguishable.
        rec.last_fetched_at = utcnow()
        rec.fetch_count = (rec.fetch_count or 0) + 1
        db.commit()

        return {"agent_id": agent_id, "environment": env,
                "version": rec.active_version,
                "config": snap.config or {},
                "released_at": rec.updated_at.isoformat() if rec.updated_at else None}
    finally:
        db.close()


class SystemPromptIn(BaseModel):
    system_prompt: str = ""
    note: str = ""


@app.get("/api/agents/{agent_id}/system-prompt")
def get_system_prompt(agent_id: str, request: Request):
    """The authored prompt, and the full text the model is actually given.

    The preview is built with the same function the run path uses, so what is
    shown here is not an approximation of the prompt — it is the prompt.
    """
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        d = _agent_to_dict(a)
        cfg = a.config or {}
        return {
            "agent_id": agent_id,
            "system_prompt": cfg.get("system_prompt", "") or "",
            "version": a.version or 1,
            "assembled": build_system_prompt(d),
            "assembled_continuous": build_system_prompt(d, continuous=True),
        }
    finally:
        db.close()


@app.post("/api/agents/{agent_id}/system-prompt")
def set_system_prompt(agent_id: str, body: SystemPromptIn, request: Request):
    """Change an agent's system prompt.

    The prompt lives inside config, not beside it, so it snapshots, diffs,
    rolls back and attributes runs exactly like every other config change —
    and a prompt edit that made things worse shows up in version performance
    the same way a temperature change does.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        prev_config = dict(a.config or {})
        if (prev_config.get("system_prompt") or "") == body.system_prompt:
            # No change is not a new version. Saying so beats silently
            # inflating the version history with identical snapshots.
            return {"ok": True, "unchanged": True, "version": a.version or 1,
                    "assembled": build_system_prompt(_agent_to_dict(a))}
        cfg = dict(prev_config)
        cfg["system_prompt"] = body.system_prompt
        a.config = cfg
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(a, "config")
        db.commit()

        snap = snapshot_agent_version(
            db, a, changed_by=sess.get("user_id"),
            changer_email=sess.get("email", ""),
            change_type="update", prev_config=prev_config,
            change_summary=body.note or "System prompt updated",
        )
        log_event(agent_id, "system_prompt.changed", {"version": snap.version})
        return {"ok": True, "unchanged": False, "version": snap.version,
                "assembled": build_system_prompt(_agent_to_dict(a))}
    finally:
        db.close()


LIFECYCLE_STAGES = ["draft", "active", "deprecated", "retired"]


class LifecycleIn(BaseModel):
    lifecycle: str
    note: str = ""


class OwnershipIn(BaseModel):
    owner_id: str | None = None
    contact: str | None = None
    account: str | None = None


@app.post("/api/agents/{agent_id}/lifecycle")
def set_lifecycle(agent_id: str, body: LifecycleIn, request: Request):
    """Move an agent through its lifecycle.

    Separate from run status on purpose. An agent can be deprecated and still
    running — that is the state that lets a team say "stop building on this"
    without anyone having to be brave enough to switch it off yet.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    stage = (body.lifecycle or "").strip().lower()
    if stage not in LIFECYCLE_STAGES:
        raise HTTPException(400, f"lifecycle must be one of: {', '.join(LIFECYCLE_STAGES)}")

    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        previous = a.lifecycle or "active"

        # Retiring something still executing is almost always a mistake — say so
        # rather than silently stopping it on the user's behalf.
        if stage == "retired" and (a.status == "running" or a.live):
            raise HTTPException(400,
                "this agent is still running — stop it before retiring, "
                "or mark it deprecated instead")

        a.lifecycle = stage
        a.lifecycle_note = (body.note or "")[:512]
        a.lifecycle_changed_at = utcnow()
        db.commit()

        log_audit(db, agent_id=agent_id, event="lifecycle.changed",
                  data={"from": previous, "to": stage, "note": a.lifecycle_note},
                  user_id=sess.get("user_id"))
        # persist=False: log_audit above already wrote the durable row, with
        # the note and the user attached. This call is only for the live feed.
        log_event(agent_id, "lifecycle.changed", {"from": previous, "to": stage},
                  persist=False)
        return {"ok": True, "agent_id": agent_id, "from": previous, "to": stage,
                "note": a.lifecycle_note}
    finally:
        db.close()


@app.post("/api/agents/{agent_id}/ownership")
def set_ownership(agent_id: str, body: OwnershipIn, request: Request):
    """Set who is responsible for an agent and where to reach them."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        before = {"owner_id": a.owner_id, "contact": a.contact, "account": a.account}
        if body.owner_id is not None:
            if body.owner_id and not db.query(User).filter(User.id == body.owner_id).first():
                raise HTTPException(400, "no such user")
            a.owner_id = body.owner_id or None
        if body.contact is not None:
            a.contact = (body.contact or "")[:255]
        if body.account is not None:
            a.account = (body.account or "")[:128]
        db.commit()

        log_audit(db, agent_id=agent_id, event="ownership.changed",
                  data={"before": before,
                        "after": {"owner_id": a.owner_id, "contact": a.contact,
                                  "account": a.account}},
                  user_id=sess.get("user_id"))
        return {"ok": True, "agent_id": agent_id, "owner_id": a.owner_id,
                "contact": a.contact or "", "account": a.account or ""}
    finally:
        db.close()


@app.get("/api/agents/{agent_id}/ownership")
def get_ownership(agent_id: str, request: Request):
    """Who owns this agent, where to reach them, and its lifecycle stage."""
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        owner = db.query(User).filter(User.id == a.owner_id).first() if a.owner_id else None
        users = [{"id": u.id, "name": u.name or u.email, "email": u.email}
                 for u in db.query(User).filter(User.is_active == True).all()]  # noqa: E712
        return {
            "agent_id": agent_id,
            "owner_id": a.owner_id or "",
            "owner_name": (owner.name or owner.email) if owner else "",
            "owner_email": owner.email if owner else "",
            "contact": a.contact or "",
            "account": a.account or "",
            "lifecycle": a.lifecycle or "active",
            "lifecycle_note": a.lifecycle_note or "",
            "lifecycle_changed_at": a.lifecycle_changed_at.isoformat() if a.lifecycle_changed_at else None,
            "assignable_users": users,
        }
    finally:
        db.close()


@app.get("/api/agents/{agent_id}/runs")
def agent_runs(agent_id: str):
    db = SessionLocal()
    try:
        runs = db.query(RunModel).filter(RunModel.agent_id == agent_id).order_by(RunModel.started_at.desc()).limit(20).all()
        return {"agent_id": agent_id, "runs": [
            {"claim": r.claim, "outcome": r.outcome, "published": r.published,
             "steps_used": r.steps_used, "config_version": r.config_version,
             "provider": r.provider, "model": r.model,
             "trace": r.trace or [], "started_at": r.started_at.isoformat() if r.started_at else "",
             "finished_at": r.finished_at.isoformat() if r.finished_at else "",
             "detail": r.detail or {}}
            for r in runs
        ]}
    finally:
        db.close()


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
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
    finally:
        db.close()
    return diagnose_agent(agent_id)


@app.get("/api/metrics/portfolio")
def portfolio_metrics():
    db = SessionLocal()
    try:
        agents = db.query(AgentModel).all()
        running = [a for a in agents if a.status == "running"]
        n = len(running) or 1
        return {
            "agents_active": len(running),
            "avg_containment": round(sum(a.containment or 0 for a in running) / n, 1),
            "avg_resolution": round(sum(a.resolution or 0 for a in running) / n, 1),
            "avg_escalation": round(sum(a.escalation or 0 for a in running) / n, 1),
            "total_clinical_flags": 0,
            "health_score": round(
                sum(a.containment or 0 for a in running) / n * 0.4
                + sum(a.resolution or 0 for a in running) / n * 0.4
                + (100 - sum(a.escalation or 0 for a in running) / n) * 0.2, 0),
        }
    finally:
        db.close()

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
    db = SessionLocal()
    try:
        out = []
        for d in DIAGNOSTICS:
            a = db.query(AgentModel).filter(AgentModel.id == d["agent_id"]).first()
            out.append({**d, "status": a.status if a else None, "clinical_flags": 0})
        return {"cases": out}
    finally:
        db.close()

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
            if p in providers_mod.ALL_PROVIDERS and m:
                SETTINGS["models"][p] = m.strip()
    _save_settings(SETTINGS)
    return get_settings()


@app.post("/api/settings/test/{provider}")
def test_provider(provider: str, request: Request, body: dict = None):
    if provider not in providers_mod.ALL_PROVIDERS:
        raise HTTPException(400, "unknown provider")
    # Use key from request body if provided (for testing before saving), else fall back to stored
    key = (body or {}).get("api_key", "").strip() if body else ""
    if not key:
        key = get_key(provider)
    if not key:
        return {"ok": False, "message": "No key set for this provider."}
    return providers_mod.test_connection(provider, key, get_model(provider))


# ──────────────────────────────────────────────── automation endpoints
@app.get("/api/agents/{agent_id}/automation")
def get_automation(agent_id: str):
    db = SessionLocal()
    try:
        if not db.query(AgentModel).filter(AgentModel.id == agent_id).first():
            raise HTTPException(404, "agent not found")
    finally:
        db.close()
    return automation_mod.get_agent_automation(agent_id)


@app.post("/api/agents/{agent_id}/automation")
def update_automation(agent_id: str, updates: dict):
    db = SessionLocal()
    try:
        if not db.query(AgentModel).filter(AgentModel.id == agent_id).first():
            raise HTTPException(404, "agent not found")
    finally:
        db.close()
    result = automation_mod.update_agent_automation(agent_id, updates)
    return {"ok": True, "automation": result}


# ── Analytics API ───────────────────────────────────────────────────

@app.get("/api/analytics")
def analytics_dashboard(request: Request, days: int = 30):
    """Aggregated usage analytics: runs over time, token usage, success rates, top agents."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    from datetime import timedelta
    db = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(days=days)
        runs = db.query(RunModel).filter(RunModel.started_at >= cutoff).all()
        agents = db.query(AgentModel).all()

        # ── Runs per day ────────────────────────────────
        runs_by_day = {}
        tokens_by_day = {}
        for r in runs:
            day = r.started_at.strftime("%Y-%m-%d") if r.started_at else "unknown"
            runs_by_day[day] = runs_by_day.get(day, 0) + 1
            tokens_by_day[day] = tokens_by_day.get(day, 0) + (r.total_tokens or 0)

        # Sort by date
        sorted_days = sorted(runs_by_day.keys())
        runs_series = [{"date": d, "count": runs_by_day[d]} for d in sorted_days]
        tokens_series = [{"date": d, "tokens": tokens_by_day[d]} for d in sorted_days]

        # ── Outcome breakdown ───────────────────────────
        outcomes = {}
        for r in runs:
            o = r.outcome or "UNKNOWN"
            outcomes[o] = outcomes.get(o, 0) + 1

        # ── Top agents by run count ─────────────────────
        agent_runs = {}
        agent_tokens = {}
        agent_names = {a.id: a.name for a in agents}
        for r in runs:
            aid = r.agent_id
            agent_runs[aid] = agent_runs.get(aid, 0) + 1
            agent_tokens[aid] = agent_tokens.get(aid, 0) + (r.total_tokens or 0)

        top_agents = sorted(agent_runs.items(), key=lambda x: x[1], reverse=True)[:10]
        top_agents_list = [
            {"id": aid, "name": agent_names.get(aid, aid), "runs": cnt,
             "tokens": agent_tokens.get(aid, 0)}
            for aid, cnt in top_agents
        ]

        # ── Provider breakdown ──────────────────────────
        providers = {}
        models = {}
        for r in runs:
            p = r.provider or "unknown"
            m = r.model or "unknown"
            providers[p] = providers.get(p, 0) + 1
            models[m] = models.get(m, 0) + 1

        # ── Summary stats ───────────────────────────────
        total_runs = len(runs)
        total_tokens = sum(r.total_tokens or 0 for r in runs)
        total_input = sum(r.input_tokens or 0 for r in runs)
        total_output = sum(r.output_tokens or 0 for r in runs)
        avg_tokens = round(total_tokens / total_runs) if total_runs else 0
        completed = sum(1 for r in runs if r.outcome == "COMPLETED")
        escalated = sum(1 for r in runs if r.outcome == "ESCALATED")
        errored = sum(1 for r in runs if r.outcome == "ERROR")
        success_rate = round(completed / total_runs * 100, 1) if total_runs else 0

        # ── Avg latency (runs with both timestamps) ────
        latencies = []
        for r in runs:
            if r.started_at and r.finished_at:
                dt = (r.finished_at - r.started_at).total_seconds()
                if dt >= 0:
                    latencies.append(dt)
        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0
        p95_latency = round(sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0, 2)

        return {
            "period_days": days,
            "summary": {
                "total_runs": total_runs,
                "total_tokens": total_tokens,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "avg_tokens_per_run": avg_tokens,
                "success_rate": success_rate,
                "completed": completed,
                "escalated": escalated,
                "errored": errored,
                "avg_latency_s": avg_latency,
                "p95_latency_s": p95_latency,
                "active_agents": len([a for a in agents if a.status == "running"]),
                "total_agents": len(agents),
            },
            "runs_by_day": runs_series,
            "tokens_by_day": tokens_series,
            "outcomes": outcomes,
            "top_agents": top_agents_list,
            "providers": providers,
            "models": models,
        }
    finally:
        db.close()


# ── CAR (Cortex Adaptive Runtime) API ──────────────────────────────

@app.get("/api/car/health")
def car_health(request: Request):
    """Platform-wide health from the Adaptive Runtime."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.health()


@app.get("/api/car/fingerprint/{agent_id}")
def car_fingerprint(agent_id: str, request: Request):
    """Behavioral fingerprint for one agent."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.fingerprint(agent_id)


@app.get("/api/car/fingerprints")
def car_fingerprints_all(request: Request):
    """All agent fingerprints."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return {aid: fp.snapshot() for aid, fp in _car_engine.fingerprints.items()}


@app.post("/api/car/route")
def car_route(request: Request, body: dict):
    """Adaptive routing: pick optimal provider/model for a task."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    agent_id = body.get("agent_id", "unknown")
    task_type = body.get("task_type", "general")
    providers = body.get("providers", [])
    optimize = body.get("optimize_for", "balanced")
    if not providers:
        raise HTTPException(400, "providers list required")
    return _car_engine.route(agent_id, task_type, providers, optimize)


@app.post("/api/car/predict")
def car_predict(request: Request, body: dict):
    """Pre-execution run prediction."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.predict(
        agent_id=body.get("agent_id", "unknown"),
        provider=body.get("provider", "unknown"),
        model=body.get("model", "unknown"),
        prompt_tokens=body.get("prompt_tokens", 500),
        tool_count=body.get("tool_count", 0),
        system_tokens=body.get("system_tokens", 0),
        task_type=body.get("task_type", "general"),
    )


@app.get("/api/car/leaderboard/{task_type}")
def car_leaderboard(task_type: str, request: Request):
    """Provider/model leaderboard for a task type."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.routing_leaderboard(task_type)


@app.get("/api/car/pressure")
def car_pressure(request: Request):
    """Which metric dimension drives the most drift fleet-wide."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.pressure()


@app.get("/api/car/audit")
def car_audit(request: Request, limit: int = 50, agent_id: str = None):
    """Recent CAR audit trail."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.get_audit_log(limit=limit, agent_id=agent_id)


@app.get("/api/car/state")
def car_state(request: Request):
    """Full CAR engine state snapshot."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.state_snapshot()


@app.post("/api/car/policy")
def car_update_policy(request: Request, body: dict):
    """Update CAR prediction policy (governed, versioned)."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return _car_engine.update_prediction_policy(
        weights=body.get("weights"),
        thresholds=body.get("thresholds"),
        by=sess.get("email", "admin"),
    )


@app.post("/webhooks/{agent_id}/{event_type}")
def webhook_trigger(agent_id: str, event_type: str):
    """Webhook endpoint for event-triggered agent execution."""
    db = SessionLocal()
    try:
        if not db.query(AgentModel).filter(AgentModel.id == agent_id).first():
            raise HTTPException(404, "agent not found")
    finally:
        db.close()

    # check_event_trigger returns a plain bool — it is false both when automation
    # is disabled for the agent and when this event type is not subscribed, so
    # say which rather than reporting a bare refusal.
    if not automation_mod.check_event_trigger(agent_id, event_type):
        auto = automation_mod.get_agent_automation(agent_id) or {}
        reason = ("automation is not enabled for this agent"
                  if not auto.get("enabled")
                  else f"'{event_type}' is not in this agent's event triggers")
        log_event(agent_id, "webhook.ignored", {"event": event_type, "reason": reason})
        return {"ok": False, "executed": False, "agent_id": agent_id,
                "event": event_type, "reason": reason}

    # Run the agent for real, through the same path the API route uses. The
    # caller is told what actually happened rather than that it was received.
    log_event(agent_id, "webhook.trigger", {"event": event_type})
    try:
        result = _execute_agent(agent_id, f"[webhook:{event_type}] Event received.")
    except HTTPException as e:
        log_event(agent_id, "webhook.error", {"event": event_type, "detail": str(e.detail)})
        return {"ok": False, "executed": False, "agent_id": agent_id,
                "event": event_type, "error": e.detail}
    except Exception as e:
        log_event(agent_id, "webhook.error", {"event": event_type, "error": str(e)[:500]})
        return {"ok": False, "executed": False, "agent_id": agent_id,
                "event": event_type, "error": str(e)[:500]}

    # _execute_agent returns a "run" only when one actually happened. Without
    # one it could not start at all (no provider key, for instance) — which is
    # not an execution, and must not be reported as one.
    run = result.get("run")
    if run is None:
        log_event(agent_id, "webhook.not_executed",
                  {"event": event_type, "error": result.get("error", "")})
        return {"ok": False, "executed": False, "agent_id": agent_id,
                "event": event_type,
                "error": result.get("error", "agent could not be executed")}

    automation_mod.record_run(agent_id)
    return {"ok": bool(result.get("ok")), "executed": True,
            "agent_id": agent_id, "event": event_type,
            "run_id": run.get("id", ""),
            "outcome": run.get("outcome", ""),
            "run_summary": (run.get("detail", {}) or {}).get("summary", "")[:500]}


# ═══════════════════════════════════════════════════════════════
#  OBSERVABILITY  (metrics · traces · logs · alerts · SLOs · health)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/observability/summary")
def obs_summary(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return obs.dashboard_summary()

@app.get("/api/observability/metrics")
def obs_metrics(request: Request, name: str = None, window: int = 300, last_n: int = 24):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"series": obs.metrics.query(name=name, window=window, last_n=last_n),
            "current_gauges": obs.metrics.current_gauges()}

@app.get("/api/observability/traces")
def obs_traces(request: Request, has_errors: bool = None, limit: int = 50):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"traces": obs.traces.list_traces(has_errors=has_errors, limit=limit),
            "stats": obs.traces.stats()}

@app.get("/api/observability/traces/{trace_id}")
def obs_trace_detail(trace_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    trace = obs.traces.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "trace not found")
    return trace

@app.get("/api/observability/service-map")
def obs_service_map(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return obs.traces.service_map()

@app.get("/api/observability/logs")
def obs_logs(request: Request, q: str = None, level: str = None, source: str = None,
             trace_id: str = None, limit: int = 100, offset: int = 0):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return obs.logs.search(query=q, level=level, source=source, trace_id=trace_id,
                           limit=limit, offset=offset)

@app.get("/api/observability/logs/errors")
def obs_error_groups(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"error_groups": obs.logs.error_groups(),
            "throughput": obs.logs.throughput()}

@app.get("/api/observability/alerts")
def obs_alerts(request: Request, status: str = None, severity: str = None):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"alerts": obs.alerts.list_alerts(status=status, severity=severity),
            "rules": obs.alerts.list_rules(), "stats": obs.alerts.stats()}

class AlertRuleIn(BaseModel):
    name: str
    metric: str
    condition: str = "gt"
    threshold: float = 0.0
    window_seconds: int = 300
    severity: str = "warning"
    description: str = ""
    notification_channels: list = []
    min_breaches: int = 1

@app.post("/api/observability/alerts/rules")
def obs_create_rule(body: AlertRuleIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return obs.alerts.create_rule(
        name=body.name, metric=body.metric, condition=body.condition,
        threshold=body.threshold, window_seconds=body.window_seconds,
        severity=body.severity, description=body.description,
        notification_channels=body.notification_channels,
        min_breaches=body.min_breaches, created_by=sess.get("user_id"))

@app.delete("/api/observability/alerts/rules/{rule_id}")
def obs_delete_rule(rule_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return obs.alerts.delete_rule(rule_id)

@app.post("/api/observability/alerts/{alert_id}/acknowledge")
def obs_ack_alert(alert_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return obs.alerts.acknowledge(alert_id, sess.get("user_id", "unknown"))

@app.post("/api/observability/alerts/{alert_id}/resolve")
def obs_resolve_alert(alert_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return obs.alerts.resolve(alert_id)

@app.post("/api/observability/alerts/evaluate")
def obs_evaluate(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"fired": obs.check_alerts()}

@app.get("/api/observability/slos")
def obs_slos(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"slos": obs.slos.list_slos(), "stats": obs.slos.stats(),
            "burn_rate_alerts": obs.slos.burn_rate_alerts()}

@app.get("/api/observability/health")
def obs_health(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    # Score every active agent from its recent run stats
    db = SessionLocal()
    try:
        agents = db.query(AgentModel).filter(
            (AgentModel.is_deleted == False) | (AgentModel.is_deleted == None)  # noqa: E711,E712
        ).all()
        for a in agents:
            runs = db.query(RunModel).filter(RunModel.agent_id == a.id).order_by(
                RunModel.started_at.desc()).limit(50).all()
            total = len(runs)
            success = sum(1 for r in runs if r.outcome == "COMPLETED")
            latencies = []
            for r in runs:
                if r.started_at and r.finished_at:
                    latencies.append((r.finished_at - r.started_at).total_seconds())
            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
            recent_errors = sum(1 for r in runs[:10] if r.outcome == "ERROR")
            past_errors = sum(1 for r in runs[10:30] if r.outcome == "ERROR")
            obs.health.score(a.id, {
                "total_runs": total, "success_runs": success, "p95_latency": p95,
                "recent_errors": recent_errors, "past_errors": past_errors,
                "runs_per_hour": 1, "expected_runs_per_hour": 1, "uptime_pct": 100,
            })
    finally:
        db.close()
    return obs.health.fleet_health()


# ═══════════════════════════════════════════════════════════════
#  USAGE & COST
# ═══════════════════════════════════════════════════════════════

@app.get("/api/usage/dashboard")
def usage_dashboard(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return usage_tracker.dashboard()

@app.get("/api/usage/agent/{agent_id}")
def usage_agent(agent_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"agent_id": agent_id, "usage": usage_tracker.agent_cost(agent_id),
            "budget": usage_tracker.budget_status(agent_id),
            "recent": usage_tracker.recent_records(agent_id=agent_id, limit=50)}

@app.get("/api/usage/pricing")
def usage_pricing(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"pricing": usage_tracker.calculator.all_prices()}

class BudgetIn(BaseModel):
    scope: str = "_fleet"
    daily_limit: float = None
    monthly_limit: float = None

@app.post("/api/usage/budget")
def usage_set_budget(body: BudgetIn, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return usage_tracker.set_budget(body.scope, body.daily_limit, body.monthly_limit)

class PriceIn(BaseModel):
    model: str
    input_price: float
    output_price: float

@app.post("/api/usage/pricing")
def usage_set_price(body: PriceIn, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    usage_tracker.calculator.set_price(body.model, body.input_price, body.output_price)
    return {"ok": True, "model": body.model}


# ═══════════════════════════════════════════════════════════════
#  TEAMS / WORKSPACES  (multi-tenant)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/teams/roles")
def teams_roles(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"roles": team_manager.roles_catalog()}

@app.get("/api/teams")
def teams_list(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return {"workspaces": team_manager.list_workspaces_for_user(db, sess["user_id"])}
    finally:
        db.close()

class WorkspaceIn(BaseModel):
    name: str
    plan: str = "free"

@app.post("/api/teams")
def teams_create(body: WorkspaceIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return team_manager.create_workspace(db, body.name, sess["user_id"], body.plan)
    finally:
        db.close()

@app.get("/api/teams/{workspace_id}/members")
def teams_members(workspace_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        if not team_manager.get_member_role(db, workspace_id, sess["user_id"]):
            raise HTTPException(403, "not a member of this workspace")
        return {"members": team_manager.list_members(db, workspace_id),
                "invites": team_manager.list_invites(db, workspace_id)}
    finally:
        db.close()

class InviteIn(BaseModel):
    email: str
    role: str = "operator"

@app.post("/api/teams/{workspace_id}/invite")
def teams_invite(workspace_id: str, body: InviteIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        if not team_manager.can(db, workspace_id, sess["user_id"], "members:invite"):
            raise HTTPException(403, "insufficient permissions")
        return team_manager.invite(db, workspace_id, body.email, body.role, sess["user_id"])
    finally:
        db.close()

class RoleUpdateIn(BaseModel):
    role: str

@app.post("/api/teams/{workspace_id}/members/{user_id}/role")
def teams_update_role(workspace_id: str, user_id: str, body: RoleUpdateIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        if not team_manager.can(db, workspace_id, sess["user_id"], "members:invite"):
            raise HTTPException(403, "insufficient permissions")
        return team_manager.update_member_role(db, workspace_id, user_id, body.role, sess["user_id"])
    finally:
        db.close()

@app.delete("/api/teams/{workspace_id}/members/{user_id}")
def teams_remove_member(workspace_id: str, user_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        if not team_manager.can(db, workspace_id, sess["user_id"], "members:remove"):
            raise HTTPException(403, "insufficient permissions")
        return team_manager.remove_member(db, workspace_id, user_id)
    finally:
        db.close()

class AcceptInviteIn(BaseModel):
    token: str

@app.post("/api/teams/accept-invite")
def teams_accept(body: AcceptInviteIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return team_manager.accept_invite(db, body.token, sess["user_id"])
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  VERSIONS & RECOVERY  (recycle bin, rollback, rebuild)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/agents/{agent_id}/versions")
def agent_versions(agent_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        rows = list_agent_versions(db, agent_id)
        return {"agent_id": agent_id,
                "versions": [{
                    "version": r.version, "name": r.name,
                    "change_type": r.change_type, "change_summary": r.change_summary,
                    "changed_by": r.changed_by, "changer_email": r.changer_email,
                    "diff": r.diff, "record_hash": r.record_hash[:12],
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                } for r in rows],
                "integrity": verify_version_chain(db, agent_id)}
    finally:
        db.close()

@app.post("/api/agents/{agent_id}/versions/{version}/restore")
def agent_restore_version(agent_id: str, version: int, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        result = restore_agent_version(db, a, version,
                                       restored_by=sess.get("user_id"),
                                       changer_email=sess.get("email", ""))
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "restore failed"))
        log_event(agent_id, "agent.version_restored",
                  {"from_version": version, "by": sess.get("email", "")})
        return result
    finally:
        db.close()

@app.get("/api/recycle-bin")
def recycle_bin(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        owner = None if sess.get("is_admin") else sess.get("user_id")
        agents = list_recycle_bin(db, owner_id=owner)
        return {"deleted_agents": [{
            "id": a.id, "name": a.name, "description": a.description or "",
            "deleted_at": a.deleted_at.isoformat() if a.deleted_at else None,
            "deleted_by": a.deleted_by,
            "purge_after": a.purge_after.isoformat() if a.purge_after else None,
            "version": a.version,
        } for a in agents], "retention_days": RECYCLE_BIN_RETENTION_DAYS}
    finally:
        db.close()

@app.post("/api/recycle-bin/{agent_id}/restore")
def recycle_restore(agent_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        a = db.query(AgentModel).filter(AgentModel.id == agent_id).first()
        if not a:
            raise HTTPException(404, "agent not found")
        result = restore_agent(db, a, restored_by=sess.get("user_id"))
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "restore failed"))
        return result
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  INTEGRATIONS  (live control panel)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/integrations")
def integrations_list(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"integrations": integration_manager.list_integrations(),
            "available_types": integration_manager.available_types()}

class IntegrationIn(BaseModel):
    type: str
    name: str = ""
    config: dict = {}
    agent_id: str = None

@app.post("/api/integrations")
def integrations_register(body: IntegrationIn, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    if body.agent_id:
        return integration_manager.register_for_agent(body.agent_id, body.type, body.config, body.name)
    return integration_manager.register_global(body.type, body.config, body.name)

class IntegrationActionIn(BaseModel):
    integration: str
    action: str
    params: dict = {}
    agent_id: str = None
    request_id: str = ""
    environment: str = "production"
    data_scope: str = ""
    target_system: str = ""
    evidence: list = []
    financial_impact: float | None = None
    actions_last_hour: int = 0
    approval_id: str | None = None

@app.post("/api/integrations/execute")
def integrations_execute(body: IntegrationActionIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    authorization = None
    if body.agent_id:
        proposed = body.model_dump()
        proposed["action"] = f"integration.{body.integration}.{body.action}"
        proposed["target_system"] = body.target_system or body.integration
        authorization = _authorize_action(body.agent_id, proposed, sess)
        if authorization["configured"] and authorization["decision"] not in ("ALLOW", "ALLOW_WITH_LIMITS"):
            attestation_id = _create_attestation(
                agent_id=body.agent_id, sess=sess, action=proposed["action"],
                action_input=json.dumps(body.params, default=str)[:500],
                action_result=authorization["decision"],
                action_summary="; ".join(authorization["reasons"]),
                policy_checked=True, policy_passed=False, policy_details=authorization,
                human_approval_required=authorization["decision"] == "HUMAN_REVIEW",
            )
            return {"ok": False, "executed": False, "authorization": authorization,
                    "attestation_id": attestation_id}
    result = integration_manager.execute(body.integration, body.action, body.params, body.agent_id)
    if body.agent_id and authorization and authorization["configured"]:
        attestation_id = _create_attestation(
            agent_id=body.agent_id, sess=sess,
            action=f"integration.{body.integration}.{body.action}",
            action_input=json.dumps(body.params, default=str)[:500],
            action_result="COMPLETED" if result.get("ok") else "ERROR",
            action_summary=json.dumps(result, default=str)[:500],
            policy_checked=True, policy_passed=True, policy_details=authorization,
            human_approval_granted=bool(body.approval_id),
        )
        result["authorization"] = authorization
        result["attestation_id"] = attestation_id
    return result

@app.delete("/api/integrations/{name}")
def integrations_remove(name: str, request: Request, agent_id: str = None):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return integration_manager.remove(name, agent_id)


# ═══════════════════════════════════════════════════════════════
#  AGENT-TO-AGENT  (message bus + workflows)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/comms/stats")
def comms_stats(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return message_bus.stats()

@app.get("/api/comms/history")
def comms_history(request: Request, agent_id: str = None, limit: int = 50):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"messages": message_bus.history(agent_id=agent_id, limit=limit)}

class MessageIn(BaseModel):
    from_agent: str
    to_agent: str
    payload: dict
    msg_type: str = "task"

@app.post("/api/comms/send")
def comms_send(body: MessageIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return message_bus.send(body.from_agent, body.to_agent, body.payload,
                            body.msg_type, user_id=sess.get("user_id"))

class WorkflowIn(BaseModel):
    name: str
    steps: list

@app.post("/api/comms/workflows")
def comms_create_workflow(body: WorkflowIn, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    return message_bus.create_workflow(body.name, body.steps, created_by=sess.get("user_id"))

@app.get("/api/comms/workflows")
def comms_list_workflows(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"workflows": message_bus.list_workflows()}

@app.post("/api/comms/workflows/{workflow_id}/run")
def comms_run_workflow(workflow_id: str, request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")

    wf_before = message_bus.get_workflow(workflow_id)
    if not wf_before or wf_before.get("error"):
        raise HTTPException(404, "workflow not found")

    # The bus advances one wave of ready steps per call and keeps everything in
    # memory. Mirror each wave into workflow_runs/workflow_step_runs so the
    # history survives a restart and Phase 2 has something to learn from.
    db = SessionLocal()
    wr = None
    try:
        try:
            wr = p2_recorder.begin(db, wf_before, owner_id=sess.get("user_id"),
                                   definition=message_bus.get_definition(workflow_id))
        except Exception as e:
            print(f"[phase2] could not open workflow record for {workflow_id}: {e}")

        # Execute exactly once. Everything below is recording, and a recording
        # failure must never re-run agents — that would double-charge and
        # repeat side effects.
        result = message_bus.run_workflow(workflow_id)

        if wr is not None:
            try:
                wf_after = result.get("workflow") or wf_before
                p2_recorder.record_wave(db, wr, result, wf_after)

                # Close the record out only once the DAG has nothing left.
                done = (result.get("remaining", 0) == 0
                        or wf_after.get("status") == "completed")
                if done:
                    p2_recorder.finalize(db, wr)

                result["workflow_run_id"] = wr.id
                result["recorded"] = True
            except Exception as e:
                print(f"[phase2] workflow recording failed for {workflow_id}: {e}")
                result["recorded"] = False
        else:
            result["recorded"] = False

        return result
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  PHASE 2  (relationships, workflow history, patterns)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/phase2/graph")
def p2_graph(request: Request, min_strength: int = 0):
    """The agent relationship graph — nodes + weighted edges."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return p2_discovery.graph(db, owner_id=sess.get("user_id"),
                                  min_strength=min_strength)
    finally:
        db.close()


@app.get("/api/phase2/agents/{agent_id}/relationships")
def p2_agent_relationships(agent_id: str, request: Request):
    """Edges touching one agent, split into incoming and outgoing."""
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return p2_discovery.for_agent(db, agent_id)
    finally:
        db.close()


@app.post("/api/phase2/discover")
def p2_discover(request: Request, source: str = "both"):
    """Run relationship discovery. source = config | runtime | both."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    if source not in ("config", "runtime", "both"):
        raise HTTPException(400, "source must be config, runtime, or both")
    uid = sess.get("user_id")
    db = SessionLocal()
    try:
        out = {}
        if source in ("config", "both"):
            out["config"] = p2_discovery.discover_from_config(db, owner_id=uid)
        if source in ("runtime", "both"):
            out["runtime"] = p2_discovery.discover_from_runtime(db, owner_id=uid)
        return out
    finally:
        db.close()


@app.get("/api/phase2/workflow-runs")
def p2_workflow_runs(request: Request, limit: int = 50):
    """Persisted multi-agent workflow history, newest first."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return {"runs": p2_recorder.history(db, owner_id=sess.get("user_id"),
                                            limit=min(limit, 200))}
    finally:
        db.close()


@app.get("/api/phase2/workflow-runs/{run_id}")
def p2_workflow_run_detail(run_id: str, request: Request):
    """One workflow run with its per-step breakdown."""
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        wr = db.query(phase2.WorkflowRun).filter(
            phase2.WorkflowRun.id == run_id).first()
        if not wr:
            raise HTTPException(404, "workflow run not found")
        d = wr.to_dict()
        d["step_detail"] = [s.to_dict() for s in wr.steps]
        return d
    finally:
        db.close()


@app.post("/api/phase2/workflow-runs/{run_id}/rerun")
def p2_rerun_workflow(run_id: str, request: Request):
    """Replay a recorded workflow from its stored definition.

    Patterns need a sequence to run more than once, and the bus forgets
    definitions on restart — so replaying from the recorded definition is how
    a workflow stays repeatable across sessions.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")

    db = SessionLocal()
    try:
        src_run = db.query(phase2.WorkflowRun).filter(
            phase2.WorkflowRun.id == run_id).first()
        if not src_run:
            raise HTTPException(404, "workflow run not found")

        steps = p2_recorder.replay_payload(src_run)
        if not steps:
            raise HTTPException(400, "this run has no replayable definition")

        created = message_bus.create_workflow(
            src_run.name or "Replay", steps, created_by=sess.get("user_id"))
        wid = created.get("workflow_id") or (created.get("workflow") or {}).get("id")
        if not wid:
            raise HTTPException(500, "could not recreate workflow")

        wf_now = message_bus.get_workflow(wid)
        wr = p2_recorder.begin(db, wf_now, owner_id=sess.get("user_id"),
                               definition=message_bus.get_definition(wid))

        # Drive every wave to completion. Bounded so a malformed DAG whose
        # steps never become ready cannot spin here forever.
        waves = 0
        for _ in range(len(steps) + 2):
            result = message_bus.run_workflow(wid)
            waves += 1
            try:
                p2_recorder.record_wave(db, wr, result,
                                        result.get("workflow") or wf_now)
            except Exception as e:
                print(f"[phase2] replay recording failed for {wid}: {e}")
            if (result.get("remaining") or 0) == 0:
                break

        p2_recorder.finalize(db, wr)
        return {"ok": True, "workflow_run_id": wr.id, "bus_workflow_id": wid,
                "waves": waves, "replayed_from": run_id,
                "status": wr.status, "steps": wr.step_count,
                "succeeded": wr.steps_succeeded}
    finally:
        db.close()


@app.get("/api/phase2/agents/{agent_id}/versions")
def p2_version_performance(agent_id: str, request: Request):
    """How each config version of this agent has actually performed.

    Derived entirely from runs that already exist — nothing to set up.
    """
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return phase2.version_performance(db, agent_id)
    finally:
        db.close()


@app.get("/api/phase2/regressions")
def p2_regressions(request: Request):
    """Agents whose newest config version is measurably worse than the last."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return {"regressions": phase2.fleet_regressions(db, owner_id=sess.get("user_id"))}
    finally:
        db.close()


@app.post("/api/phase2/patterns/analyze")
def p2_analyze_patterns(request: Request, lookback_days: int = 30,
                        min_executions: int = 2):
    """Score recurring agent sequences from persisted workflow history."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    db = SessionLocal()
    try:
        return p2_patterns.analyze(db, owner_id=sess.get("user_id"),
                                   lookback_days=lookback_days,
                                   min_executions=min_executions)
    finally:
        db.close()


@app.get("/api/phase2/patterns")
def p2_list_patterns(request: Request, limit: int = 10,
                     min_success_rate: float = 0.0, agents: str = None):
    """Top patterns, or patterns containing every agent in ?agents=a,b,c"""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    uid = sess.get("user_id")
    db = SessionLocal()
    try:
        if agents:
            ids = [a.strip() for a in agents.split(",") if a.strip()]
            return {"patterns": p2_patterns.for_agents(db, ids, owner_id=uid)}
        return {"patterns": p2_patterns.top(db, owner_id=uid, limit=limit,
                                            min_success_rate=min_success_rate)}
    finally:
        db.close()


class Phase2FeedbackIn(BaseModel):
    verdict: str                      # correct | incorrect | partial
    run_id: str | None = None
    workflow_run_id: str | None = None
    rating: int | None = None
    comment: str = ""


@app.post("/api/phase2/feedback")
def p2_feedback(body: Phase2FeedbackIn, request: Request):
    """Record a human verdict on a run or workflow run.

    Metrics say a workflow completed; only a person says it was right.
    """
    sess = _get_session(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    if body.verdict not in ("correct", "incorrect", "partial"):
        raise HTTPException(400, "verdict must be correct, incorrect, or partial")
    if not body.run_id and not body.workflow_run_id:
        raise HTTPException(400, "provide run_id or workflow_run_id")
    db = SessionLocal()
    try:
        fb = phase2.RunFeedback(
            owner_id=sess.get("user_id"),
            run_id=body.run_id,
            workflow_run_id=body.workflow_run_id,
            verdict=body.verdict,
            rating=body.rating,
            comment=(body.comment or "")[:4000],
            created_by=sess.get("user_id"),
        )
        db.add(fb)
        db.commit()
        return {"ok": True, "feedback": fb.to_dict()}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  PLUGINS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/plugins")
def plugins_list(request: Request, category: str = None):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"plugins": plugin_manager.list_plugins(category=category),
            "categories": plugin_manager.categories(),
            "stats": plugin_manager.stats()}

@app.post("/api/plugins/{name}/enable")
def plugins_enable(name: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return plugin_manager.enable(name)

@app.post("/api/plugins/{name}/disable")
def plugins_disable(name: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return plugin_manager.disable(name)

class PluginConfigIn(BaseModel):
    config: dict

@app.post("/api/plugins/{name}/configure")
def plugins_configure(name: str, body: PluginConfigIn, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return plugin_manager.configure(name, body.config)

class PluginInstallIn(BaseModel):
    manifest: dict = None
    url: str = None

@app.post("/api/plugins/install")
def plugins_install(body: PluginInstallIn, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    if body.url:
        return plugin_manager.install_from_url(body.url)
    if body.manifest:
        return plugin_manager.install(body.manifest)
    raise HTTPException(400, "provide a manifest or url")

@app.post("/api/plugins/{name}/assign/{agent_id}")
def plugins_assign(name: str, agent_id: str, request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return plugin_manager.assign_to_agent(agent_id, name)


# ═══════════════════════════════════════════════════════════════
#  WEBSOCKET LIVE STREAMING
# ═══════════════════════════════════════════════════════════════

from fastapi import WebSocket, WebSocketDisconnect

@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket, channel: str = "global"):
    """Live event stream. Connect with ?channel=global or ?channel=agent:<id>."""
    await websocket.accept()
    stream_manager.register(channel, websocket)
    try:
        # Send recent buffered events on connect
        for event in stream_manager.recent_events(limit=20):
            await websocket.send_text(json.dumps(event))
        while True:
            # Keep the connection alive; ignore inbound client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        stream_manager.unregister(channel, websocket)
    except Exception:
        stream_manager.unregister(channel, websocket)

@app.get("/api/stream/channels")
def stream_channels(request: Request):
    if not _get_session(request):
        raise HTTPException(401, "not authenticated")
    return {"active_channels": stream_manager.active_channels(),
            "recent_events": stream_manager.recent_events(limit=50)}


@app.get("/landing")
def landing_page():
    """Marketing / product landing page."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "landing.html")) as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "landing page not found")


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

  <button class="sso-btn" id="btn-google" onclick="window.location.href='/api/auth/login/google'" style="display:none">
    <svg viewBox="0 0 24 24"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>
    Sign in with Google
  </button>
  <button class="sso-btn" id="btn-github" onclick="window.location.href='/api/auth/login/github'" style="display:none">
    <svg viewBox="0 0 24 24"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" fill="#333"/></svg>
    Sign in with GitHub
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

// Check which OAuth providers are configured and show buttons
(async function(){
  try{
    const r=await fetch('/api/auth/providers');
    if(r.ok){
      const d=await r.json();
      if(d.providers && d.providers.includes('google'))
        document.getElementById('btn-google').style.display='';
      if(d.providers && d.providers.includes('github'))
        document.getElementById('btn-github').style.display='';
      // Hide the divider if no OAuth providers are available
      if(!d.providers || d.providers.length===0){
        const div=document.querySelector('.divider');
        if(div) div.style.display='none';
      }
    }
  }catch(e){}
})();
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
:root{--fg:#14181f;--panel:#fffcf8;--bg:#f5f0eb}
/* ── Enterprise module styles (observability, usage, teams, plugins, mesh, recovery) ── */
.card-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:10px}
.data-table{width:100%;border-collapse:collapse;font-size:13px}
.data-table th{text-align:left;padding:7px 8px;border-bottom:2px solid var(--line);font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--faint)}
.data-table td{padding:7px 8px;border-bottom:1px solid var(--line)}
.data-table tbody tr:hover{background:var(--accentsoft)}
.chip{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;background:var(--accentsoft);color:var(--terra);text-transform:uppercase;letter-spacing:.3px}
.btn-sm{padding:4px 10px;font-size:11px;border-radius:5px;border:1px solid var(--accent);background:var(--card);color:var(--accent);cursor:pointer}
.btn-sm:hover{background:var(--accent);color:#fff}
.switch{position:relative;display:inline-block;width:36px;height:20px}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:var(--line2);border-radius:20px;transition:.2s}
.slider:before{position:absolute;content:"";height:14px;width:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.slider{background:var(--sunset1)}
.switch input:checked+.slider:before{transform:translateX(16px)}
.spinner{width:28px;height:28px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;margin:0 auto}
@keyframes spin{to{transform:rotate(360deg)}}
.mono{font-family:'IBM Plex Mono',monospace}
.header{background:linear-gradient(135deg,#1a1008 0%,#2d1810 50%,#1a1008 100%);color:#fff;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid var(--accent)}
.brand{display:flex;align-items:baseline;gap:12px}
.logo{font-family:'Archivo';font-weight:800;font-size:20px;letter-spacing:.14em;padding-left:.14em;background:linear-gradient(135deg,#f97316,#fbbf24);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:11px;letter-spacing:.08em;color:#c4956a}
.nav{display:flex;gap:4px;align-items:center}
.navbtn{background:none;border:1px solid #3d2a1a;color:#c4956a;padding:5px 10px;font-size:10.5px;cursor:pointer;border-radius:3px;font-family:'IBM Plex Mono';letter-spacing:.04em;transition:all .15s}
.navbtn:hover{border-color:#c4632a;color:#f97316}
.navbtn.active{background:linear-gradient(135deg,#c4632a,#ea580c);border-color:#c4632a;color:#fff}
.more-wrap{position:relative}
.more-btn{background:none;border:1px solid #3d2a1a;color:#c4956a;padding:5px 10px;font-size:10.5px;cursor:pointer;border-radius:3px;font-family:'IBM Plex Mono';letter-spacing:.04em;transition:all .15s}
.more-btn:hover{border-color:#c4632a;color:#f97316}
.more-btn.has-active{border-color:#c4632a;color:#f97316}
.more-menu{display:none;position:absolute;top:calc(100% + 6px);left:0;background:#1a1208;border:1px solid #3d2a1a;border-radius:6px;padding:6px 0;min-width:180px;z-index:999;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.more-menu.open{display:block}
.more-menu .navbtn{display:block;width:100%;text-align:left;border:none;border-radius:0;padding:8px 14px;font-size:11px}
.more-menu .navbtn:hover{background:rgba(249,115,22,.1)}
.more-menu .navbtn.active{background:linear-gradient(135deg,#c4632a,#ea580c);border:none;color:#fff}
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
    <button class="navbtn" id="nav-observability" onclick="setView('observability')">Observability</button>
    <button class="navbtn" id="nav-runs" onclick="setView('runs')">Runs</button>
    <button class="navbtn" id="nav-usage" onclick="setView('usage')">Usage</button>
    <button class="navbtn" id="nav-analytics" onclick="setView('analytics')">Analytics</button>
    <button class="navbtn" id="nav-runtime" onclick="setView('runtime')">Runtime</button>
    <button class="navbtn" id="nav-settings" onclick="setView('settings')">Settings</button>
    <div class="more-wrap">
      <button class="more-btn" id="more-toggle" onclick="toggleMoreMenu(event)">More ▾</button>
      <div class="more-menu" id="more-menu">
        <button class="navbtn" id="nav-control" onclick="setView('control');closeMore()">Control</button>
        <button class="navbtn" id="nav-integrations" onclick="setView('integrations');closeMore()">Integrations</button>
        <button class="navbtn" id="nav-deploy" onclick="setView('deploy');closeMore()">Deploy</button>
        <button class="navbtn" id="nav-events" onclick="setView('events');closeMore()">Event Log</button>
        <button class="navbtn" id="nav-history" onclick="setView('history');closeMore()">History</button>
        <button class="navbtn" id="nav-automation" onclick="setView('automation');closeMore()">Automation</button>
        <button class="navbtn" id="nav-templates" onclick="setView('templates');closeMore()">Templates</button>
        <button class="navbtn" id="nav-teams" onclick="setView('teams');closeMore()">Teams</button>
        <button class="navbtn" id="nav-plugins" onclick="setView('plugins');closeMore()">Plugins</button>
        <button class="navbtn" id="nav-comms" onclick="setView('comms');closeMore()">Agent Mesh</button>
        <button class="navbtn" id="nav-workflows" onclick="setView('workflows');closeMore()">Workflows</button>
        <button class="navbtn" id="nav-recovery" onclick="setView('recovery');closeMore()">Recovery</button>
        <button class="navbtn" id="nav-attestations" onclick="setView('attestations');closeMore()">Attestations</button>
        <button class="navbtn" id="nav-approvals" onclick="setView('approvals');closeMore()">Approvals</button>
        <button class="navbtn" id="nav-admin" onclick="setView('admin');closeMore()" style="display:none">Admin</button>
        <button class="navbtn" id="nav-alltabs" onclick="toggleAllTabs()"
          style="border-top:1px solid rgba(255,255,255,.12);opacity:.75;font-size:11px">▸ Show all tabs</button>
      </div>
    </div>
  </div>
  <div class="hmeta" style="display:flex;align-items:center;gap:12px">
    <span><span id="llm-state">rule-based</span> · <span id="count">0</span> agents</span>
    <span id="user-badge" style="display:inline-flex;align-items:center;gap:6px;background:rgba(249,115,22,.15);border:1px solid rgba(249,115,22,.3);border-radius:20px;padding:3px 12px 3px 8px;font-size:10.5px">
      <span style="width:22px;height:22px;border-radius:50%;background:linear-gradient(135deg,#f97316,#fbbf24);display:flex;align-items:center;justify-content:center;font-family:Archivo;font-weight:700;font-size:10px;color:#1a1008" id="user-avatar"></span>
      <span id="user-name" style="color:#fbbf24"></span>
    </span>
    <button id="notif-bell" onclick="toggleNotifPanel()" style="background:none;border:none;cursor:pointer;position:relative;padding:4px 6px;font-size:16px;color:#c4956a" title="Notifications">🔔<span id="notif-badge" style="display:none;position:absolute;top:0;right:0;background:#ef4444;color:#fff;font-size:9px;font-weight:700;border-radius:50%;width:16px;height:16px;line-height:16px;text-align:center">0</span></button>
    <div id="notif-panel" style="display:none;position:absolute;top:48px;right:80px;width:340px;max-height:400px;overflow-y:auto;background:#1e1408;border:1px solid #3d2a1a;border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.5);z-index:999;font-size:12px"></div>
    <button onclick="doLogout()" style="background:none;border:1px solid #3d2a1a;color:#c4956a;padding:3px 10px;font-size:10px;cursor:pointer;border-radius:3px;font-family:'IBM Plex Mono';letter-spacing:.04em;transition:all .15s" onmouseover="this.style.borderColor='#c4632a';this.style.color='#f97316'" onmouseout="this.style.borderColor='#3d2a1a';this.style.color='#c4956a'">Sign Out</button>
  </div>
</div>

<div class="view" id="root"></div>

<script>
let AGENTS=[], sel=null, pending=null, view='monitor', META={}, USER=null, advancedMode=false;

/* Refresh the agent list + header counts. Called at boot and after any change
   that alters what the list shows (ownership, lifecycle, delete). */
async function loadAgents(){
  const r=await fetch('/api/agents'); const d=await r.json();
  AGENTS=d.agents||[]; META=d;
  const c=document.getElementById('count');
  if(c) c.textContent=d.total||0;
  const l=document.getElementById('llm-state');
  if(l) l.textContent = d.llm ? 'model-assisted' : 'rule-based';
  if(!sel && AGENTS.length) sel=AGENTS[0].id;
  return d;
}
const TABS=['monitor','agents','observability','control','runs','usage','integrations','deploy','events','history','automation','templates','teams','plugins','comms','workflows','recovery','attestations','approvals','analytics','runtime','settings'];

async function boot(){
  // Load user session
  try{
    const ur=await fetch('/api/auth/me');
    if(!ur.ok){doLogout();return;}
    const ud=await ur.json();
    USER=ud.user||{};
    const uname=USER.name||USER.email||'User';
    document.getElementById('user-name').textContent=uname;
    document.getElementById('user-avatar').textContent=uname.split(' ').map(w=>(w||'')[0]||'').join('').toUpperCase().slice(0,2)||'U';
    if(USER.is_admin){document.getElementById('nav-admin').style.display='';}
    applyRoleNav();
  }catch(e){console.error('boot auth:',e);}
  try{ await loadAgents(); }catch(e){console.error('boot agents:',e);}
  try{pollNotifications();}catch(e){}
  await render();
}

async function doLogout(){
  await fetch('/api/auth/logout',{method:'POST'});
  document.cookie='cortex_session=;path=/;expires=Thu, 01 Jan 1970 00:00:00 GMT';
  window.location.reload();
}

const PRIMARY_TABS=['monitor','agents','observability','runs','usage','analytics','runtime','settings'];
const MORE_TABS=['control','integrations','deploy','events','history','automation','templates','teams','plugins','comms','workflows','recovery','attestations','approvals','admin'];

/* ── Role-based nav ──────────────────────────────────────────
   Every tab still works and every API stays open — this only decides which
   buttons are worth showing someone by default. A PM does not need the
   plugin registry in their way; an engineer does. "Show all tabs" is always
   one click away, because a hidden tab someone knows exists is worse than
   a crowded nav. */
const ROLE_TABS={
  FDE:      ['monitor','agents','control','runs','deploy','integrations',
             'templates','events','workflows','recovery','settings'],
  PM:       ['monitor','agents','runs','analytics','usage','approvals',
             'attestations','teams','settings'],
  Engineer: ['monitor','agents','control','runs','runtime','observability',
             'events','integrations','deploy','plugins','comms','workflows',
             'automation','history','recovery','settings'],
  Admin:    null,   // null = show everything
};

function showingAllTabs(){
  try{ return localStorage.getItem('cortex_all_tabs')==='1'; }catch(e){ return false; }
}
function toggleAllTabs(){
  try{ localStorage.setItem('cortex_all_tabs', showingAllTabs()?'0':'1'); }catch(e){}
  applyRoleNav();
  closeMore();
}
function applyRoleNav(){
  const allowed = ROLE_TABS[(USER&&USER.role)||''] || null;
  const showAll = showingAllTabs() || !allowed;
  TABS.forEach(v=>{
    const el=document.getElementById('nav-'+v);
    if(!el) return;
    if(v==='admin'){ return; }              // admin has its own is_admin gate
    // Never hide the tab you are currently looking at.
    el.style.display = (showAll || allowed.includes(v) || v===view) ? '' : 'none';
  });
  const t=document.getElementById('nav-alltabs');
  if(t) t.textContent = showAll ? '▾ Fewer tabs' : '▸ Show all tabs';
}
function setActiveNav(){
  TABS.forEach(v=>{
    const el=document.getElementById('nav-'+v);
    if(el) el.classList.toggle('active', v===view);
  });
  // Highlight "More" button if current view is in the dropdown
  const mb=document.getElementById('more-toggle');
  if(mb) mb.classList.toggle('has-active', MORE_TABS.includes(view));
}
function toggleMoreMenu(e){e.stopPropagation();document.getElementById('more-menu').classList.toggle('open');}
function closeMore(){document.getElementById('more-menu').classList.remove('open');}
document.addEventListener('click',function(e){if(!e.target.closest('.more-wrap'))closeMore();});

async function render(){
  setActiveNav();
  applyRoleNav();
  const fn={monitor:renderMonitor,agents:renderAgents,control:renderControl,runs:renderRuns,
    integrations:renderIntegrations,deploy:renderDeploy,events:renderEventLog,
    history:renderHistory,automation:renderAutomation,templates:renderTemplates,
    attestations:renderAttestations,approvals:renderApprovals,analytics:renderAnalytics,
    runtime:renderRuntime,settings:renderSettings,admin:renderAdmin,
    observability:renderObservability,usage:renderUsage,teams:renderTeams,
    plugins:renderPlugins,comms:renderComms,recovery:renderRecovery,
    workflows:renderWorkflows}[view];
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
    </div>

    <div id="ai-chat" style="margin-top:24px;border:1px solid var(--line);border-radius:8px;overflow:hidden">
      <div style="padding:12px 16px;background:var(--bg2);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;cursor:pointer" onclick="toggleChat()">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:18px">💬</span>
          <span style="font-weight:600;font-family:'Archivo',sans-serif;font-size:13px">Cortex Assistant</span>
          <span style="font-size:10px;color:var(--muted)">Ask anything about your agents</span>
        </div>
        <span id="chat-toggle" style="font-size:12px;color:var(--muted)">▼</span>
      </div>
      <div id="chat-body" style="display:none">
        <div id="chat-messages" style="height:260px;overflow-y:auto;padding:12px 16px;font-size:13px"></div>
        <div style="padding:8px 12px;border-top:1px solid var(--line);display:flex;gap:8px">
          <input type="text" id="chat-input" placeholder="e.g. Which agents have errors? What was the last run result?" style="flex:1;padding:8px 10px;border:1px solid var(--line);border-radius:4px;font-size:12px;background:var(--bg2);color:inherit" onkeydown="if(event.key==='Enter')sendChat()">
          <button class="btn accent" style="padding:6px 16px;font-size:12px" onclick="sendChat()">Send</button>
        </div>
      </div>
    </div>`;
}

async function jumpToControl(id){ sel=id; view='control'; await render(); }

/* ═══════════════ AI ASSISTANT ═══════════════ */
let _chatOpen=false, _chatHistory=[];
function toggleChat(){
  _chatOpen=!_chatOpen;
  document.getElementById('chat-body').style.display=_chatOpen?'block':'none';
  document.getElementById('chat-toggle').textContent=_chatOpen?'▲':'▼';
  if(_chatOpen&&!_chatHistory.length){
    document.getElementById('chat-messages').innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:12px">Ask me about your agents — status, errors, performance, configuration, or anything else.</div>';
  }
}
async function sendChat(){
  const input=document.getElementById('chat-input');
  const msg=input.value.trim();
  if(!msg)return;
  input.value='';
  _chatHistory.push({role:'user',content:msg});
  const box=document.getElementById('chat-messages');
  box.innerHTML+=`<div style="margin:8px 0;display:flex;justify-content:flex-end"><div style="background:var(--accent);color:#fff;padding:8px 12px;border-radius:12px 12px 2px 12px;max-width:80%;font-size:12px">${esc(msg)}</div></div>`;
  box.innerHTML+=`<div id="chat-loading" style="margin:8px 0;color:var(--muted);font-size:11px">Thinking...</div>`;
  box.scrollTop=box.scrollHeight;
  try{
    const r=await fetch('/api/assistant/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,history:_chatHistory.slice(-10)})});
    const d=await r.json();
    document.getElementById('chat-loading')?.remove();
    const reply=d.reply||'Sorry, I could not process that.';
    _chatHistory.push({role:'assistant',content:reply});
    box.innerHTML+=`<div style="margin:8px 0;display:flex"><div style="background:var(--bg2);border:1px solid var(--line);padding:8px 12px;border-radius:12px 12px 12px 2px;max-width:80%;font-size:12px;white-space:pre-wrap">${esc(reply)}</div></div>`;
  }catch(e){
    document.getElementById('chat-loading')?.remove();
    box.innerHTML+=`<div style="margin:8px 0;color:var(--brick);font-size:11px">Error connecting to assistant</div>`;
  }
  box.scrollTop=box.scrollHeight;
}

/* ═══════════════ AGENTS — Registration & Management ═══════════════ */
async function renderAgents(){
  const agentList=AGENTS.map(a=>`
    <div class="agent-card" data-s="${a.status}" onclick="sel='${a.id}';setView('control')">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:4px">
        <div class="ac-name">${esc(a.name)}</div>
        <div style="display:flex;gap:4px;align-items:center">
          ${(a.lifecycle&&a.lifecycle!=='active')?lifecycleBadge(a.lifecycle):''}
          <span class="pill ${a.status}">${a.status}</span>
          <button class="btn ghost" style="padding:2px 6px;font-size:10px;color:var(--brick)" onclick="event.stopPropagation();deleteAgent('${a.id}','${esc(a.name)}')">Delete</button>
        </div>
      </div>
      <div class="ac-desc">${esc(a.description||'')}</div>
      <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
        <span class="typetag ${a.type}">${a.type}</span>
        <span class="eptag">${(a.endpoint||{}).type||'embedded'}</span>
        <span style="font-family:'IBM Plex Mono';font-size:9px;color:var(--faint);padding:2px 6px">${a.data_sources_count||0} sources · ${a.tools_count||0} tools</span>
        <span style="font-family:'IBM Plex Mono';font-size:9px;padding:2px 6px;color:${a.owner_name?'var(--muted)':'var(--brick)'}">${a.owner_name?esc(a.owner_name):'unowned'}</span>
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
      <span class="typetag ${a.type}">${a.type}</span>${a.live?'<span class="livetag">LIVE</span>':''}${(a.lifecycle&&a.lifecycle!=='active')?' '+lifecycleBadge(a.lifecycle):''}
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
    <div id="ver-perf" style="margin-top:10px"></div>
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
              ${d.auth_hint?`<span title="Stored encrypted. Cortex never shows the value back." style="font-size:10px;padding:1px 6px;border-radius:3px;background:#e4ded2;color:var(--muted);margin-left:4px;font-family:'IBM Plex Mono'">${esc(d.auth_hint)}</span>`:''}
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

  loadVersionPerf(sel);   // sel is the agent id string, not an object
  loadOwnership(sel);
  loadPrompt(sel);
  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div class="panel">
      <div class="phead">
        <div>
          <h3>${esc(a.name)} ${lifecycleBadge(a.lifecycle||'active','vertical-align:middle;margin-left:6px')}</h3>
          <div class="acct">${esc(a.account||'Custom')}</div>
          <div style="margin-top:4px;font-size:11.5px;color:var(--muted)">${esc(a.description||'')}</div>
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
          ${modeToggle}
          <div class="ctrls">
            <button class="btn ${a.status!=='running'?'accent':'ghost'}" onclick="ctl('start')">${a.status==='running'?'Running':'Start'}</button>
            <button class="btn ghost" onclick="ctl('stop')">Stop</button>
            <button class="btn ghost" onclick="ctl('pause')">Pause</button>
            <button class="btn ghost" onclick="ctl('resume')">Resume</button>
          </div>
        </div>
      </div>
      ${advancedMode ? advancedConfig : `<div class="sect"><h4>Overview</h4>${simpleConfig}</div>`}

      <div class="sect">
        <h4>Ownership &amp; Lifecycle</h4>
        <div id="own-panel"><div class="hint" style="margin:0">Loading…</div></div>
      </div>

      <div class="sect">
        <h4>System Prompt</h4>
        <div id="prompt-panel"><div class="hint" style="margin:0">Loading…</div></div>
      </div>

      ${liveRunPanel(a)}
      <div class="sect">
        <h4>Change Config — Plain English</h4>
        <textarea class="ask" id="ask" placeholder="e.g. Set temperature to 0.5, increase timeout to 10 minutes, escalate at moderate severity"></textarea>
        <div class="ctrls" style="margin-top:10px"><button class="btn accent" id="propose" onclick="propose()">Propose Change</button></div>
        <div class="hint">Cortex proposes a diff and waits for your approval. Try: <code>retry 5 times</code>, <code>timeout 60 seconds</code>, <code>confidence 0.9</code>, <code>switch to openai</code>, <code>turn off confirm</code>.</div>
        <div id="result"></div>
      </div>
    </div></div>`;
  startDaemonPoll();
}

function liveRunPanel(a){
  const cfg=a.config||{};
  const beh=cfg.behavior||{};
  const exec=cfg.execution||{};
  const standing=cfg.standing_instruction||'';
  const interval=cfg.run_interval_seconds||60;
  const intgr=cfg.integrations||[];
  return `<div class="sect livebox">
    <h4>Continuous Mode</h4>
    <div id="daemon-status" style="margin-bottom:12px;padding:12px;border-radius:6px;background:#faf8f5;border:1px solid var(--line)">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="width:8px;height:8px;border-radius:50%;background:${a.status==='running'?'var(--seal)':'var(--faint)'};display:inline-block"></span>
        <span style="font-weight:600;font-size:13px">${a.status==='running'?'Running Continuously':'Stopped'}</span>
        <span id="daemon-cycle" style="font-size:11px;color:var(--muted);margin-left:auto"></span>
      </div>
      <div id="daemon-detail" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
        <div style="text-align:center;padding:6px;background:white;border-radius:4px;border:1px solid var(--line)"><div id="d-cycles" style="font-size:16px;font-weight:600">—</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase">Cycles</div></div>
        <div style="text-align:center;padding:6px;background:white;border-radius:4px;border:1px solid var(--line)"><div id="d-next" style="font-size:16px;font-weight:600">—</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase">Next In</div></div>
        <div style="text-align:center;padding:6px;background:white;border-radius:4px;border:1px solid var(--line)"><div id="d-errors" style="font-size:16px;font-weight:600">0</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase">Errors</div></div>
        <div style="text-align:center;padding:6px;background:white;border-radius:4px;border:1px solid var(--line)"><div id="d-interval" style="font-size:16px;font-weight:600">${interval}s</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase">Interval</div></div>
      </div>
      <div id="d-error-msg" style="display:none;margin-top:8px;padding:8px;background:#fdf2f0;border:1px solid #e8c5bf;border-radius:4px;font-size:11px;color:var(--brick)"></div>
    </div>

    <div style="margin-bottom:12px">
      <label style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);display:block;margin-bottom:6px">Standing Instruction</label>
      <textarea class="ask" id="standing" placeholder="What should this agent do on each cycle? e.g. Monitor support queue, classify new tickets, summarize findings..." style="min-height:80px">${esc(standing)}</textarea>
    </div>
    <div style="display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:12px;align-items:end">
      <div>
        <label style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);display:block;margin-bottom:6px">Run Interval</label>
        <select id="interval-sel" style="width:100%;padding:8px;border:1px solid var(--line);border-radius:4px;font-size:12px;background:white">
          <option value="30" ${interval===30?'selected':''}>Every 30 seconds</option>
          <option value="60" ${interval===60?'selected':''}>Every 1 minute</option>
          <option value="120" ${interval===120?'selected':''}>Every 2 minutes</option>
          <option value="300" ${interval===300?'selected':''}>Every 5 minutes</option>
          <option value="600" ${interval===600?'selected':''}>Every 10 minutes</option>
          <option value="1800" ${interval===1800?'selected':''}>Every 30 minutes</option>
          <option value="3600" ${interval===3600?'selected':''}>Every 1 hour</option>
        </select>
      </div>
      <button class="btn accent" onclick="saveStanding()" style="padding:8px 16px">Save</button>
    </div>
    <div class="hint" style="margin:0 0 12px">The agent runs this instruction on every cycle while status is <b>running</b>. Pause/resume controls the loop without changing the instruction.</div>

    <div style="margin-bottom:12px;padding:12px;border-radius:6px;border:1px solid var(--line);background:#faf8f5">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent)">Integrations (${intgr.length})</div>
        <button class="btn ghost" style="padding:2px 10px;font-size:10px" onclick="document.getElementById('add-intg').style.display=document.getElementById('add-intg').style.display==='none'?'block':'none'">+ Add</button>
      </div>
      <div id="intg-list">
        ${intgr.length?intgr.map(ig=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 8px;margin-bottom:4px;background:white;border-radius:4px;border:1px solid var(--line)">
          <div>
            <span style="font-weight:500;font-size:12px">${esc(ig.name)}</span>
            <span style="font-size:10px;padding:1px 6px;border-radius:3px;background:#d4dce0;color:var(--ink);margin-left:4px">${esc(ig.type)}</span>
            <span style="font-size:10px;color:${ig.status==='connected'?'var(--seal)':'var(--faint)'};margin-left:4px">${ig.status||'pending'}</span>
          </div>
          <button class="btn ghost" style="padding:2px 8px;font-size:10px" onclick="removeIntegration('${esc(ig.name)}')">×</button>
        </div>`).join(''):'<div style="font-size:11px;color:var(--faint)">No integrations. Connect Slack, GitHub, webhooks, or APIs to let agents interact with real systems.</div>'}
      </div>
      <div id="add-intg" style="display:none;margin-top:8px;padding:10px;background:white;border-radius:4px;border:1px dashed var(--line)">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
          <div><label style="font-size:10px;font-weight:500;color:var(--muted)">Name</label><input id="intg-name" placeholder="e.g. team-slack" style="font-size:11px;padding:6px 8px;width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:4px"></div>
          <div><label style="font-size:10px;font-weight:500;color:var(--muted)">Type</label><select id="intg-type" style="font-size:11px;padding:6px 8px;width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:4px">
            <option value="slack">Slack</option><option value="github">GitHub</option><option value="webhook">Webhook</option>
            <option value="rest_api">REST API</option><option value="database">Database</option><option value="email">Email/SMTP</option>
            <option value="s3">S3/Storage</option><option value="custom">Custom</option></select></div>
        </div>
        <div style="margin-bottom:8px"><label style="font-size:10px;font-weight:500;color:var(--muted)">Endpoint / Config</label><input id="intg-endpoint" placeholder="https://hooks.slack.com/... or connection string" style="font-size:11px;padding:6px 8px;width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:4px"></div>
        <button class="btn accent" style="font-size:11px;padding:6px 14px" onclick="addIntegration()">Connect</button>
      </div>
    </div>

    <div class="sect" style="margin-top:0;padding-top:12px;border-top:1px solid var(--line)">
      <h4>Manual Run</h4>
      <div class="hint" style="margin:0 0 8px">Run a one-off task outside the continuous loop.</div>
      <textarea class="ask" id="claim" placeholder="Enter a one-off input for this agent..."></textarea>
      <div class="ctrls" style="margin-top:8px"><button class="btn accent" id="runbtn" onclick="runAgent()">Run Once</button></div>
      <div id="runout"></div>
    </div>
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

async function saveStanding(){
  const si=document.getElementById('standing').value;
  const iv=parseInt(document.getElementById('interval-sel').value);
  await fetch('/api/agents/'+sel+'/standing-instruction',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({standing_instruction:si,run_interval_seconds:iv})});
  await boot(); renderControl();
}

async function addIntegration(){
  const name=document.getElementById('intg-name').value.trim();
  if(!name) return;
  const type=document.getElementById('intg-type').value;
  const endpoint=document.getElementById('intg-endpoint').value.trim();
  const a=await (await fetch('/api/agents/'+sel)).json();
  const cfg=a.config||{};
  const intgr=cfg.integrations||[];
  intgr.push({name,type,endpoint,status:'connected',added_at:new Date().toISOString()});
  cfg.integrations=intgr;
  await fetch('/api/agents/'+sel+'/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:cfg})});
  await boot(); renderControl();
}

async function removeIntegration(name){
  const a=await (await fetch('/api/agents/'+sel)).json();
  const cfg=a.config||{};
  cfg.integrations=(cfg.integrations||[]).filter(i=>i.name!==name);
  await fetch('/api/agents/'+sel+'/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:cfg})});
  await boot(); renderControl();
}

let _daemonPoll=null;
function startDaemonPoll(){
  if(_daemonPoll) clearInterval(_daemonPoll);
  _daemonPoll=setInterval(async()=>{
    if(!sel) return;
    try{
      const d=await (await fetch('/api/agents/'+sel+'/daemon')).json();
      const ce=document.getElementById('d-cycles');
      if(!ce) return;
      document.getElementById('d-cycles').textContent=d.cycle_count||0;
      const nextIn=d.next_run_in_seconds;
      document.getElementById('d-next').textContent=nextIn!=null?(nextIn>60?Math.floor(nextIn/60)+'m':nextIn+'s'):'—';
      document.getElementById('d-errors').textContent=d.consecutive_errors||0;
      document.getElementById('d-errors').style.color=(d.consecutive_errors>0)?'var(--brick)':'inherit';
      const cyc=document.getElementById('daemon-cycle');
      if(cyc) cyc.textContent=d.active?(d.paused?'PAUSED':'cycle #'+(d.cycle_count||0)):'not tracked';
      const errEl=document.getElementById('d-error-msg');
      if(d.last_error && errEl){errEl.style.display='block';errEl.textContent=d.last_error;}
      else if(errEl){errEl.style.display='none';}
    }catch(e){}
  },5000);
}

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
  const [cfgRes,relRes]=await Promise.all([
    fetch('/api/agents/'+sel).then(r=>r.json()).catch(()=>({})),
    fetch('/api/agents/'+sel+'/releases').then(r=>r.json()).catch(()=>({releases:[]})),
  ]);
  const cfg=cfgRes.config||{}, cur=a?.version||1;
  const rel={}; (relRes.releases||[]).forEach(r=>{rel[r.environment]=r;});

  const ago=iso=>{
    if(!iso) return 'never';
    const s=Math.floor((Date.now()-new Date(iso).getTime())/1000);
    if(s<60) return 'just now';
    if(s<3600) return Math.floor(s/60)+'m ago';
    if(s<86400) return Math.floor(s/3600)+'h ago';
    return Math.floor(s/86400)+'d ago';
  };

  const envCard=name=>{
    const r=rel[name];
    if(!r) return `<div class="env-card" style="border-left:3px solid var(--faint)">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div><div class="env-name">${esc(name[0].toUpperCase()+name.slice(1))}</div>
          <div class="env-url" style="color:var(--faint)">No version released</div></div>
        <span class="pill stopped">none</span></div></div>`;

    const behind = r.active_version < cur;
    const neverFetched = !r.last_fetched_at;
    return `<div class="env-card" style="border-left:3px solid ${neverFetched?'var(--ochre)':'var(--seal)'}">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div><div class="env-name">${esc(name[0].toUpperCase()+name.slice(1))}</div>
          <div class="env-url">running v${r.active_version}</div></div>
        <span class="pill ${neverFetched?'stopped':'running'}">${neverFetched?'not picked up':'live'}</span>
      </div>
      <div class="env-status">
        <div class="health-dot ${neverFetched?'gray':'green'}"></div>
        <span>released ${esc(ago(r.released_at))}${r.released_by_email?' by '+esc(r.released_by_email):''}
          · fetched ${esc(ago(r.last_fetched_at))}${r.fetch_count?' ('+r.fetch_count+'x)':''}</span>
      </div>
      ${r.note?`<div class="hint" style="margin-top:6px">${esc(r.note)}</div>`:''}
      ${neverFetched?`<div class="flag" style="margin-top:6px;font-size:11px">This environment has never fetched its config. Released, but not confirmed running.</div>`:''}
      ${behind?`<div class="hint" style="margin-top:6px">Behind — this workspace is on v${cur}.</div>`:''}
    </div>`;
  };

  const origin=location.origin;
  document.getElementById('root').innerHTML=`<div class="wrap">${sidebar()}
    <div>
      <h2>${esc(a?.name||sel)} — Releases</h2>
      <div class="panel" style="margin-bottom:16px">
        <div class="phead"><h3>Environments</h3><span class="vtag">workspace v${cur}</span></div>
        <div class="sect">
          <div class="hint" style="margin-bottom:10px">
            CORTEX does not run your agents — it holds their config. Releasing marks a version live
            for an environment; your agent picks it up on its next fetch.
          </div>
          ${envCard('staging')}${envCard('production')}
        </div>
      </div>

      <div class="panel" style="margin-bottom:16px">
        <div class="phead"><h3>Release a version</h3></div>
        <div class="sect">
          <div class="form-group"><label>Environment</label>
            <select id="rel-env"><option value="staging">Staging</option><option value="production">Production</option></select></div>
          <div class="form-group"><label>Version</label>
            <input id="rel-ver" type="number" min="1" max="${cur}" value="${cur}">
            <div class="hint" style="margin-top:4px">Defaults to this workspace's current version. Lower it to roll an environment back.</div></div>
          <div class="form-group"><label>Note (optional)</label>
            <input id="rel-note" placeholder="e.g. loosened confidence threshold after the Tuesday regression"></div>
          <button class="btn accent" onclick="releaseAgent()">Release</button>
          <div id="rel-msg" style="margin-top:10px;font-size:12px"></div>
        </div>
      </div>

      <div class="panel">
        <div class="phead"><h3>How your agent fetches this</h3></div>
        <div class="sect">
          <div class="hint" style="margin-bottom:8px">One call at boot, or on an interval. Needs an API key with <span class="mono">agents:read</span>.</div>
          <pre class="mono" style="background:var(--paper);border:1px solid var(--line);border-radius:4px;padding:10px;font-size:11px;overflow-x:auto;white-space:pre">curl -H "Authorization: Bearer ctx_..." \\
  "${esc(origin)}/api/agents/${esc(sel)}/config?env=production"</pre>
          <div class="hint" style="margin-top:8px">Returns the config snapshot at the released version, plus the version number — so your runtime can log which config it is actually on.</div>
        </div>
      </div>
    </div></div>`;
}

async function releaseAgent(){
  const env=document.getElementById('rel-env').value;
  const ver=parseInt(document.getElementById('rel-ver').value,10);
  const note=document.getElementById('rel-note').value.trim();
  const msg=document.getElementById('rel-msg');
  if(!ver||ver<1){msg.innerHTML='<span style="color:var(--brick)">Pick a version.</span>';return;}
  msg.innerHTML='<span style="color:var(--accent)"><span class="spin">&#x25D4;</span> Releasing…</span>';
  let d;
  try{
    d=await (await fetch('/api/agents/'+sel+'/release',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({environment:env,version:ver,note:note})})).json();
  }catch(e){ msg.innerHTML='<span style="color:var(--brick)">Request failed: '+esc(e.message)+'</span>'; return; }
  if(d&&d.ok){
    msg.innerHTML='<span style="color:var(--seal)">v'+d.active_version+' released to '+esc(env)+'.</span>'
      +' <span style="color:var(--muted)">It goes live there on the next fetch'
      +(d.previous_version?', replacing v'+d.previous_version:'')+'.</span>';
  }else{
    msg.innerHTML='<span style="color:var(--brick)">'+esc((d&&(d.error||d.detail))||'Release failed.')+'</span>';
  }
  setTimeout(renderDeploy,900);
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

  // Fetch API keys
  let apiKeysHtml='';
  try{
    const keysR=await fetch('/api/keys');
    const keysD=await keysR.json();
    const keys=keysD.keys||[];
    const keyRows=keys.map(k=>`<tr>
      <td style="padding:8px 10px;font-weight:500">${esc(k.name)}</td>
      <td style="padding:8px 10px;font-family:'IBM Plex Mono';font-size:11px">${esc(k.prefix)}...</td>
      <td style="padding:8px 10px;font-size:11px">${(k.scopes||[]).join(', ')}</td>
      <td style="padding:8px 10px;font-size:11px;color:var(--muted)">${k.last_used_at?new Date(k.last_used_at).toLocaleDateString():'never'}</td>
      <td style="padding:8px 10px"><button class="btn ghost" style="font-size:11px;padding:2px 8px;color:var(--brick)" onclick="revokeApiKey('${k.id}')">Revoke</button></td>
    </tr>`).join('');
    apiKeysHtml=`<div style="margin-top:32px;border-top:1px solid var(--line);padding-top:24px">
      <h3 style="font-family:'Archivo',sans-serif;margin-bottom:4px">API Keys</h3>
      <div class="hint" style="margin-bottom:12px">Create keys for external systems to call your agents programmatically via the REST API.</div>
      <div style="display:flex;gap:8px;margin-bottom:16px;align-items:end">
        <div style="flex:1"><label style="font-size:11px;display:block;margin-bottom:4px">Key Name</label>
          <input type="text" id="new_key_name" placeholder="e.g. CI Pipeline" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font-size:12px">
        </div>
        <div><label style="font-size:11px;display:block;margin-bottom:4px">Scopes</label>
          <select id="new_key_scopes" multiple style="padding:4px 6px;border:1px solid var(--line);border-radius:3px;font-size:11px;min-width:160px" size="3">
            <option value="agents:read" selected>agents:read</option>
            <option value="agents:run" selected>agents:run</option>
            <option value="agents:write">agents:write</option>
          </select>
        </div>
        <button class="btn accent" style="padding:6px 14px" onclick="createApiKey()">Create Key</button>
      </div>
      <div id="new_key_result" style="display:none;margin-bottom:16px;padding:12px;background:#f0fdf4;border:1px solid #86efac;border-radius:4px;font-family:'IBM Plex Mono';font-size:12px;word-break:break-all"></div>
      ${keys.length?`<table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:var(--bg2);text-align:left">
          <th style="padding:8px 10px">Name</th><th style="padding:8px 10px">Prefix</th><th style="padding:8px 10px">Scopes</th><th style="padding:8px 10px">Last Used</th><th style="padding:8px 10px"></th>
        </tr></thead>
        <tbody>${keyRows}</tbody>
      </table>`:'<div class="hint">No API keys yet.</div>'}
    </div>`;
  }catch(e){apiKeysHtml='';}

  // Fetch webhooks
  let webhooksHtml='';
  try{
    const whR=await fetch('/api/webhooks');
    const whD=await whR.json();
    const hooks=whD.webhooks||[];
    const hookRows=hooks.map(h=>`<tr>
      <td style="padding:8px 10px;font-family:'IBM Plex Mono';font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.url)}</td>
      <td style="padding:8px 10px;font-size:11px">${(h.events||[]).join(', ')}</td>
      <td style="padding:8px 10px;font-size:11px">${h.is_active?'<span style="color:var(--seal)">active</span>':`<span style="color:var(--brick)">disabled (${h.failure_count} failures)</span>`}</td>
      <td style="padding:8px 10px;font-size:11px;color:var(--muted)">${h.last_triggered_at?new Date(h.last_triggered_at).toLocaleDateString():'never'}</td>
      <td style="padding:8px 10px">
        <button class="btn ghost" style="font-size:11px;padding:2px 8px" onclick="testWebhook('${h.id}')">Test</button>
        <button class="btn ghost" style="font-size:11px;padding:2px 8px;color:var(--brick)" onclick="deleteWebhook('${h.id}')">Delete</button>
      </td>
    </tr>`).join('');
    webhooksHtml=`<div style="margin-top:32px;border-top:1px solid var(--line);padding-top:24px">
      <h3 style="font-family:'Archivo',sans-serif;margin-bottom:4px">Webhooks</h3>
      <div class="hint" style="margin-bottom:12px">Get notified via HTTP POST when agent events occur. Payloads are signed with HMAC-SHA256.</div>
      <div style="display:flex;gap:8px;margin-bottom:16px;align-items:end;flex-wrap:wrap">
        <div style="flex:1;min-width:200px"><label style="font-size:11px;display:block;margin-bottom:4px">Endpoint URL</label>
          <input type="url" id="new_wh_url" placeholder="https://your-server.com/webhook" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font-size:12px">
        </div>
        <div><label style="font-size:11px;display:block;margin-bottom:4px">Events</label>
          <select id="new_wh_events" multiple style="padding:4px 6px;border:1px solid var(--line);border-radius:3px;font-size:11px;min-width:140px" size="4">
            <option value="run.completed" selected>run.completed</option>
            <option value="run.escalated" selected>run.escalated</option>
            <option value="run.error" selected>run.error</option>
            <option value="agent.status_changed">agent.status_changed</option>
          </select>
        </div>
        <button class="btn accent" style="padding:6px 14px" onclick="createWebhook()">Add Webhook</button>
      </div>
      <div id="wh_result" style="display:none;margin-bottom:16px;padding:12px;background:#f0fdf4;border:1px solid #86efac;border-radius:4px;font-family:'IBM Plex Mono';font-size:12px;word-break:break-all"></div>
      ${hooks.length?`<table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:var(--bg2);text-align:left">
          <th style="padding:8px 10px">URL</th><th style="padding:8px 10px">Events</th><th style="padding:8px 10px">Status</th><th style="padding:8px 10px">Last Fired</th><th style="padding:8px 10px"></th>
        </tr></thead>
        <tbody>${hookRows}</tbody>
      </table>`:'<div class="hint">No webhooks configured.</div>'}
    </div>`;
  }catch(e){webhooksHtml='';}

  document.getElementById('root').innerHTML=`<div style="max-width:620px">
    <h2>Settings</h2>
    <div class="hint" style="margin-bottom:16px">Configure LLM providers and models. Keys are stored locally and never leave your instance.</div>
    ${provOpts}
    <div style="margin-top:16px">
      <button class="btn accent" onclick="saveSettings()">Save Settings</button>
      <span id="settings_msg" style="margin-left:12px;font-size:11px"></span>
    </div>
    ${apiKeysHtml}
    ${webhooksHtml}
  </div>`;
}

async function selectProvider(p){
  await(await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:p})})).json();
}
async function testProvider(p){
  const key=document.getElementById('key_'+p).value;
  if(!key){document.getElementById('test_'+p).textContent='no key';document.getElementById('test_'+p).style.color='var(--brick)';return;}
  // Save the key first so the status updates
  await saveSettings();
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

async function createApiKey(){
  const name=document.getElementById('new_key_name').value.trim();
  if(!name){alert('Enter a key name');return;}
  const sel=document.getElementById('new_key_scopes');
  const scopes=Array.from(sel.selectedOptions).map(o=>o.value);
  const r=await fetch('/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,scopes})});
  const d=await r.json();
  if(d.ok){
    const box=document.getElementById('new_key_result');
    box.style.display='block';
    box.innerHTML='<strong style="color:#16a34a">Copy this key now — it will not be shown again:</strong><br><br>'+esc(d.key);
    document.getElementById('new_key_name').value='';
    setTimeout(()=>renderSettings(),8000);
  }
}
async function revokeApiKey(id){
  if(!confirm('Revoke this API key? Any systems using it will stop working.'))return;
  await fetch('/api/keys/'+id,{method:'DELETE'});
  await renderSettings();
}

/* ═══════════════ ADMIN PANEL ═══════════════ */
async function renderAdmin(){
  if(!USER||!USER.is_admin){document.getElementById('root').innerHTML='<div style="padding:40px;color:var(--brick)">Admin access required.</div>';return;}
  const [usersR, statsR]=await Promise.all([fetch('/api/admin/users'),fetch('/api/admin/stats')]);
  const usersD=await usersR.json(), statsD=await statsR.json();
  const users=usersD.users||[];
  document.getElementById('root').innerHTML=`
    <div style="padding:24px">
      <h2 style="font-family:'Archivo',sans-serif;font-weight:700;margin-bottom:16px">Admin Panel</h2>
      <div style="display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap">
        <div class="card" style="padding:16px;text-align:center;min-width:120px">
          <div style="font-size:28px;font-weight:700;color:var(--accent)">${statsD.total_users}</div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Total Users</div>
        </div>
        <div class="card" style="padding:16px;text-align:center;min-width:120px">
          <div style="font-size:28px;font-weight:700;color:var(--seal)">${statsD.active_users}</div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Active Users</div>
        </div>
        <div class="card" style="padding:16px;text-align:center;min-width:120px">
          <div style="font-size:28px;font-weight:700;color:var(--ochre)">${statsD.admin_users}</div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Admins</div>
        </div>
        <div class="card" style="padding:16px;text-align:center;min-width:120px">
          <div style="font-size:28px;font-weight:700;color:var(--accent)">${statsD.total_agents}</div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Total Agents</div>
        </div>
        <div class="card" style="padding:16px;text-align:center;min-width:120px">
          <div style="font-size:28px;font-weight:700;color:var(--seal)">${statsD.total_runs}</div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Total Runs</div>
        </div>
      </div>
      <div class="card" style="padding:20px">
        <h3 style="font-family:'Archivo',sans-serif;font-weight:600;margin-bottom:12px">User Management</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="border-bottom:2px solid var(--line);text-align:left">
              <th style="padding:8px 12px">Name</th>
              <th style="padding:8px 12px">Email</th>
              <th style="padding:8px 12px">Role</th>
              <th style="padding:8px 12px">Org</th>
              <th style="padding:8px 12px">Status</th>
              <th style="padding:8px 12px">Admin</th>
              <th style="padding:8px 12px">Last Login</th>
              <th style="padding:8px 12px">Actions</th>
            </tr>
          </thead>
          <tbody>
            ${users.map(u=>`<tr style="border-bottom:1px solid var(--line)">
              <td style="padding:8px 12px;font-weight:500">${esc(u.name)}</td>
              <td style="padding:8px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">${esc(u.email)}</td>
              <td style="padding:8px 12px">${esc(u.role)}</td>
              <td style="padding:8px 12px">${esc(u.org||'—')}</td>
              <td style="padding:8px 12px">
                <span style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;
                  background:${u.is_active?'var(--sealsoft)':'var(--bricksoft)'};color:${u.is_active?'var(--seal)':'var(--brick)'}">
                  ${u.is_active?'Active':'Disabled'}</span>
              </td>
              <td style="padding:8px 12px">
                <span style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;
                  background:${u.is_admin?'var(--ochresoft)':'var(--card)'};color:${u.is_admin?'var(--ochre)':'var(--muted)'}">
                  ${u.is_admin?'Admin':'User'}</span>
              </td>
              <td style="padding:8px 12px;font-size:12px;color:var(--muted)">${u.last_login?new Date(u.last_login).toLocaleDateString():'Never'}</td>
              <td style="padding:8px 12px">
                ${u.id===USER.user_id?'<span style="color:var(--muted);font-size:11px">You</span>':`
                  <button class="btn ghost" style="font-size:11px;padding:3px 8px;margin-right:4px" onclick="adminToggleAdmin('${u.id}',${!u.is_admin})">${u.is_admin?'Remove Admin':'Make Admin'}</button>
                  <button class="btn ghost" style="font-size:11px;padding:3px 8px;margin-right:4px" onclick="adminToggleActive('${u.id}',${!u.is_active})">${u.is_active?'Disable':'Enable'}</button>
                  ${!u.is_admin?`<button class="btn ghost" style="font-size:11px;padding:3px 8px;color:var(--brick)" onclick="adminDeleteUser('${u.id}','${esc(u.email)}')">Delete</button>`:''}`}
              </td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

async function adminToggleAdmin(uid,val){
  await fetch('/api/admin/users/'+uid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({is_admin:val})});
  await renderAdmin();
}
async function adminToggleActive(uid,val){
  await fetch('/api/admin/users/'+uid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({is_active:val})});
  await renderAdmin();
}
async function adminDeleteUser(uid,email){
  if(!confirm('Delete user '+email+'? This cannot be undone.')) return;
  await fetch('/api/admin/users/'+uid,{method:'DELETE'});
  await renderAdmin();
}

/* ═══════════════ WEBHOOKS ═══════════════ */
async function createWebhook(){
  const url=document.getElementById('new_wh_url').value.trim();
  if(!url){alert('Enter a webhook URL');return;}
  const sel=document.getElementById('new_wh_events');
  const events=Array.from(sel.selectedOptions).map(o=>o.value);
  const r=await fetch('/api/webhooks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,events})});
  const d=await r.json();
  if(d.ok){
    const box=document.getElementById('wh_result');
    box.style.display='block';
    box.innerHTML='<strong style="color:#16a34a">Webhook created.</strong> Signing secret: <code>'+esc(d.secret)+'</code>';
    document.getElementById('new_wh_url').value='';
    setTimeout(()=>renderSettings(),5000);
  }
}
async function testWebhook(id){
  const r=await fetch('/api/webhooks/'+id+'/test',{method:'POST'});
  const d=await r.json();
  alert(d.ok?'Webhook test sent successfully':'Webhook test failed: '+(d.error||'unknown error'));
}
async function deleteWebhook(id){
  if(!confirm('Delete this webhook?'))return;
  await fetch('/api/webhooks/'+id,{method:'DELETE'});
  await renderSettings();
}

/* ═══════════════ TEMPLATES ═══════════════ */
async function renderTemplates(){
  const r=await fetch('/api/templates');
  const d=await r.json();
  const tmpls=d.templates||[];
  const cats=[...new Set(tmpls.map(t=>t.category))];
  const catFilter=cats.map(c=>`<button class="btn ghost" style="font-size:11px;padding:3px 10px" onclick="filterTemplates('${c}')">${c}</button>`).join(' ');

  const cards=tmpls.map(t=>`<div class="card" style="padding:16px;display:flex;flex-direction:column;gap:8px">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:24px">${esc(t.icon||'🤖')}</span>
      <div>
        <div style="font-weight:600;font-size:14px">${esc(t.name)}</div>
        <div style="font-size:11px;color:var(--muted)">${esc(t.category)} · used ${t.use_count}x</div>
      </div>
    </div>
    <div style="font-size:12px;color:var(--faint);flex:1">${esc(t.description||'No description')}</div>
    <div style="display:flex;gap:6px;margin-top:4px">
      <button class="btn accent" style="font-size:11px;padding:4px 12px" onclick="cloneTemplate('${t.id}')">Use Template</button>
      <button class="btn ghost" style="font-size:11px;padding:4px 8px;color:var(--brick)" onclick="deleteTemplate('${t.id}')">Delete</button>
    </div>
  </div>`).join('');

  document.getElementById('root').innerHTML=`<div style="padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="font-family:'Archivo',sans-serif;font-weight:700">Agent Templates</h2>
      <button class="btn accent" onclick="showSaveAsTemplate()">+ Save Agent as Template</button>
    </div>
    <div class="hint" style="margin-bottom:16px">Reusable agent configurations. Clone a template to spin up a pre-configured agent instantly.</div>
    ${cats.length?`<div style="margin-bottom:16px;display:flex;gap:6px">${catFilter}</div>`:''}
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px">
      ${cards||'<div class="hint">No templates yet. Save an agent as a template to get started.</div>'}
    </div>
    <div id="tmpl-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:none;align-items:center;justify-content:center"></div>
  </div>`;
}
async function cloneTemplate(id){
  const r=await fetch('/api/templates/'+id+'/clone',{method:'POST'});
  const d=await r.json();
  if(d.ok){alert('Agent created: '+d.slug);await boot();setView('agents');}
}
async function deleteTemplate(id){
  if(!confirm('Delete this template?'))return;
  await fetch('/api/templates/'+id,{method:'DELETE'});
  await renderTemplates();
}
async function showSaveAsTemplate(){
  const name=prompt('Template name:');
  if(!name)return;
  const desc=prompt('Description (optional):');
  const cat=prompt('Category (customer-support / data-processing / monitoring / automation / custom):','custom');
  const icon=prompt('Emoji icon:','🤖');
  // Pick an agent to snapshot
  if(!AGENTS.length){alert('No agents to save as template');return;}
  const agentNames=AGENTS.map((a,i)=>`${i+1}. ${a.name}`).join('\\n');
  const pick=prompt('Which agent to save?\\n'+agentNames);
  const idx=parseInt(pick)-1;
  if(isNaN(idx)||idx<0||idx>=AGENTS.length)return;
  const r=await fetch('/api/templates/from-agent',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent_id:AGENTS[idx].id,name,description:desc||'',category:cat||'custom',icon:icon||'🤖'})});
  const d=await r.json();
  if(d.ok){alert('Template saved');await renderTemplates();}
}

/* ═══════════════ ATTESTATIONS ═══════════════ */
async function renderAttestations(){
  const agentFilter=sel?'?agent_id='+sel:'';
  const r=await fetch('/api/attestations'+agentFilter);
  const d=await r.json();
  const records=d.attestations||[];

  const agentOpts=AGENTS.map(a=>`<option value="${a.id}" ${a.id===sel?'selected':''}>${esc(a.name)}</option>`).join('');

  const rows=records.map(r=>`<tr style="border-bottom:1px solid var(--line)">
    <td style="padding:8px 10px;font-size:11px;color:var(--muted)">${r.created_at?new Date(r.created_at).toLocaleString():''}</td>
    <td style="padding:8px 10px;font-weight:500;font-size:12px">${esc(r.action)}</td>
    <td style="padding:8px 10px;font-size:12px">${esc(r.agent_name)} <span style="font-size:10px;color:var(--muted)">v${r.agent_version}</span></td>
    <td style="padding:8px 10px;font-size:12px">${esc(r.authorized_by||'system')}</td>
    <td style="padding:8px 10px;font-size:11px">${esc(r.provider)}/${esc(r.model)}</td>
    <td style="padding:8px 10px"><span style="font-size:11px;padding:2px 6px;border-radius:3px;background:${r.action_result==='COMPLETED'?'rgba(34,197,94,.15);color:#22c55e':r.action_result==='ERROR'?'rgba(239,68,68,.15);color:#ef4444':'rgba(249,115,22,.15);color:#f97316'}">${esc(r.action_result||'—')}</span></td>
    <td style="padding:8px 10px;font-size:11px">${r.human_approval_required?(r.human_approval_granted?'✅ approved':'⏳ pending'):'—'}</td>
    <td style="padding:8px 10px;font-family:'IBM Plex Mono';font-size:9px;color:var(--faint);max-width:80px;overflow:hidden;text-overflow:ellipsis" title="${esc(r.record_hash)}">${esc((r.record_hash||'').slice(0,12))}…</td>
  </tr>`).join('');

  document.getElementById('root').innerHTML=`<div style="padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="font-family:'Archivo',sans-serif;font-weight:700">Attestation Trail</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <select onchange="sel=this.value;renderAttestations()" style="padding:4px 8px;border:1px solid var(--line);border-radius:3px;font-size:12px;background:var(--bg2);color:inherit">
          <option value="">All agents</option>
          ${agentOpts}
        </select>
        ${sel?`<button class="btn ghost" style="font-size:11px" onclick="verifyChain('${sel}')">Verify Chain</button>`:''}
      </div>
    </div>
    <div class="hint" style="margin-bottom:16px">Immutable, hash-chained provenance records. Every agent action is logged with who authorized it, which model ran, and what happened.</div>
    <div id="verify-result" style="display:none;margin-bottom:16px;padding:12px;border-radius:4px;font-size:12px"></div>
    ${records.length?`<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:var(--bg2);text-align:left">
        <th style="padding:8px 10px">Time</th><th style="padding:8px 10px">Action</th><th style="padding:8px 10px">Agent</th>
        <th style="padding:8px 10px">Authorized By</th><th style="padding:8px 10px">Model</th><th style="padding:8px 10px">Result</th>
        <th style="padding:8px 10px">Approval</th><th style="padding:8px 10px">Hash</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`:'<div class="hint">No attestation records yet. Run an agent to generate provenance data.</div>'}
  </div>`;
}
async function verifyChain(agentId){
  const r=await fetch('/api/attestations/verify/'+agentId);
  const d=await r.json();
  const box=document.getElementById('verify-result');
  box.style.display='block';
  if(d.ok){
    box.style.background='rgba(34,197,94,.1)';box.style.border='1px solid #22c55e';box.style.color='#22c55e';
    box.textContent='✓ Chain verified — '+d.total+' records, all intact.';
  }else{
    box.style.background='rgba(239,68,68,.1)';box.style.border='1px solid #ef4444';box.style.color='#ef4444';
    box.textContent='✗ Chain broken — '+d.broken_links.length+' link(s) tampered with out of '+d.total+' records.';
  }
}

/* ═══════════════ APPROVALS ═══════════════ */
async function renderApprovals(){
  const [pendingR,decidedR]=await Promise.all([fetch('/api/approvals?status=pending'),fetch('/api/approvals?status=approved')]);
  const pendingD=await pendingR.json(), decidedD=await decidedR.json();
  const pending=pendingD.approvals||[], decided=decidedD.approvals||[];

  const pendingCards=pending.map(a=>`<div class="card" style="padding:16px;margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;align-items:start">
      <div>
        <div style="font-weight:600;font-size:14px">${esc(a.action)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">Agent: ${esc(a.agent_id)} · ${a.created_at?new Date(a.created_at).toLocaleString():''}</div>
        ${a.context&&a.context.claim?`<div style="font-size:12px;margin-top:8px;padding:8px;background:var(--bg2);border-radius:3px">${esc(a.context.claim)}</div>`:''}
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn accent" style="font-size:12px;padding:6px 16px" onclick="decideApproval('${a.id}','approved')">Approve</button>
        <button class="btn ghost" style="font-size:12px;padding:6px 16px;color:var(--brick)" onclick="decideApproval('${a.id}','rejected')">Reject</button>
      </div>
    </div>
  </div>`).join('');

  document.getElementById('root').innerHTML=`<div style="padding:24px">
    <h2 style="font-family:'Archivo',sans-serif;font-weight:700;margin-bottom:4px">Approval Queue</h2>
    <div class="hint" style="margin-bottom:20px">Review and approve or reject pending agent actions. Agents configured to require human approval will pause here before executing.</div>
    <h3 style="font-family:'Archivo',sans-serif;font-weight:600;margin-bottom:12px">Pending (${pending.length})</h3>
    ${pending.length?pendingCards:'<div class="hint" style="margin-bottom:24px">No pending approvals.</div>'}
    <h3 style="font-family:'Archivo',sans-serif;font-weight:600;margin-top:24px;margin-bottom:12px">Recent Decisions</h3>
    ${decided.length?`<table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:var(--bg2);text-align:left">
        <th style="padding:8px 10px">Action</th><th style="padding:8px 10px">Agent</th><th style="padding:8px 10px">Status</th><th style="padding:8px 10px">Decided</th>
      </tr></thead>
      <tbody>${decided.map(a=>`<tr>
        <td style="padding:8px 10px">${esc(a.action)}</td>
        <td style="padding:8px 10px">${esc(a.agent_id)}</td>
        <td style="padding:8px 10px"><span style="color:${a.status==='approved'?'var(--seal)':'var(--brick)'}">${esc(a.status)}</span></td>
        <td style="padding:8px 10px;font-size:11px;color:var(--muted)">${a.created_at?new Date(a.created_at).toLocaleString():''}</td>
      </tr>`).join('')}
      </tbody>
    </table>`:'<div class="hint">No decisions yet.</div>'}
  </div>`;
}
async function decideApproval(id,decision){
  const note=decision==='rejected'?prompt('Reason for rejection (optional):',''):'';
  await fetch('/api/approvals/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision,note:note||''})});
  await renderApprovals();
}

/* ═══════════════ NOTIFICATIONS ═══════════════ */
let _notifOpen=false;
async function pollNotifications(){
  try{
    const r=await fetch('/api/notifications');
    const d=await r.json();
    const badge=document.getElementById('notif-badge');
    if(d.unread>0){badge.style.display='';badge.textContent=d.unread>9?'9+':d.unread;}
    else{badge.style.display='none';}
    window._notifData=d.notifications||[];
  }catch(e){}
}
function toggleNotifPanel(){
  const panel=document.getElementById('notif-panel');
  _notifOpen=!_notifOpen;
  if(!_notifOpen){panel.style.display='none';return;}
  const notifs=window._notifData||[];
  if(!notifs.length){
    panel.innerHTML='<div style="padding:20px;text-align:center;color:var(--muted)">No notifications</div>';
  }else{
    panel.innerHTML=`<div style="padding:8px 12px;border-bottom:1px solid #3d2a1a;display:flex;justify-content:space-between;align-items:center">
      <span style="font-weight:600;font-size:12px">Notifications</span>
      <button class="btn ghost" style="font-size:10px;padding:2px 6px" onclick="markAllRead()">Mark all read</button>
    </div>`+notifs.map(n=>`<div style="padding:10px 12px;border-bottom:1px solid #2a1f0f;${n.is_read?'opacity:.6':''}cursor:pointer" onclick="dismissNotif('${n.id}')">
      <div style="font-weight:${n.is_read?'400':'600'};font-size:12px;margin-bottom:2px">${esc(n.title)}</div>
      ${n.body?`<div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(n.body)}</div>`:''}
      <div style="font-size:10px;color:var(--faint);margin-top:2px">${n.created_at?new Date(n.created_at).toLocaleString():''}</div>
    </div>`).join('');
  }
  panel.style.display='block';
}
async function markAllRead(){
  await fetch('/api/notifications/read',{method:'POST'});
  document.getElementById('notif-badge').style.display='none';
  (window._notifData||[]).forEach(n=>n.is_read=true);
  toggleNotifPanel();toggleNotifPanel();
}
async function dismissNotif(id){
  await fetch('/api/notifications/'+id,{method:'DELETE'});
  window._notifData=(window._notifData||[]).filter(n=>n.id!==id);
  pollNotifications();
  if(_notifOpen){toggleNotifPanel();toggleNotifPanel();}
}
setInterval(pollNotifications,15000);

/* ═══════════════ ANALYTICS DASHBOARD ═══════════════ */
async function renderAnalytics(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="text-align:center;padding:60px;color:var(--muted)">Loading analytics...</div>';
  let data;
  try{
    const r=await fetch('/api/analytics?days=30');
    data=await r.json();
  }catch(e){
    root.innerHTML='<div style="padding:40px;color:#dc2626">Failed to load analytics</div>';
    return;
  }
  const s=data.summary;

  // ── SVG bar chart helper ──
  function barChart(series, labelKey, valueKey, color, w, h){
    if(!series.length) return '<div style="color:var(--muted);font-size:12px;padding:20px">No data yet</div>';
    const max=Math.max(...series.map(d=>d[valueKey]),1);
    const bw=Math.max(4, Math.floor((w-20)/series.length)-2);
    const bars=series.map((d,i)=>{
      const bh=Math.max(1, d[valueKey]/max*(h-30));
      const x=10+i*(bw+2);
      const y=h-25-bh;
      const label=d[labelKey].slice(5); // trim year from date
      return `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="${color}" rx="1"><title>${d[labelKey]}: ${d[valueKey].toLocaleString()}</title></rect>${i%Math.max(1,Math.floor(series.length/6))===0?`<text x="${x}" y="${h-6}" font-size="9" fill="var(--muted)" font-family="IBM Plex Mono">${label}</text>`:''}`;
    }).join('');
    return `<svg width="100%" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMinYMid meet">${bars}</svg>`;
  }

  // ── Donut chart helper ──
  function donut(obj, colors, size){
    const entries=Object.entries(obj);
    if(!entries.length) return '<div style="color:var(--muted);font-size:12px;padding:20px">No data</div>';
    const total=entries.reduce((s,e)=>s+e[1],0)||1;
    const r=size/2-8, cx=size/2, cy=size/2;
    let cum=0;
    const arcs=entries.map(([k,v],i)=>{
      const pct=v/total;
      const start=cum*2*Math.PI-Math.PI/2;
      cum+=pct;
      const end=cum*2*Math.PI-Math.PI/2;
      const large=pct>0.5?1:0;
      const x1=cx+r*Math.cos(start), y1=cy+r*Math.sin(start);
      const x2=cx+r*Math.cos(end), y2=cy+r*Math.sin(end);
      const c=colors[i%colors.length];
      return pct>0.001?`<path d="M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z" fill="${c}"><title>${k}: ${v} (${(pct*100).toFixed(1)}%)</title></path>`:'';
    }).join('');
    // Inner circle for donut effect
    const inner=`<circle cx="${cx}" cy="${cy}" r="${r*0.55}" fill="var(--card)"/>`;
    const legend=entries.map(([k,v],i)=>`<div style="display:flex;align-items:center;gap:6px;font-size:11px"><div style="width:10px;height:10px;border-radius:2px;background:${colors[i%colors.length]}"></div><span style="color:var(--muted)">${esc(k)}</span><span style="margin-left:auto;font-family:'IBM Plex Mono';color:var(--fg)">${v}</span></div>`).join('');
    return `<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap"><svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${arcs}${inner}<text x="${cx}" y="${cy+4}" text-anchor="middle" font-size="14" font-weight="600" fill="var(--fg)" font-family="IBM Plex Mono">${total}</text></svg><div style="display:flex;flex-direction:column;gap:4px;min-width:120px">${legend}</div></div>`;
  }

  const donutColors=['#f97316','#22c55e','#ef4444','#3b82f6','#a855f7','#eab308','#06b6d4','#ec4899'];

  root.innerHTML=`
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
      <div>
        <h2 style="font-family:'Archivo';font-size:20px;font-weight:700;color:var(--fg);margin:0">Usage Analytics</h2>
        <div style="font-size:12px;color:var(--muted);margin-top:2px">Last ${data.period_days} days</div>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn ghost" onclick="window._analyticsDays=7;renderAnalytics()" style="font-size:10px;padding:4px 10px">7d</button>
        <button class="btn ghost" onclick="window._analyticsDays=30;renderAnalytics()" style="font-size:10px;padding:4px 10px">30d</button>
        <button class="btn ghost" onclick="window._analyticsDays=90;renderAnalytics()" style="font-size:10px;padding:4px 10px">90d</button>
      </div>
    </div>

    <!-- KPI Row -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px">
      <div class="card" style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;font-family:'Archivo';color:var(--accent)">${s.total_runs.toLocaleString()}</div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:.06em;margin-top:4px">TOTAL RUNS</div>
      </div>
      <div class="card" style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;font-family:'Archivo';color:#22c55e">${s.success_rate}%</div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:.06em;margin-top:4px">SUCCESS RATE</div>
      </div>
      <div class="card" style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;font-family:'Archivo';color:var(--fg)">${s.total_tokens>=1e6?(s.total_tokens/1e6).toFixed(1)+'M':s.total_tokens>=1e3?(s.total_tokens/1e3).toFixed(1)+'K':s.total_tokens}</div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:.06em;margin-top:4px">TOTAL TOKENS</div>
      </div>
      <div class="card" style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;font-family:'Archivo';color:var(--fg)">${s.avg_tokens_per_run.toLocaleString()}</div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:.06em;margin-top:4px">AVG TOKENS/RUN</div>
      </div>
      <div class="card" style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;font-family:'Archivo';color:var(--fg)">${s.avg_latency_s}s</div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:.06em;margin-top:4px">AVG LATENCY</div>
      </div>
      <div class="card" style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;font-family:'Archivo';color:var(--fg)">${s.p95_latency_s}s</div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:.06em;margin-top:4px">P95 LATENCY</div>
      </div>
    </div>

    <!-- Charts Row -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:12px;letter-spacing:.04em">Runs per Day</div>
        ${barChart(data.runs_by_day, 'date', 'count', '#f97316', 400, 160)}
      </div>
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:12px;letter-spacing:.04em">Token Usage per Day</div>
        ${barChart(data.tokens_by_day, 'date', 'tokens', '#3b82f6', 400, 160)}
      </div>
    </div>

    <!-- Breakdown Row -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:12px;letter-spacing:.04em">Outcomes</div>
        ${donut(data.outcomes, donutColors, 120)}
      </div>
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:12px;letter-spacing:.04em">Providers</div>
        ${donut(data.providers, ['#f97316','#3b82f6','#22c55e','#a855f7','#eab308'], 120)}
      </div>
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:12px;letter-spacing:.04em">Models</div>
        ${donut(data.models, ['#06b6d4','#ec4899','#f97316','#22c55e','#a855f7'], 120)}
      </div>
    </div>

    <!-- Top Agents Table -->
    <div class="card" style="padding:16px">
      <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:12px;letter-spacing:.04em">Top Agents by Usage</div>
      ${data.top_agents.length?`
      <div style="overflow-x:auto">
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--line)">
          <th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;letter-spacing:.06em">AGENT</th>
          <th style="text-align:right;padding:6px 8px;color:var(--muted);font-size:10px;letter-spacing:.06em">RUNS</th>
          <th style="text-align:right;padding:6px 8px;color:var(--muted);font-size:10px;letter-spacing:.06em">TOKENS</th>
          <th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;letter-spacing:.06em">USAGE BAR</th>
        </tr></thead>
        <tbody>
        ${data.top_agents.map((a,i)=>{
          const maxRuns=data.top_agents[0].runs||1;
          const pct=Math.round(a.runs/maxRuns*100);
          return `<tr style="border-bottom:1px solid var(--line)">
            <td style="padding:8px;font-family:'IBM Plex Mono';color:var(--fg)">${esc(a.name)}</td>
            <td style="padding:8px;text-align:right;font-family:'IBM Plex Mono';color:var(--accent);font-weight:600">${a.runs}</td>
            <td style="padding:8px;text-align:right;font-family:'IBM Plex Mono';color:var(--muted)">${a.tokens>=1e6?(a.tokens/1e6).toFixed(1)+'M':a.tokens>=1e3?(a.tokens/1e3).toFixed(1)+'K':a.tokens}</td>
            <td style="padding:8px"><div style="background:var(--line);border-radius:3px;height:8px;width:100%;overflow:hidden"><div style="height:100%;width:${pct}%;background:linear-gradient(90deg,#f97316,#fbbf24);border-radius:3px"></div></div></td>
          </tr>`;
        }).join('')}
        </tbody>
      </table></div>`:'<div style="color:var(--muted);font-size:12px;padding:20px;text-align:center">No agent data yet. Run some agents to see usage analytics.</div>'}
    </div>

    <!-- Token Breakdown -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:8px;letter-spacing:.04em">Token Breakdown</div>
        <div style="display:flex;gap:20px;margin-top:12px">
          <div><div style="font-size:18px;font-weight:700;font-family:'Archivo';color:#3b82f6">${s.input_tokens>=1e6?(s.input_tokens/1e6).toFixed(2)+'M':s.input_tokens>=1e3?(s.input_tokens/1e3).toFixed(1)+'K':s.input_tokens}</div><div style="font-size:10px;color:var(--muted);margin-top:2px">INPUT</div></div>
          <div><div style="font-size:18px;font-weight:700;font-family:'Archivo';color:#f97316">${s.output_tokens>=1e6?(s.output_tokens/1e6).toFixed(2)+'M':s.output_tokens>=1e3?(s.output_tokens/1e3).toFixed(1)+'K':s.output_tokens}</div><div style="font-size:10px;color:var(--muted);margin-top:2px">OUTPUT</div></div>
        </div>
        <div style="display:flex;height:12px;border-radius:6px;overflow:hidden;margin-top:12px;background:var(--line)">
          <div style="width:${s.total_tokens?Math.round(s.input_tokens/s.total_tokens*100):50}%;background:#3b82f6"></div>
          <div style="width:${s.total_tokens?Math.round(s.output_tokens/s.total_tokens*100):50}%;background:#f97316"></div>
        </div>
      </div>
      <div class="card" style="padding:16px">
        <div style="font-size:12px;font-weight:600;color:var(--fg);margin-bottom:8px;letter-spacing:.04em">Run Outcomes</div>
        <div style="display:flex;gap:20px;margin-top:12px">
          <div><div style="font-size:18px;font-weight:700;font-family:'Archivo';color:#22c55e">${s.completed}</div><div style="font-size:10px;color:var(--muted);margin-top:2px">COMPLETED</div></div>
          <div><div style="font-size:18px;font-weight:700;font-family:'Archivo';color:#eab308">${s.escalated}</div><div style="font-size:10px;color:var(--muted);margin-top:2px">ESCALATED</div></div>
          <div><div style="font-size:18px;font-weight:700;font-family:'Archivo';color:#ef4444">${s.errored}</div><div style="font-size:10px;color:var(--muted);margin-top:2px">ERRORS</div></div>
        </div>
        <div style="display:flex;height:12px;border-radius:6px;overflow:hidden;margin-top:12px;background:var(--line)">
          ${s.total_runs?`<div style="width:${Math.round(s.completed/s.total_runs*100)}%;background:#22c55e"></div><div style="width:${Math.round(s.escalated/s.total_runs*100)}%;background:#eab308"></div><div style="width:${Math.round(s.errored/s.total_runs*100)}%;background:#ef4444"></div>`:''}
        </div>
      </div>
    </div>
  `;
}

/* ═══════════════ ADAPTIVE RUNTIME (CAR) ═══════════════ */
async function renderRuntime(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div><div style="margin-top:12px;color:var(--muted);font-size:13px">Loading Adaptive Runtime...</div></div></div>';
  let health,pressure,state,audit;
  try{
    [health,pressure,state,audit]=await Promise.all([
      fetch('/api/car/health').then(r=>r.json()),
      fetch('/api/car/pressure').then(r=>r.json()),
      fetch('/api/car/state').then(r=>r.json()),
      fetch('/api/car/audit?limit=25').then(r=>r.json()),
    ]);
  }catch(e){
    root.innerHTML='<div style="padding:24px"><div class="stat-card" style="padding:32px;text-align:center;color:var(--muted)">Could not load runtime data.</div></div>';
    return;
  }

  const hc=health.health||'unknown';
  const hColor=hc==='healthy'?'#22c55e':hc==='elevated'?'#eab308':'#ef4444';
  const hIcon=hc==='healthy'?'●':hc==='elevated'?'◐':'◉';

  // Fingerprint cards
  const fps=state.fingerprints||{};
  const fpIds=Object.keys(fps);
  let fpCards='';
  if(fpIds.length===0){
    fpCards='<div style="color:var(--muted);font-size:13px;padding:16px">No agent fingerprints yet. Run some agents to build behavioral profiles.</div>';
  }else{
    fpCards=fpIds.map(aid=>{
      const f=fps[aid];
      const d=f.drift||{};
      const dc=d.direction==='stable'?'#22c55e':d.direction==='elevated'?'#eab308':'#ef4444';
      const envs=f.envelopes||{};
      const envHTML=Object.keys(envs).map(k=>{
        const e=envs[k];
        return '<div style="display:flex;justify-content:space-between;font-size:11px;padding:2px 0"><span style="color:var(--muted)">'+esc(k)+'</span><span>med '+Math.round(e.median)+' · p95 '+Math.round(e.p95)+' · CV '+e.cv+'</span></div>';
      }).join('');
      return '<div class="stat-card" style="padding:16px;margin-bottom:8px">'+
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'+
          '<div style="font-weight:600;font-size:13px">'+esc(aid)+'</div>'+
          '<div style="display:flex;align-items:center;gap:8px">'+
            '<span style="font-size:10px;padding:2px 8px;border-radius:10px;background:'+dc+'22;color:'+dc+';font-weight:600">'+esc(d.direction||'--')+'</span>'+
            '<span style="font-size:11px;color:var(--muted)">drift '+((d.score||0)*100).toFixed(0)+'%</span>'+
          '</div>'+
        '</div>'+
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px">'+
          '<div><div style="font-size:10px;color:var(--muted)">RUNS</div><div style="font-size:15px;font-weight:600">'+(f.total_runs||0)+'</div></div>'+
          '<div><div style="font-size:10px;color:var(--muted)">SUCCESS</div><div style="font-size:15px;font-weight:600">'+((f.success_rate||0)*100).toFixed(1)+'%</div></div>'+
          '<div><div style="font-size:10px;color:var(--muted)">TOKENS</div><div style="font-size:15px;font-weight:600">'+(f.total_tokens||0).toLocaleString()+'</div></div>'+
          '<div><div style="font-size:10px;color:var(--muted)">COST</div><div style="font-size:15px;font-weight:600">$'+(f.total_cost||0).toFixed(2)+'</div></div>'+
        '</div>'+
        '<div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px">'+
          '<div style="font-size:10px;font-weight:600;letter-spacing:.06em;color:var(--muted);margin-bottom:4px">BEHAVIORAL ENVELOPES</div>'+
          (envHTML||'<div style="font-size:11px;color:var(--muted)">Building... (need 5+ runs)</div>')+
        '</div>'+
      '</div>';
    }).join('');
  }

  // Router leaderboards
  const routers=state.routers||{};
  const taskTypes=Object.keys(routers);
  let lbHTML='';
  if(taskTypes.length===0){
    lbHTML='<div style="color:var(--muted);font-size:13px;padding:16px">No routing data yet.</div>';
  }else{
    lbHTML=taskTypes.map(tt=>{
      const r=routers[tt];
      const arms=Object.values(r.arms||{}).filter(a=>a.total_pulls>0).sort((a,b)=>(b.total_successes/(b.total_pulls||1))-(a.total_successes/(a.total_pulls||1)));
      if(!arms.length)return '';
      return '<div style="margin-bottom:16px">'+
        '<div style="font-size:11px;font-weight:600;letter-spacing:.06em;color:var(--muted);margin-bottom:6px">TASK: '+esc(tt.toUpperCase())+'</div>'+
        '<table style="width:100%;font-size:12px;border-collapse:collapse">'+
        '<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left;padding:4px 8px;color:var(--muted);font-size:10px">PROVIDER</th><th style="text-align:left;padding:4px 8px;color:var(--muted);font-size:10px">MODEL</th><th style="text-align:right;padding:4px 8px;color:var(--muted);font-size:10px">PULLS</th><th style="text-align:right;padding:4px 8px;color:var(--muted);font-size:10px">SUCCESS</th><th style="text-align:right;padding:4px 8px;color:var(--muted);font-size:10px">AVG LAT</th><th style="text-align:right;padding:4px 8px;color:var(--muted);font-size:10px">AVG COST</th></tr>'+
        arms.map((a,i)=>{
          const sr=a.total_pulls?((a.total_successes/a.total_pulls)*100).toFixed(0):'--';
          const bg=i===0?'rgba(34,197,94,.06)':'transparent';
          return '<tr style="background:'+bg+'"><td style="padding:4px 8px">'+esc(a.provider)+'</td><td style="padding:4px 8px;font-family:var(--mono)">'+esc(a.model)+'</td><td style="text-align:right;padding:4px 8px">'+a.total_pulls+'</td><td style="text-align:right;padding:4px 8px;font-weight:600">'+sr+'%</td><td style="text-align:right;padding:4px 8px">'+Math.round(a.avg_latency)+'ms</td><td style="text-align:right;padding:4px 8px">$'+a.avg_cost.toFixed(4)+'</td></tr>';
        }).join('')+
        '</table></div>';
    }).join('');
  }

  // Audit trail
  const auditRows=(audit||[]).slice().reverse().map(e=>
    '<div style="display:flex;gap:12px;padding:5px 0;border-bottom:1px solid var(--border);font-size:11px">'+
      '<span style="color:var(--muted);white-space:nowrap;font-family:var(--mono)">'+esc(e.ts||'')+'</span>'+
      '<span style="color:#f97316;font-weight:600;min-width:120px">'+esc(e.action||'')+'</span>'+
      '<span style="flex:1;color:var(--fg)">'+esc(e.detail||'')+'</span>'+
      '<span style="color:var(--muted);font-family:var(--mono);font-size:10px">'+esc(e.sig||'')+'</span>'+
    '</div>'
  ).join('');

  // Pressure
  const pr=pressure.averages||{};
  const pp=pressure.primary_pressure||'--';

  // Policy
  const pol=state.prediction_policy||{};
  const pw=pol.weights||{};

  root.innerHTML=`
    <div style="padding:24px">
      <div style="display:flex;align-items:center;gap:16px;margin-bottom:24px">
        <div>
          <div style="font-size:20px;font-weight:700;font-family:var(--head)">Adaptive Runtime</div>
          <div style="font-size:12px;color:var(--muted)">Behavioral fingerprinting · Adaptive routing · Run prediction · Governed audit</div>
        </div>
        <div style="margin-left:auto;display:flex;align-items:center;gap:12px">
          <span style="font-size:24px;color:${hColor}">${hIcon}</span>
          <div>
            <div style="font-size:14px;font-weight:700;color:${hColor}">${esc((hc||'').toUpperCase())}</div>
            <div style="font-size:10px;color:var(--muted)">${health.total_agents||0} agents · ${health.total_runs||0} runs · $${(health.total_cost||0).toFixed(2)}</div>
          </div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
        <div class="stat-card" style="padding:14px">
          <div style="font-size:10px;letter-spacing:.06em;color:var(--muted);margin-bottom:4px">FLEET SUCCESS</div>
          <div style="font-size:22px;font-weight:700">${health.fleet_success_rate!==null?((health.fleet_success_rate||0)*100).toFixed(1)+'%':'--'}</div>
        </div>
        <div class="stat-card" style="padding:14px">
          <div style="font-size:10px;letter-spacing:.06em;color:var(--muted);margin-bottom:4px">FLEET CV</div>
          <div style="font-size:22px;font-weight:700">${(health.fleet_cv||0).toFixed(3)}</div>
          <div style="font-size:10px;color:var(--muted)">dispersion</div>
        </div>
        <div class="stat-card" style="padding:14px">
          <div style="font-size:10px;letter-spacing:.06em;color:var(--muted);margin-bottom:4px">FAILURE TAIL</div>
          <div style="font-size:22px;font-weight:700">${((health.failure_tail_share||0)*100).toFixed(1)}%</div>
          <div style="font-size:10px;color:var(--muted)">outlier share</div>
        </div>
        <div class="stat-card" style="padding:14px">
          <div style="font-size:10px;letter-spacing:.06em;color:var(--muted);margin-bottom:4px">DRIFT PRESSURE</div>
          <div style="font-size:22px;font-weight:700">${esc(pp)}</div>
          <div style="font-size:10px;color:var(--muted)">${Object.entries(pr).map(([k,v])=>k+': '+v).join(' · ')}</div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
        <div>
          <div style="font-size:13px;font-weight:700;margin-bottom:10px;font-family:var(--head)">Agent Fingerprints</div>
          <div style="max-height:400px;overflow-y:auto">${fpCards}</div>
        </div>
        <div>
          <div style="font-size:13px;font-weight:700;margin-bottom:10px;font-family:var(--head)">Routing Leaderboard</div>
          <div class="stat-card" style="padding:16px;max-height:400px;overflow-y:auto">${lbHTML}</div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
        <div>
          <div style="font-size:13px;font-weight:700;margin-bottom:10px;font-family:var(--head)">Audit Trail</div>
          <div class="stat-card" style="padding:12px;max-height:300px;overflow-y:auto;font-family:var(--mono)">
            ${auditRows||'<div style="color:var(--muted);font-size:12px">No audit entries yet.</div>'}
          </div>
        </div>
        <div>
          <div style="font-size:13px;font-weight:700;margin-bottom:10px;font-family:var(--head)">Prediction Policy</div>
          <div class="stat-card" style="padding:16px">
            <div style="font-size:10px;color:var(--muted);margin-bottom:8px">VERSION ${esc(pol.version||'--')}</div>
            <div style="font-size:11px;margin-bottom:4px"><strong>Weights</strong></div>
            <div style="font-size:12px;margin-bottom:8px;padding-left:8px">
              Complexity: ${((pw.prompt_complexity||0)*100).toFixed(0)}%<br/>
              Historical Fit: ${((pw.historical_fit||0)*100).toFixed(0)}%<br/>
              Provider Health: ${((pw.provider_health||0)*100).toFixed(0)}%
            </div>
            <div style="font-size:11px;margin-bottom:4px"><strong>Thresholds</strong></div>
            <div style="font-size:12px;padding-left:8px">
              High confidence: ≥${(pol.thresholds||{}).high_confidence||75}<br/>
              Medium confidence: ≥${(pol.thresholds||{}).medium_confidence||50}
            </div>
          </div>
          ${health.drifting_agents&&health.drifting_agents.length?
            '<div style="margin-top:12px"><div style="font-size:13px;font-weight:700;margin-bottom:8px;font-family:var(--head);color:#ef4444">Drifting Agents</div><div class="stat-card" style="padding:12px">'+
            health.drifting_agents.map(a=>'<div style="font-size:12px;padding:3px 0;color:#ef4444">◉ '+esc(a)+'</div>').join('')+
            '</div></div>':''}
        </div>
      </div>
    </div>
  `;
}

/* ═══════════════ OBSERVABILITY (metrics · traces · logs · alerts · SLOs · health) ═══════════════ */
let obsSubTab='overview';
async function renderObservability(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div><div style="margin-top:12px;color:var(--muted);font-size:13px">Loading observability...</div></div></div>';
  let s;
  try{ s=await fetch('/api/observability/summary').then(r=>r.json()); }
  catch(e){ root.innerHTML='<div style="padding:24px"><div class="stat-card" style="padding:32px;text-align:center;color:var(--muted)">Could not load observability data.</div></div>'; return; }
  const sub=['overview','traces','logs','alerts','slos','health'];
  const tabRow='<div class="tab-row" style="margin-bottom:16px">'+sub.map(t=>
    `<button class="tab-btn${obsSubTab===t?' active':''}" onclick="obsSubTab='${t}';renderObservability()">${t.toUpperCase()}</button>`).join('')+'</div>';
  let body='';
  if(obsSubTab==='overview'){
    const h=s.health||{}, al=s.alerts||{}, sl=(s.slos||{}).summary||{}, lo=s.logs||{}, tr=s.traces||{};
    const hColor=h.status==='healthy'?'#22c55e':h.status==='degraded'?'#eab308':'#ef4444';
    body=`<div class="grid-4" style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px">
      ${statCard('Fleet Health',(h.avg_score!=null?h.avg_score:100)+'<span style="font-size:13px;color:var(--muted)">/100</span>',h.status||'healthy',hColor)}
      ${statCard('Firing Alerts',al.firing||0,(al.total_rules||0)+' rules',al.firing>0?'#ef4444':'#22c55e')}
      ${statCard('SLOs At Risk',sl.at_risk||0,(sl.total_slos||0)+' tracked',sl.at_risk>0?'#eab308':'#22c55e')}
      ${statCard('Traces',tr.total_traces||0,(tr.total_spans||0)+' spans','#f97316')}
    </div>
    <div class="grid-2" style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div class="stat-card"><div class="card-title">Log Volume</div>
        ${Object.entries(lo.level_counts||{}).map(([k,v])=>`<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line)"><span style="text-transform:uppercase;font-size:11px;color:${k==='error'||k==='fatal'?'#ef4444':k==='warn'?'#eab308':'var(--muted)'}">${esc(k)}</span><span style="font-family:monospace">${v}</span></div>`).join('')||'<div style="color:var(--muted);font-size:12px">No logs yet</div>'}
      </div>
      <div class="stat-card"><div class="card-title">Health by Status</div>
        ${Object.entries(h.by_status||{}).map(([k,v])=>`<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line)"><span style="text-transform:capitalize;font-size:12px">${esc(k)}</span><span style="font-family:monospace">${v}</span></div>`).join('')||'<div style="color:var(--muted);font-size:12px">No agents scored yet</div>'}
      </div>
    </div>`;
  } else if(obsSubTab==='traces'){
    const d=await fetch('/api/observability/traces?limit=40').then(r=>r.json()).catch(()=>({traces:[]}));
    const rows=(d.traces||[]).map(t=>`<tr onclick="showTrace('${t.trace_id}')" style="cursor:pointer">
      <td style="font-family:monospace;font-size:11px">${esc(t.trace_id.slice(0,12))}</td>
      <td>${t.span_count}</td><td>${(t.total_duration_ms||0).toFixed(1)}ms</td>
      <td>${(t.services||[]).join(', ')}</td>
      <td>${t.has_errors?'<span style="color:#ef4444">● error</span>':'<span style="color:#22c55e">● ok</span>'}</td></tr>`).join('');
    body=`<div class="stat-card"><div class="card-title">Distributed Traces</div>
      <table class="data-table"><thead><tr><th>Trace ID</th><th>Spans</th><th>Duration</th><th>Services</th><th>Status</th></tr></thead>
      <tbody>${rows||'<tr><td colspan=5 style="color:var(--muted);padding:16px">No traces captured yet. Run an agent to generate traces.</td></tr>'}</tbody></table>
      <div id="trace-detail"></div></div>`;
  } else if(obsSubTab==='logs'){
    const d=await fetch('/api/observability/logs?limit=100').then(r=>r.json()).catch(()=>({entries:[]}));
    const eg=await fetch('/api/observability/logs/errors').then(r=>r.json()).catch(()=>({error_groups:[]}));
    const errRows=(eg.error_groups||[]).slice(0,8).map(g=>`<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line)"><span style="font-size:12px;color:#ef4444">${esc(g.message)}</span><span style="font-family:monospace;color:var(--muted)">×${g.count}</span></div>`).join('');
    const logRows=(d.entries||[]).map(e=>{
      const c=e.level==='error'||e.level==='fatal'?'#ef4444':e.level==='warn'?'#eab308':'var(--muted)';
      return `<div style="padding:5px 0;border-bottom:1px solid var(--line);font-family:monospace;font-size:11px"><span style="color:${c};text-transform:uppercase">[${esc(e.level)}]</span> <span style="color:var(--muted)">${esc((e.timestamp||'').slice(11,19))}</span> ${esc(e.message)}</div>`;
    }).join('');
    body=`<input id="logSearch" placeholder="Search logs..." onkeydown="if(event.key==='Enter')searchLogs()" style="width:100%;padding:9px 12px;margin-bottom:14px;background:var(--panel);border:1px solid var(--line);border-radius:6px;color:var(--fg)">
    ${errRows?`<div class="stat-card" style="margin-bottom:14px"><div class="card-title">Top Error Groups (auto-fingerprinted)</div>${errRows}</div>`:''}
    <div class="stat-card"><div class="card-title">Live Log Stream</div><div id="logStream" style="max-height:440px;overflow-y:auto">${logRows||'<div style="color:var(--muted);font-size:12px">No logs yet</div>'}</div></div>`;
  } else if(obsSubTab==='alerts'){
    const d=await fetch('/api/observability/alerts').then(r=>r.json()).catch(()=>({alerts:[],rules:[]}));
    const alertRows=(d.alerts||[]).slice(0,20).map(a=>{
      const c=a.severity==='critical'||a.severity==='fatal'?'#ef4444':a.severity==='warning'?'#eab308':'#3b82f6';
      const st=a.status==='firing'?`<button class="btn-sm" onclick="ackAlert('${a.id}')">Acknowledge</button>`:`<span style="color:var(--muted);font-size:11px">${esc(a.status)}</span>`;
      return `<tr><td><span style="color:${c}">●</span> ${esc(a.rule_name)}</td><td style="font-size:12px">${esc(a.message)}</td><td>${esc(a.severity)}</td><td>${st}</td></tr>`;
    }).join('');
    const ruleRows=(d.rules||[]).map(r=>`<tr><td>${esc(r.name)}</td><td style="font-family:monospace;font-size:11px">${esc(r.metric)} ${esc(r.condition)} ${r.threshold}</td><td>${esc(r.severity)}</td><td>${r.enabled?'<span style="color:#22c55e">enabled</span>':'<span style="color:var(--muted)">off</span>'}</td></tr>`).join('');
    body=`<div class="stat-card" style="margin-bottom:14px"><div class="card-title">Active Alerts</div>
      <table class="data-table"><thead><tr><th>Rule</th><th>Message</th><th>Severity</th><th>Action</th></tr></thead>
      <tbody>${alertRows||'<tr><td colspan=4 style="color:#22c55e;padding:16px">✓ No active alerts — all systems nominal</td></tr>'}</tbody></table></div>
    <div class="stat-card"><div class="card-title">Alert Rules</div>
      <table class="data-table"><thead><tr><th>Name</th><th>Condition</th><th>Severity</th><th>State</th></tr></thead>
      <tbody>${ruleRows}</tbody></table></div>`;
  } else if(obsSubTab==='slos'){
    const d=await fetch('/api/observability/slos').then(r=>r.json()).catch(()=>({slos:[]}));
    body='<div class="grid-2" style="display:grid;grid-template-columns:1fr 1fr;gap:14px">'+(d.slos||[]).map(sl=>{
      const bp=Math.round((sl.error_budget_remaining||0)*100);
      const bc=bp>50?'#22c55e':bp>20?'#eab308':'#ef4444';
      return `<div class="stat-card"><div style="display:flex;justify-content:space-between"><span style="font-weight:600">${esc(sl.name)}</span><span style="font-family:monospace;color:${sl.is_healthy?'#22c55e':'#ef4444'}">${esc(sl.current_sli_pct)}</span></div>
      <div style="font-size:11px;color:var(--muted);margin:6px 0">${esc(sl.description||'')}</div>
      <div style="font-size:11px;margin-bottom:4px">Target ${esc(sl.target_pct)} · Burn rate ${sl.burn_rate}×</div>
      <div style="background:var(--line);height:8px;border-radius:4px;overflow:hidden"><div style="width:${bp}%;height:100%;background:${bc}"></div></div>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">Error budget: ${esc(sl.error_budget_remaining_pct)} remaining</div></div>`;
    }).join('')+'</div>';
  } else if(obsSubTab==='health'){
    const d=await fetch('/api/observability/health').then(r=>r.json()).catch(()=>({scores:[]}));
    body=`<div class="stat-card"><div class="card-title">Agent Health Scores (composite 0-100)</div>
      <table class="data-table"><thead><tr><th>Agent</th><th>Score</th><th>Status</th><th>Success</th><th>Latency</th><th>Uptime</th></tr></thead>
      <tbody>${(d.scores||[]).map(sc=>{
        const c=sc.status==='healthy'?'#22c55e':sc.status==='degraded'?'#eab308':'#ef4444';
        const dm=sc.dimensions||{};
        const nm=(AGENTS.find(a=>a.id===sc.agent_id)||{}).name||sc.agent_id;
        return `<tr><td>${esc(nm)}</td><td style="font-family:monospace;color:${c};font-weight:600">${sc.score}</td><td style="color:${c}">● ${esc(sc.status)}</td><td>${dm.success_rate||0}</td><td>${dm.latency||0}</td><td>${dm.uptime||0}</td></tr>`;
      }).join('')||'<tr><td colspan=6 style="color:var(--muted);padding:16px">No agents scored yet</td></tr>'}</tbody></table></div>`;
  }
  root.innerHTML='<div style="padding:24px"><h2 style="margin:0 0 4px">Observability</h2><div style="color:var(--muted);font-size:12px;margin-bottom:16px">Metrics, distributed tracing, log aggregation, alerting, and SLOs — the operational nervous system.</div>'+tabRow+body+'</div>';
}
function statCard(title,value,sub,color){
  return `<div class="stat-card"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">${esc(title)}</div>
    <div style="font-size:26px;font-weight:700;margin:6px 0;color:${color||'var(--fg)'}">${value}</div>
    <div style="font-size:11px;color:var(--muted)">${esc(sub||'')}</div></div>`;
}
async function showTrace(tid){
  const t=await fetch('/api/observability/traces/'+tid).then(r=>r.json()).catch(()=>null);
  if(!t)return;
  const max=t.total_duration_ms||1;
  const rows=(t.spans||[]).map(s=>{
    const off=t.start_time?0:0;
    const w=Math.max(2,((s.duration_ms||0)/max)*100);
    const c=s.status==='error'?'#ef4444':'#f97316';
    return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0"><span style="width:180px;font-size:11px;font-family:monospace">${esc(s.operation)}</span><div style="flex:1;background:var(--line);border-radius:3px;height:16px"><div style="width:${w}%;height:100%;background:${c};border-radius:3px"></div></div><span style="width:70px;text-align:right;font-size:11px;font-family:monospace">${(s.duration_ms||0).toFixed(1)}ms</span></div>`;
  }).join('');
  document.getElementById('trace-detail').innerHTML=`<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)"><div class="card-title">Waterfall — ${esc(tid.slice(0,12))}</div>${rows}</div>`;
}
async function ackAlert(id){ await fetch('/api/observability/alerts/'+id+'/acknowledge',{method:'POST'}); renderObservability(); }
async function searchLogs(){
  const q=document.getElementById('logSearch').value;
  const d=await fetch('/api/observability/logs?limit=100&q='+encodeURIComponent(q)).then(r=>r.json()).catch(()=>({entries:[]}));
  document.getElementById('logStream').innerHTML=(d.entries||[]).map(e=>{
    const c=e.level==='error'||e.level==='fatal'?'#ef4444':e.level==='warn'?'#eab308':'var(--muted)';
    return `<div style="padding:5px 0;border-bottom:1px solid var(--line);font-family:monospace;font-size:11px"><span style="color:${c};text-transform:uppercase">[${esc(e.level)}]</span> <span style="color:var(--muted)">${esc((e.timestamp||'').slice(11,19))}</span> ${esc(e.message)}</div>`;
  }).join('')||'<div style="color:var(--muted);font-size:12px">No matching logs</div>';
}

/* ═══════════════ USAGE & COST ═══════════════ */
async function renderUsage(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div></div></div>';
  let d;
  try{ d=await fetch('/api/usage/dashboard').then(r=>r.json()); }
  catch(e){ root.innerHTML='<div style="padding:24px"><div class="stat-card" style="padding:32px;text-align:center;color:var(--muted)">Could not load usage data.</div></div>'; return; }
  const t=d.totals||{};
  const cards=`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px">
    ${statCard('Total Spend','$'+(t.total_cost||0).toFixed(2),(t.total_runs||0)+' runs','#f97316')}
    ${statCard('Total Tokens',((t.total_tokens||0)/1000).toFixed(1)+'K','across fleet','#f97316')}
    ${statCard('Avg / Run','$'+(t.avg_cost_per_run||0).toFixed(4),'per execution','#f97316')}
    ${statCard('Projected / Mo','$'+(t.projected_monthly_cost||0).toFixed(2),'from last 7 days','#eab308')}
  </div>`;
  const prov=(d.by_provider||[]).map(p=>`<tr><td style="text-transform:capitalize">${esc(p.provider||'unknown')}</td><td>${((p.tokens||0)/1000).toFixed(1)}K</td><td>${p.runs}</td><td style="font-family:monospace">$${(p.cost||0).toFixed(4)}</td></tr>`).join('');
  const model=(d.by_model||[]).map(p=>`<tr><td style="font-family:monospace;font-size:11px">${esc(p.model||'unknown')}</td><td>${((p.tokens||0)/1000).toFixed(1)}K</td><td style="font-family:monospace">$${(p.cost||0).toFixed(4)}</td></tr>`).join('');
  const agent=(d.by_agent||[]).map(p=>{const nm=(AGENTS.find(a=>a.id===p.agent_id)||{}).name||p.agent_id;return `<tr><td>${esc(nm)}</td><td>${((p.tokens||0)/1000).toFixed(1)}K</td><td>${p.runs}</td><td style="font-family:monospace">$${(p.cost||0).toFixed(4)}</td></tr>`;}).join('');
  const user=(d.by_user||[]).map(p=>`<tr><td style="font-family:monospace;font-size:11px">${esc(p.user_id||'system')}</td><td>${((p.tokens||0)/1000).toFixed(1)}K</td><td>${p.runs}</td><td style="font-family:monospace">$${(p.cost||0).toFixed(4)}</td></tr>`).join('');
  // spark for daily
  const series=d.daily_series||[];
  const maxc=Math.max(0.0001,...series.map(s=>s.cost||0));
  const spark=series.map(s=>`<div title="${s.date}: $${(s.cost||0).toFixed(4)}" style="flex:1;background:#f97316;height:${Math.max(2,(s.cost/maxc)*60)}px;border-radius:2px 2px 0 0;opacity:.85"></div>`).join('');
  root.innerHTML=`<div style="padding:24px"><h2 style="margin:0 0 4px">Usage & Cost</h2>
    <div style="color:var(--muted);font-size:12px;margin-bottom:16px">Cost attribution across agents, users, providers, and models. Customers bring their own keys — Cortex gives them the visibility and guardrails.</div>
    ${cards}
    <div class="stat-card" style="margin-bottom:14px"><div class="card-title">Daily Spend (30 days)</div><div style="display:flex;gap:2px;align-items:flex-end;height:64px">${spark}</div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div class="stat-card"><div class="card-title">By Provider</div><table class="data-table"><thead><tr><th>Provider</th><th>Tokens</th><th>Runs</th><th>Cost</th></tr></thead><tbody>${prov||'<tr><td colspan=4 style="color:var(--muted);padding:12px">No usage yet</td></tr>'}</tbody></table></div>
      <div class="stat-card"><div class="card-title">By Model</div><table class="data-table"><thead><tr><th>Model</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>${model||'<tr><td colspan=3 style="color:var(--muted);padding:12px">No usage yet</td></tr>'}</tbody></table></div>
      <div class="stat-card"><div class="card-title">By Agent</div><table class="data-table"><thead><tr><th>Agent</th><th>Tokens</th><th>Runs</th><th>Cost</th></tr></thead><tbody>${agent||'<tr><td colspan=4 style="color:var(--muted);padding:12px">No usage yet</td></tr>'}</tbody></table></div>
      <div class="stat-card"><div class="card-title">By User</div><table class="data-table"><thead><tr><th>User</th><th>Tokens</th><th>Runs</th><th>Cost</th></tr></thead><tbody>${user||'<tr><td colspan=4 style="color:var(--muted);padding:12px">No usage yet</td></tr>'}</tbody></table></div>
    </div></div>`;
}

/* ═══════════════ TEAMS / WORKSPACES ═══════════════ */
async function renderTeams(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div></div></div>';
  const d=await fetch('/api/teams').then(r=>r.json()).catch(()=>({workspaces:[]}));
  const roles=await fetch('/api/teams/roles').then(r=>r.json()).catch(()=>({roles:[]}));
  const wsRows=(d.workspaces||[]).map(w=>`<tr onclick="showWorkspace('${w.id}')" style="cursor:pointer"><td>${esc(w.name)}</td><td style="font-family:monospace;font-size:11px">${esc(w.slug)}</td><td>${w.member_count} members</td><td><span style="text-transform:capitalize">${esc(w.my_role||'')}</span></td></tr>`).join('');
  const roleCards=(roles.roles||[]).map(r=>`<div style="padding:8px 0;border-bottom:1px solid var(--line)"><div style="display:flex;justify-content:space-between"><span style="font-weight:600">${esc(r.label)}</span><span style="font-size:11px;color:var(--muted)">level ${r.level}</span></div><div style="font-size:11px;color:var(--muted)">${esc(r.description)}</div></div>`).join('');
  root.innerHTML=`<div style="padding:24px"><h2 style="margin:0 0 4px">Teams & Workspaces</h2>
    <div style="color:var(--muted);font-size:12px;margin-bottom:16px">Multi-tenant workspaces with role-based access. Owners and admins invite members; Cortex manages access, not billing.</div>
    <div style="display:flex;gap:8px;margin-bottom:14px"><input id="wsName" placeholder="New workspace name" style="flex:1;padding:9px 12px;background:var(--panel);border:1px solid var(--line);border-radius:6px;color:var(--fg)"><button class="btn" onclick="createWorkspace()">Create Workspace</button></div>
    <div style="display:grid;grid-template-columns:2fr 1fr;gap:14px">
      <div class="stat-card"><div class="card-title">Your Workspaces</div><table class="data-table"><thead><tr><th>Name</th><th>Slug</th><th>Members</th><th>Your Role</th></tr></thead><tbody>${wsRows||'<tr><td colspan=4 style="color:var(--muted);padding:12px">No workspaces yet. Create one above.</td></tr>'}</tbody></table><div id="wsDetail"></div></div>
      <div class="stat-card"><div class="card-title">Roles</div>${roleCards}</div>
    </div></div>`;
}
async function createWorkspace(){
  const name=document.getElementById('wsName').value.trim(); if(!name)return;
  await fetch('/api/teams',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  renderTeams();
}
async function showWorkspace(wid){
  const d=await fetch('/api/teams/'+wid+'/members').then(r=>r.json()).catch(()=>({members:[],invites:[]}));
  const mem=(d.members||[]).map(m=>`<tr><td>${esc(m.email)}</td><td>${esc(m.role_label||m.role)}</td></tr>`).join('');
  const inv=(d.invites||[]).map(i=>`<tr><td>${esc(i.email)}</td><td>${esc(i.role)}</td><td>${esc(i.status)}</td></tr>`).join('');
  document.getElementById('wsDetail').innerHTML=`<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
    <div style="display:flex;gap:8px;margin-bottom:10px"><input id="invEmail" placeholder="email to invite" style="flex:1;padding:7px 10px;background:var(--panel);border:1px solid var(--line);border-radius:6px;color:var(--fg)"><select id="invRole" style="padding:7px;background:var(--panel);border:1px solid var(--line);border-radius:6px;color:var(--fg)"><option>operator</option><option>viewer</option><option>admin</option></select><button class="btn-sm" onclick="sendInvite('${wid}')">Invite</button></div>
    <div class="card-title">Members</div><table class="data-table"><tbody>${mem}</tbody></table>
    ${inv?`<div class="card-title" style="margin-top:10px">Pending Invites</div><table class="data-table"><tbody>${inv}</tbody></table>`:''}</div>`;
}
async function sendInvite(wid){
  const email=document.getElementById('invEmail').value.trim(); const role=document.getElementById('invRole').value;
  if(!email)return;
  const r=await fetch('/api/teams/'+wid+'/invite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,role})}).then(r=>r.json());
  if(r.token) alert('Invite created. Token (share once):\\n'+r.token);
  showWorkspace(wid);
}

/* ═══════════════ PLUGINS ═══════════════ */
async function renderPlugins(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div></div></div>';
  const d=await fetch('/api/plugins').then(r=>r.json()).catch(()=>({plugins:[]}));
  const cards=(d.plugins||[]).map(p=>`<div class="stat-card"><div style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:18px">${esc(p.icon||'🔌')} <span style="font-weight:600;font-size:14px">${esc(p.name)}</span></span><label class="switch"><input type="checkbox" ${p.enabled?'checked':''} onchange="togglePlugin('${p.name}',this.checked)"><span class="slider"></span></label></div>
    <div style="font-size:12px;color:var(--muted);margin:8px 0">${esc(p.description)}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap"><span class="chip">${esc(p.category)}</span>${(p.hooks||[]).map(h=>`<span class="chip" style="opacity:.7">${esc(h)}</span>`).join('')}</div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px">Used by ${p.install_count||0} agents · ${p.invoke_count||0} invocations</div></div>`).join('');
  root.innerHTML=`<div style="padding:24px"><h2 style="margin:0 0 4px">Plugins</h2>
    <div style="color:var(--muted);font-size:12px;margin-bottom:16px">Extend agents with tools, integrations, and lifecycle hooks. ${(d.stats||{}).enabled||0} of ${(d.stats||{}).total||0} enabled.</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px">${cards}</div></div>`;
}
async function togglePlugin(name,on){ await fetch('/api/plugins/'+encodeURIComponent(name)+'/'+(on?'enable':'disable'),{method:'POST'}); }

/* ═══════════════ AGENT MESH (comms) ═══════════════ */
async function renderComms(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div></div></div>';
  const [stats,hist,wf]=await Promise.all([
    fetch('/api/comms/stats').then(r=>r.json()).catch(()=>({})),
    fetch('/api/comms/history?limit=40').then(r=>r.json()).catch(()=>({messages:[]})),
    fetch('/api/comms/workflows').then(r=>r.json()).catch(()=>({workflows:[]})),
  ]);
  const msgs=(hist.messages||[]).map(m=>`<tr><td style="font-family:monospace;font-size:11px">${esc(m.from)}</td><td>→</td><td style="font-family:monospace;font-size:11px">${esc(m.to)}</td><td>${esc(m.type)}</td><td>${esc(m.status)}</td></tr>`).join('');
  const wfs=(wf.workflows||[]).map(w=>`<tr><td>${esc(w.name)}</td><td>${Object.keys(w.steps||{}).length} steps</td><td>${esc(w.status)}</td><td><button class="btn-sm" onclick="runWorkflow('${w.id}')">Run</button></td></tr>`).join('');
  root.innerHTML=`<div style="padding:24px"><h2 style="margin:0 0 4px">Agent Mesh</h2>
    <div style="color:var(--muted);font-size:12px;margin-bottom:16px">Agent-to-agent messaging and multi-agent workflows (DAGs). ${stats.total_messages||0} messages · ${stats.workflows||0} workflows.</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div class="stat-card"><div class="card-title">Message History</div><table class="data-table"><tbody>${msgs||'<tr><td colspan=5 style="color:var(--muted);padding:12px">No messages yet</td></tr>'}</tbody></table></div>
      <div class="stat-card"><div class="card-title">Workflows</div><table class="data-table"><tbody>${wfs||'<tr><td colspan=4 style="color:var(--muted);padding:12px">No workflows defined</td></tr>'}</tbody></table></div>
    </div></div>`;
}
async function runWorkflow(id){ await fetch('/api/comms/workflows/'+id+'/run',{method:'POST'}); renderComms(); }

/* ── system prompt: what the agent is told, and what it is actually sent ── */
async function loadPrompt(agentId){
  const d=await fetch('/api/agents/'+agentId+'/system-prompt')
    .then(r=>r.ok?r.json():null).catch(()=>null);
  const el=document.getElementById('prompt-panel');
  if(!el) return;
  if(!d){ el.innerHTML='<div class="hint" style="margin:0">Could not load the system prompt.</div>'; return; }
  el.innerHTML=`
    <div style="font-size:12px;color:var(--muted);margin-bottom:10px">
      Cortex holds the prompt, versions it with the rest of the config, and shows you
      exactly what the model receives — the preview below is built by the same code
      that runs the agent.
    </div>
    <textarea id="sp-text" class="ask" placeholder="What is this agent for, how should it behave, what must it never do?" style="min-height:150px;font-family:'IBM Plex Mono';font-size:12px">${esc(d.system_prompt||'')}</textarea>
    <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
      <button class="btn accent" style="padding:6px 12px;font-size:11px" onclick="savePrompt('${agentId}')">Save as new version</button>
      <button class="btn ghost" style="padding:6px 12px;font-size:11px" onclick="togglePreview()">Show what the model sees</button>
      <span style="font-size:10px;color:var(--faint)">currently v${d.version}</span>
      <span id="sp-msg" style="font-size:11px"></span>
    </div>
    <pre id="sp-preview" style="display:none;margin-top:10px;padding:12px;background:#faf8f5;border:1px solid var(--line);border-radius:6px;font-family:'IBM Plex Mono';font-size:11px;white-space:pre-wrap;color:var(--ink);max-height:340px;overflow:auto">${esc(d.assembled||'')}</pre>`;
}

function togglePreview(){
  const p=document.getElementById('sp-preview');
  if(p) p.style.display = p.style.display==='none' ? 'block' : 'none';
}

async function savePrompt(agentId){
  const m=document.getElementById('sp-msg');
  const r=await fetch('/api/agents/'+agentId+'/system-prompt',
    {method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({system_prompt:document.getElementById('sp-text').value})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ if(m){m.style.color='var(--brick)';m.textContent=j.detail||'Could not save.';} return; }
  const p=document.getElementById('sp-preview');
  const wasOpen = p && p.style.display!=='none';
  // Reload first — it replaces the panel, message element included — then say
  // what happened, or the confirmation is wiped the moment it is written.
  await loadPrompt(agentId);
  if(wasOpen) togglePreview();
  const m2=document.getElementById('sp-msg');
  if(m2){
    m2.style.color='var(--muted)';
    m2.textContent = j.unchanged ? 'No change — still v'+j.version : 'Saved as v'+j.version;
  }
  loadVersionPerf(agentId);
}

/* ── ownership & lifecycle: who is responsible, and where this agent is in its life ── */
const LIFECYCLE_META={
  draft:      {label:'Draft',      fg:'#6b6155', bg:'#ece5db', hint:'Being built. Not for anyone to depend on yet.'},
  active:     {label:'Active',     fg:'#2a5a30', bg:'#d4e0d6', hint:'Supported. Safe to build on.'},
  deprecated: {label:'Deprecated', fg:'#8a5a12', bg:'#f0e2c8', hint:'Still runs, but do not build anything new on it.'},
  retired:    {label:'Retired',    fg:'#7a3b32', bg:'#eedbd7', hint:'Out of service. Kept for the record.'}
};
function lifecycleBadge(stage,extra){
  const m=LIFECYCLE_META[stage]||LIFECYCLE_META.active;
  return `<span title="${esc(m.hint)}" style="font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;padding:2px 6px;border-radius:3px;color:${m.fg};background:${m.bg};${extra||''}">${m.label}</span>`;
}

async function loadOwnership(agentId){
  // Look the element up AFTER the fetch: renderControl fires this before it
  // writes its own markup, so #own-panel does not exist yet at call time.
  const d=await fetch('/api/agents/'+agentId+'/ownership').then(r=>r.ok?r.json():null).catch(()=>null);
  const el=document.getElementById('own-panel');
  if(!el) return;
  if(!d){ el.innerHTML='<div class="hint" style="margin:0">Could not load ownership.</div>'; return; }
  const stage=d.lifecycle||'active';
  const users=d.assignable_users||[];
  const opts=['<option value="">— unassigned —</option>'].concat(
    users.map(u=>`<option value="${u.id}" ${u.id===d.owner_id?'selected':''}>${esc(u.name)}</option>`)).join('');
  const stages=Object.keys(LIFECYCLE_META).map(k=>
    `<option value="${k}" ${k===stage?'selected':''}>${LIFECYCLE_META[k].label}</option>`).join('');
  const changed=d.lifecycle_changed_at
    ? new Date(d.lifecycle_changed_at).toLocaleString()
    : 'never changed';

  el.innerHTML=`
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div style="padding:12px;background:#faf8f5;border-radius:6px;border:1px solid var(--line)">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:8px">Responsible</div>
      <div class="form-group" style="margin-bottom:8px">
        <label>Owner</label>
        <select id="own-owner" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px;background:white">${opts}</select>
      </div>
      <div class="form-group" style="margin-bottom:8px">
        <label>Contact at 3am</label>
        <input id="own-contact" value="${esc(d.contact||'')}" placeholder="#oncall-channel or rota@team" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px">
      </div>
      <div class="form-group" style="margin-bottom:8px">
        <label>Account / Team</label>
        <input id="own-account" value="${esc(d.account||'')}" placeholder="e.g. Clinical Ops" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px">
      </div>
      <button class="btn accent" style="padding:6px 12px;font-size:11px" onclick="saveOwnership('${agentId}')">Save</button>
      ${d.owner_id?'':'<div style="font-size:10px;color:var(--brick);margin-top:6px">Nobody owns this agent.</div>'}
    </div>

    <div style="padding:12px;background:#faf8f5;border-radius:6px;border:1px solid var(--line)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent)">Lifecycle</div>
        ${lifecycleBadge(stage)}
      </div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">${esc(LIFECYCLE_META[stage].hint)}</div>
      <div class="form-group" style="margin-bottom:8px">
        <label>Stage</label>
        <select id="lc-stage" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px;background:white">${stages}</select>
      </div>
      <div class="form-group" style="margin-bottom:8px">
        <label>Why / what to use instead</label>
        <input id="lc-note" value="${esc(d.lifecycle_note||'')}" placeholder="e.g. superseded by intake-v2" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px">
      </div>
      <button class="btn accent" style="padding:6px 12px;font-size:11px" onclick="setLifecycle('${agentId}')">Update Stage</button>
      <div style="font-size:10px;color:var(--faint);margin-top:6px">Last changed: ${esc(changed)}</div>
      <div id="lc-msg" style="font-size:11px;margin-top:6px"></div>
    </div>
  </div>`;
}

async function saveOwnership(agentId){
  const body={owner_id:document.getElementById('own-owner').value,
              contact:document.getElementById('own-contact').value,
              account:document.getElementById('own-account').value};
  const r=await fetch('/api/agents/'+agentId+'/ownership',
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  const m=document.getElementById('lc-msg');
  if(!r.ok){ if(m){m.style.color='var(--brick)';m.textContent=j.detail||'Could not save.';} return; }
  await loadAgents();
  loadOwnership(agentId);
}

async function setLifecycle(agentId){
  const m=document.getElementById('lc-msg');
  const body={lifecycle:document.getElementById('lc-stage').value,
              note:document.getElementById('lc-note').value};
  const r=await fetch('/api/agents/'+agentId+'/lifecycle',
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){
    // The refusal is the useful part — show it verbatim rather than a generic error.
    if(m){ m.style.color='var(--brick)'; m.textContent=j.detail||'Could not change stage.'; }
    return;
  }
  await loadAgents();
  renderControl();
}

/* ── did the last config change help? (learned from runs) ── */
async function loadVersionPerf(agentId){
  const d=await fetch('/api/phase2/agents/'+agentId+'/versions')
    .then(r=>r.json()).catch(()=>null);
  const el=document.getElementById('ver-perf');
  if(!el||!d) return;
  const vs=d.versions||[], v=d.verdict;
  if(!vs.length){ el.innerHTML=''; return; }

  const tone = v&&v.direction==='worse'  ? {bg:'var(--bricksoft)', fg:'var(--brick)', label:'Regression'}
             : v&&v.direction==='better' ? {bg:'var(--accentsoft)',fg:'var(--terra)', label:'Improved'}
             : {bg:'#faf8f5', fg:'var(--muted)', label:'No clear change'};

  const rows=vs.slice(0,4).map(x=>{
    const pct=Math.round(x.success_rate*100);
    return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px">
      <span class="pill" style="background:#eceef1;color:var(--muted);min-width:34px;text-align:center">v${x.version}</span>
      <div style="flex:1;height:5px;background:#eceef1;border-radius:3px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:var(--terra)"></div>
      </div>
      <span style="font-family:'IBM Plex Mono',monospace;color:var(--muted);white-space:nowrap">
        ${pct}% · ${x.runs} run${x.runs===1?'':'s'}${x.enough_runs?'':' <i>thin</i>'}
      </span></div>`;
  }).join('');

  el.innerHTML=`<div style="border:1px solid var(--line);border-radius:4px;padding:10px;background:${tone.bg}">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
      <span style="font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:${tone.fg}">${tone.label}</span>
      <span style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em">· completion rate by config version</span>
    </div>
    ${rows}
    ${v?`<div style="margin-top:7px;font-size:11px;color:var(--ink);line-height:1.45">${esc(v.summary)}</div>`:''}
  </div>`;
}

/* ═══════════════ WORKFLOWS (Phase 2: build, run, learn) ═══════════════ */
let wfBuilder=[];        // [{agent_id, instruction, depends_on:[idx]}]
let wfAgentCache=[];
let wfOpenRun=null;      // expanded workflow run id

async function renderWorkflows(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div></div></div>';
  const [ag,hist,pats,graph]=await Promise.all([
    fetch('/api/agents').then(r=>r.json()).catch(()=>({agents:[]})),
    fetch('/api/phase2/workflow-runs?limit=25').then(r=>r.json()).catch(()=>({runs:[]})),
    fetch('/api/phase2/patterns?limit=10').then(r=>r.json()).catch(()=>({patterns:[]})),
    fetch('/api/phase2/graph').then(r=>r.json()).catch(()=>({nodes:[],edges:[]})),
  ]);
  wfAgentCache=(ag.agents||ag||[]).filter(a=>a&&a.id);
  if(!wfBuilder.length) wfBuilder=[{agent_id:'',instruction:'',depends_on:[]}];

  root.innerHTML=`<div style="padding:24px">
    <h2 style="margin:0 0 4px">Workflows</h2>
    <div style="color:var(--muted);font-size:12px;margin-bottom:16px">
      Chain agents into a DAG, run it, and Cortex records what happened. Patterns appear once the same sequence has run more than once.
    </div>
    <div style="display:grid;grid-template-columns:1.15fr .85fr;gap:14px;align-items:start">
      <div style="display:flex;flex-direction:column;gap:14px">
        ${wfBuilderCard()}
        ${wfHistoryCard(hist.runs||[])}
      </div>
      <div style="display:flex;flex-direction:column;gap:14px">
        ${wfPatternsCard(pats.patterns||[])}
        ${wfGraphCard(graph)}
      </div>
    </div></div>`;
}

/* ── builder ─────────────────────────────────────── */
function wfBuilderCard(){
  const opts=a=>wfAgentCache.map(x=>`<option value="${esc(x.id)}"${x.id===a?' selected':''}>${esc(x.name||x.slug||x.id)}</option>`).join('');
  const rows=wfBuilder.map((s,i)=>{
    const deps=wfBuilder.slice(0,i).map((_,j)=>
      `<label style="margin-right:8px;font-size:11px;white-space:nowrap"><input type="checkbox" ${s.depends_on.includes(j)?'checked':''} onchange="wfToggleDep(${i},${j})"> step ${j+1}</label>`).join('');
    return `<div style="border:1px solid var(--line);border-radius:4px;padding:10px;margin-bottom:8px;background:var(--paper)">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
        <span class="pill" style="background:var(--accentsoft);color:var(--terra)">step ${i+1}</span>
        <select onchange="wfSet(${i},'agent_id',this.value)" style="flex:1;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px">
          <option value="">— pick an agent —</option>${opts(s.agent_id)}
        </select>
        ${wfBuilder.length>1?`<button class="btn-sm" onclick="wfRemove(${i})" title="Remove step">×</button>`:''}
      </div>
      <input value="${esc(s.instruction)}" oninput="wfSet(${i},'instruction',this.value)"
        placeholder="What should this agent do?"
        style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:12px;box-sizing:border-box">
      ${i>0?`<div style="margin-top:6px;color:var(--muted);font-size:11px">waits for: ${deps||'<i>nothing</i>'}</div>`:''}
    </div>`;
  }).join('');

  return `<div class="stat-card">
    <div class="card-title">Build a workflow</div>
    <input id="wf-name" placeholder="Workflow name" value=""
      style="width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:3px;font:inherit;font-size:13px;margin:8px 0 10px;box-sizing:border-box">
    ${rows}
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn-sm" onclick="wfAddStep()">+ Add step</button>
      <div style="flex:1"></div>
      <button class="btn btn-primary" onclick="wfCreateAndRun()">Create &amp; run</button>
    </div>
    <div id="wf-status" style="margin-top:10px;font-size:12px"></div>
  </div>`;
}

function wfSet(i,k,v){ wfBuilder[i][k]=v; }
function wfToggleDep(i,j){
  const d=wfBuilder[i].depends_on, at=d.indexOf(j);
  if(at>=0) d.splice(at,1); else d.push(j);
  renderWorkflows();
}
function wfAddStep(){ wfBuilder.push({agent_id:'',instruction:'',depends_on:[]}); renderWorkflows(); }
function wfRemove(i){
  wfBuilder.splice(i,1);
  wfBuilder.forEach(s=>{ s.depends_on=s.depends_on.filter(d=>d!==i).map(d=>d>i?d-1:d); });
  renderWorkflows();
}

async function wfCreateAndRun(){
  const st=document.getElementById('wf-status');
  const name=(document.getElementById('wf-name').value||'').trim()||'Untitled workflow';
  const bad=wfBuilder.findIndex(s=>!s.agent_id);
  if(bad>=0){ st.innerHTML=`<span style="color:var(--brick)">Step ${bad+1} has no agent selected.</span>`; return; }

  const steps=wfBuilder.map((s,i)=>({id:'s'+i, agent_id:s.agent_id,
    instruction:s.instruction||'Proceed.', depends_on:s.depends_on.map(d=>'s'+d)}));

  st.innerHTML='<span style="color:var(--muted)">Creating…</span>';
  const cr=await fetch('/api/comms/workflows',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name,steps})}).then(r=>r.json());
  const wid=cr.workflow_id||(cr.workflow||{}).id;
  if(!wid){ st.innerHTML='<span style="color:var(--brick)">Could not create workflow.</span>'; return; }

  // The bus advances one wave of ready steps per call — drive it to completion.
  let waves=0, recorded=false, runId=null;
  for(let i=0;i<steps.length+2;i++){
    st.innerHTML=`<span style="color:var(--muted)">Running… wave ${i+1}</span>`;
    const r=await fetch('/api/comms/workflows/'+wid+'/run',{method:'POST'}).then(x=>x.json()).catch(()=>null);
    if(!r) break;
    waves++;
    if(r.recorded) recorded=true;
    if(r.workflow_run_id) runId=r.workflow_run_id;
    if((r.remaining||0)===0) break;
  }
  st.innerHTML=`<span style="color:var(--terra)">Ran ${waves} wave${waves===1?'':'s'}.</span>`
    + (recorded?' <span style="color:var(--muted)">Recorded to history.</span>'
              :' <span style="color:var(--brick)">Not recorded — check server logs.</span>');
  wfOpenRun=runId;
  setTimeout(renderWorkflows,600);
}

/* ── history ─────────────────────────────────────── */
function wfHistoryCard(runs){
  if(!runs.length) return `<div class="stat-card"><div class="card-title">Run history</div>
    <div style="color:var(--muted);font-size:12px;padding:12px 0">
      Nothing recorded yet. Build and run a workflow above — every run is saved here and survives a restart.
    </div></div>`;

  const rows=runs.map(r=>{
    const cls=r.status==='completed'?'running':(r.status==='failed'?'error':'stopped');
    return `<tr style="cursor:pointer" onclick="wfToggleRun('${esc(r.id)}')">
      <td>${esc(r.name||'—')}</td>
      <td><span class="pill ${cls}">${esc(r.status)}</span></td>
      <td style="font-family:monospace;font-size:11px">${r.succeeded}/${r.steps}</td>
      <td style="font-family:monospace;font-size:11px">${Math.round(r.latency_ms)}ms</td>
      <td style="font-family:monospace;font-size:11px">${r.cost_usd?('$'+r.cost_usd.toFixed(4)):'—'}</td>
      <td><button class="btn-sm" title="Run this exact workflow again"
        onclick="event.stopPropagation();wfRerun('${esc(r.id)}')">Re-run</button></td>
    </tr>` + (wfOpenRun===r.id?`<tr><td colspan="6" style="padding:0">
      <div id="wf-steps-${esc(r.id)}" style="padding:8px 12px;background:var(--paper);font-size:11px;color:var(--muted)">loading steps…</div></td></tr>`:'');
  }).join('');

  return `<div class="stat-card"><div class="card-title">Run history</div>
    <table class="data-table">
      <thead><tr><th>Workflow</th><th>Status</th><th>Steps</th><th>Latency</th><th>Cost</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="hint" style="margin-top:8px">Click a run to see its steps. Re-run repeats it exactly — that repeat is what forms a pattern.</div>
    <div id="wf-rerun" style="margin-top:6px;font-size:12px"></div></div>`;
}

async function wfRerun(id){
  const el=document.getElementById('wf-rerun');
  if(el) el.innerHTML='<span style="color:var(--muted)">Replaying…</span>';
  const r=await fetch('/api/phase2/workflow-runs/'+id+'/rerun',{method:'POST'})
    .then(x=>x.json()).catch(()=>null);
  if(el){
    el.innerHTML = (r&&r.ok)
      ? `<span style="color:var(--terra)">Replayed — ${r.succeeded}/${r.steps} steps ok.</span> <span style="color:var(--muted)">Hit Analyze to score the pattern.</span>`
      : `<span style="color:var(--brick)">${esc((r&&r.detail)||'Replay failed.')}</span>`;
  }
  setTimeout(renderWorkflows,700);
}

async function wfToggleRun(id){
  if(wfOpenRun===id){ wfOpenRun=null; return renderWorkflows(); }
  wfOpenRun=id;
  await renderWorkflows();
  const el=document.getElementById('wf-steps-'+id);
  if(!el) return;
  const d=await fetch('/api/phase2/workflow-runs/'+id).then(r=>r.json()).catch(()=>null);
  if(!d||!d.step_detail){ el.textContent='Could not load steps.'; return; }
  const name=i=>{ const a=wfAgentCache.find(x=>x.id===i); return a?(a.name||a.slug):i; };
  el.innerHTML=d.step_detail.map(s=>
    `<div style="padding:3px 0">
      <span class="pill" style="background:var(--accentsoft);color:var(--terra)">depth ${s.depth}</span>
      <b>${esc(name(s.agent_id))}</b> — ${esc(s.status)}
      ${s.latency_ms?` · ${Math.round(s.latency_ms)}ms`:''}
      ${s.run_id?` · <span style="font-family:monospace">run ${esc(s.run_id.slice(0,8))}</span>`
                :' · <i>no run row linked</i>'}
      ${s.error?` · <span style="color:var(--brick)">${esc(s.error.slice(0,120))}</span>`:''}
    </div>`).join('');
}

/* ── patterns ────────────────────────────────────── */
function wfPatternsCard(pats){
  const head=`<div class="card-title">Patterns</div>`;
  if(!pats.length) return `<div class="stat-card">${head}
    <div style="color:var(--muted);font-size:12px;padding:12px 0">
      No patterns yet. A pattern is a sequence of agents that has run <b>more than once</b> — Cortex needs a repeat before it can say anything about how a sequence performs.
    </div>
    <button class="btn-sm" onclick="wfAnalyze()">Analyze now</button>
    <div id="wf-an" style="margin-top:8px;font-size:11px;color:var(--muted)"></div></div>`;

  const name=i=>{ const a=wfAgentCache.find(x=>x.id===i); return a?(a.name||a.slug):i.slice(0,8); };
  const rows=pats.map(p=>{
    const succeeded=Math.round((p.success_rate||0)*(p.executions||0));
    const trend=p.trend==='improving'?'<span style="color:var(--terra)">improving</span>'
              :p.trend==='declining'?'<span style="color:var(--brick)">declining</span>'
              :p.trend==='stable'?'stable':'<span style="color:var(--muted)">not enough runs</span>';
    return `<div style="border-top:1px solid var(--line);padding:9px 0">
      <div style="font-size:12px;margin-bottom:3px">${(p.agents||[]).map(a=>esc(name(a))).join(' <span style="color:var(--muted)">→</span> ')}</div>
      <div style="font-size:11px;color:var(--muted);font-family:'IBM Plex Mono',monospace">
        ${succeeded}/${p.executions} runs succeeded ·
        ${Math.round(p.avg_latency_ms)}ms avg ·
        ${p.avg_cost_usd?('$'+p.avg_cost_usd.toFixed(4)+' avg'):'no cost data'} · ${trend}
      </div></div>`;
  }).join('');

  return `<div class="stat-card">${head}
    <div style="font-size:11px;color:var(--muted);margin-bottom:2px">Ranked by success rate weighted by how often each ran.</div>
    ${rows}
    <div style="margin-top:10px"><button class="btn-sm" onclick="wfAnalyze()">Re-analyze</button>
    <span id="wf-an" style="margin-left:8px;font-size:11px;color:var(--muted)"></span></div></div>`;
}

async function wfAnalyze(){
  const el=document.getElementById('wf-an');
  if(el) el.textContent='analyzing…';
  const r=await fetch('/api/phase2/patterns/analyze',{method:'POST'}).then(x=>x.json()).catch(()=>null);
  if(el&&r) el.textContent=r.note?r.note:`${r.patterns||0} pattern(s) from ${r.runs_analyzed||0} runs`;
  setTimeout(renderWorkflows,700);
}

/* ── relationship graph ──────────────────────────── */
function wfGraphCard(g){
  const nodes=g.nodes||[], edges=g.edges||[];
  const head=`<div class="card-title">Agent relationships</div>`;
  if(!nodes.length) return `<div class="stat-card">${head}
    <div style="color:var(--muted);font-size:12px;padding:12px 0">
      No relationships found yet. Cortex reads these from agent config (escalation targets, shared data sources) and from workflows that have actually run.
    </div>
    <button class="btn-sm" onclick="wfDiscover()">Discover</button>
    <div id="wf-dsc" style="margin-top:8px;font-size:11px;color:var(--muted)"></div></div>`;

  // Circular layout — readable for the handful of agents a graph like this holds.
  const W=400,H=260,cx=W/2,cy=H/2,R=Math.min(W,H)/2-58;
  const pos={};
  nodes.forEach((n,i)=>{ const a=(i/nodes.length)*2*Math.PI-Math.PI/2;
    pos[n.id]={x:cx+R*Math.cos(a),y:cy+R*Math.sin(a)}; });

  const seen=new Set();
  const lines=edges.map(e=>{
    const p=pos[e.source],q=pos[e.target]; if(!p||!q) return '';
    const key=[e.source,e.target].sort().join('|')+e.type;
    if(seen.has(key)) return ''; seen.add(key);   // one line per undirected pair+type
    const strong=e.type==='escalation'||e.type==='workflow_dependency';
    return `<line x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}"
      stroke="${strong?'var(--terra)':'var(--line)'}"
      stroke-width="${Math.max(1,(e.strength||50)/45)}"
      ${strong?'':'stroke-dasharray="3,3"'} opacity="0.75"></line>`;
  }).join('');

  const dots=nodes.map((n,i)=>{const p=pos[n.id];
    // Push the label radially outward so it clears the node and its edges.
    const a=(i/nodes.length)*2*Math.PI-Math.PI/2;
    const lx=cx+(R+20)*Math.cos(a), ly=cy+(R+20)*Math.sin(a);
    const anchor=Math.cos(a)>0.3?'start':(Math.cos(a)<-0.3?'end':'middle');
    const dy=Math.sin(a)>0.3?11:(Math.sin(a)<-0.3?-4:4);
    return `<g><circle cx="${p.x}" cy="${p.y}" r="7" fill="var(--card)" stroke="var(--terra)" stroke-width="2"></circle>
      <text x="${lx}" y="${ly+dy}" text-anchor="${anchor}" font-size="10" fill="var(--fg)">${esc((n.name||n.id).slice(0,16))}</text></g>`;
  }).join('');

  const kinds={};
  edges.forEach(e=>{kinds[e.type]=(kinds[e.type]||0)+1;});
  const legend=Object.entries(kinds).map(([k,v])=>
    `<span class="chip" style="margin-right:4px">${esc(k.replace(/_/g,' '))} ${v}</span>`).join('');

  return `<div class="stat-card">${head}
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${lines}${dots}</svg>
    <div style="margin-top:6px">${legend}</div>
    <div class="hint" style="margin-top:6px">Solid = declared handoff or an executed dependency. Dashed = shared data source or co-execution.</div>
    <div style="margin-top:8px"><button class="btn-sm" onclick="wfDiscover()">Re-discover</button>
    <span id="wf-dsc" style="margin-left:8px;font-size:11px;color:var(--muted)"></span></div></div>`;
}

async function wfDiscover(){
  const el=document.getElementById('wf-dsc');
  if(el) el.textContent='scanning…';
  const r=await fetch('/api/phase2/discover?source=both',{method:'POST'}).then(x=>x.json()).catch(()=>null);
  if(el&&r){
    const c=(r.config||{}), rt=(r.runtime||{});
    el.textContent=`config: +${c.created||0}/~${c.updated||0} · runtime: ${rt.note?rt.note:('+'+(rt.created||0)+'/~'+(rt.updated||0))}`;
  }
  setTimeout(renderWorkflows,700);
}

/* ═══════════════ RECOVERY (recycle bin + version history) ═══════════════ */
async function renderRecovery(){
  const root=document.getElementById('root');
  root.innerHTML='<div style="padding:24px"><div class="stat-card" style="text-align:center;padding:32px"><div class="spinner"></div></div></div>';
  const bin=await fetch('/api/recycle-bin').then(r=>r.json()).catch(()=>({deleted_agents:[]}));
  const binRows=(bin.deleted_agents||[]).map(a=>`<tr><td>${esc(a.name)}</td><td style="font-size:11px;color:var(--muted)">${esc((a.deleted_at||'').slice(0,10))}</td><td style="font-size:11px;color:var(--muted)">${esc((a.purge_after||'').slice(0,10))}</td><td><button class="btn-sm" onclick="restoreAgent('${a.id}')">Restore</button></td></tr>`).join('');
  const agentOpts=AGENTS.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('');
  root.innerHTML=`<div style="padding:24px"><h2 style="margin:0 0 4px">Recovery</h2>
    <div style="color:var(--muted);font-size:12px;margin-bottom:16px">Deleted agents are recoverable for ${bin.retention_days||30} days. Every config change is snapshotted and hash-chained, so any prior version can be restored or rebuilt.</div>
    <div class="stat-card" style="margin-bottom:14px"><div class="card-title">🗑 Recycle Bin</div>
      <table class="data-table"><thead><tr><th>Agent</th><th>Deleted</th><th>Purges</th><th></th></tr></thead>
      <tbody>${binRows||'<tr><td colspan=4 style="color:var(--muted);padding:12px">Recycle bin is empty</td></tr>'}</tbody></table></div>
    <div class="stat-card"><div class="card-title">Version History & Rollback</div>
      <select id="verAgent" onchange="loadVersions(this.value)" style="width:100%;padding:9px 12px;margin-bottom:12px;background:var(--panel);border:1px solid var(--line);border-radius:6px;color:var(--fg)"><option value="">Select an agent...</option>${agentOpts}</select>
      <div id="verList"></div></div></div>`;
}
async function restoreAgent(id){ await fetch('/api/recycle-bin/'+id+'/restore',{method:'POST'}); await boot(); renderRecovery(); }
async function loadVersions(aid){
  if(!aid){document.getElementById('verList').innerHTML='';return;}
  const d=await fetch('/api/agents/'+aid+'/versions').then(r=>r.json()).catch(()=>({versions:[]}));
  const integ=d.integrity||{};
  const rows=(d.versions||[]).map(v=>`<tr><td style="font-family:monospace">v${v.version}</td><td>${esc(v.change_type)}</td><td style="font-size:12px">${esc(v.change_summary||'')}</td><td style="font-size:11px;color:var(--muted)">${esc(v.changer_email||v.changed_by||'system')}</td><td style="font-size:11px;color:var(--muted)">${esc((v.created_at||'').slice(0,16).replace('T',' '))}</td><td><button class="btn-sm" onclick="restoreVersion('${aid}',${v.version})">Restore</button></td></tr>`).join('');
  document.getElementById('verList').innerHTML=`<div style="font-size:11px;margin-bottom:8px;color:${integ.intact?'#22c55e':'#ef4444'}">${integ.intact?'✓ Hash chain intact — '+integ.versions+' versions, tamper-evident':'⚠ Chain integrity issue: versions '+(integ.broken_versions||[]).join(', ')}</div>
    <table class="data-table"><thead><tr><th>Version</th><th>Type</th><th>Change</th><th>By</th><th>When</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan=6 style="color:var(--muted);padding:12px">No version history</td></tr>'}</tbody></table>`;
}
async function restoreVersion(aid,v){
  if(!confirm('Restore agent to version '+v+'? This creates a new version with the old config.'))return;
  await fetch('/api/agents/'+aid+'/versions/'+v+'/restore',{method:'POST'});
  await boot(); loadVersions(aid);
}

async function setView(v){ view=v; await render(); }
boot();
</script>
</body></html>"""

if __name__ == "__main__":
    print("CORTEX Agent Ops Hub — http://localhost:3000")
    print("model-assisted" if API_KEY else "rule-based (set ANTHROPIC_API_KEY for model translation)")
    # PORT lets you run a second instance, or dodge a port that's already taken.
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "3000")))
