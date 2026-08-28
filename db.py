"""
CORTEX Database Layer
─────────────────────
SQLAlchemy models + async session management for PostgreSQL.

Tables:
    users       — authentication (email/password + OAuth)
    agents      — agent configs, status, metrics
    runs        — execution history with full trace
    settings    — per-instance provider settings
    data_sources — agent data source configurations

Usage:
    from db import get_db, init_db, User, Agent, Run, Setting, DataSource
"""
import os
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    create_engine, Column, String, Text, Boolean, Integer, Float,
    DateTime, JSON, ForeignKey, Index, event
)
from sqlalchemy.orm import (
    declarative_base, sessionmaker, relationship, Session
)

# ── Database URL ──────────────────────────────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://cortex:cortex@localhost:5432/cortex"
)

# Handle Railway/Render-style postgres:// URLs (SQLAlchemy needs postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # auto-reconnect on stale connections
    echo=os.environ.get("SQL_ECHO", "").lower() == "true",
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def get_db():
    """Dependency — yields a DB session and closes it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────

def gen_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, default=gen_id)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True)  # null for OAuth-only users
    name = Column(String(255), default="")
    avatar_url = Column(String(512), default="")

    # OAuth fields
    oauth_provider = Column(String(32), nullable=True)  # 'google' | 'github' | null
    oauth_id = Column(String(255), nullable=True)  # provider's user ID
    oauth_data = Column(JSON, default=dict)  # extra profile data from provider

    # Account state
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    agents = relationship("Agent", back_populates="owner", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_users_oauth", "oauth_provider", "oauth_id", unique=True,
              postgresql_where=Column("oauth_provider").isnot(None)),
    )


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String(64), primary_key=True, default=gen_id)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)
    slug = Column(String(128), unique=True, nullable=False, index=True)  # URL-safe identifier
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    account = Column(String(128), default="")  # team/org label
    agent_type = Column(String(32), default="custom")  # sample | custom | imported

    # State
    status = Column(String(32), default="stopped")  # running | stopped | error
    live = Column(Boolean, default=False)
    version = Column(Integer, default=1)

    # Full config stored as JSON (model, execution, behavior, tools, data_sources, audit)
    config = Column(JSON, default=dict)

    # Endpoint config for REST/webhook agents
    endpoint = Column(JSON, default=dict)

    # Metrics (denormalized for fast reads)
    containment = Column(Float, default=0.0)
    escalation = Column(Float, default=0.0)
    resolution = Column(Float, default=0.0)

    # Soft-delete / recovery — a deleted agent is retained (recycle bin), not destroyed
    is_deleted = Column(Boolean, default=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(String(64), nullable=True)
    purge_after = Column(DateTime(timezone=True), nullable=True)  # hard-delete eligible after this

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    owner = relationship("User", back_populates="agents")
    runs = relationship("Run", back_populates="agent", cascade="all, delete-orphan",
                        order_by="Run.started_at.desc()")
    data_sources = relationship("DataSource", back_populates="agent", cascade="all, delete-orphan")
    versions = relationship("AgentVersion", back_populates="agent", cascade="all, delete-orphan",
                            order_by="AgentVersion.version.desc()")


class AgentVersion(Base):
    """Immutable snapshot of an agent's full config at each version.

    Every config change writes a new row. Deleting an agent never removes these
    while the agent sits in the recycle bin, so any prior version can be restored
    or the agent rebuilt from scratch even after deletion.
    """
    __tablename__ = "agent_versions"

    id = Column(String(64), primary_key=True, default=gen_id)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)

    # Full point-in-time snapshot
    name = Column(String(255), default="")
    slug = Column(String(128), default="")
    description = Column(Text, default="")
    config = Column(JSON, default=dict)      # complete config blob at this version
    endpoint = Column(JSON, default=dict)

    # Provenance — who made this change and what changed
    changed_by = Column(String(64), nullable=True)      # user_id
    changer_email = Column(String(255), default="")
    change_summary = Column(String(512), default="")    # human-readable diff summary
    change_type = Column(String(32), default="update")  # create | update | restore | rebuild
    diff = Column(JSON, default=dict)                    # {field: {from, to}}

    # Integrity — hash chain per agent for tamper evidence
    prev_hash = Column(String(64), default="")
    record_hash = Column(String(64), default="")

    created_at = Column(DateTime(timezone=True), default=utcnow)

    agent = relationship("Agent", back_populates="versions")

    __table_args__ = (
        Index("ix_agentversion_agent_version", "agent_id", "version", unique=True),
        Index("ix_agentversion_agent_time", "agent_id", "created_at"),
    )


class Run(Base):
    __tablename__ = "runs"

    id = Column(String(64), primary_key=True, default=gen_id)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)

    claim = Column(Text, default="")  # input text
    outcome = Column(String(32), default="")  # COMPLETED | ESCALATED | ERROR | WEBHOOK_PENDING
    published = Column(Boolean, default=False)
    steps_used = Column(Integer, default=0)
    config_version = Column(Integer, default=1)

    # Provider info
    provider = Column(String(32), default="")
    model = Column(String(128), default="")

    # Full trace stored as JSON array
    trace = Column(JSON, default=list)

    # Result detail
    detail = Column(JSON, default=dict)  # {summary, reason, citations, route_to}

    # Token usage
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    user_id = Column(String(64), nullable=True, index=True)

    # Timestamps
    started_at = Column(DateTime(timezone=True), default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    agent = relationship("Agent", back_populates="runs")

    __table_args__ = (
        Index("ix_runs_agent_started", "agent_id", "started_at"),
    )


class ApiKey(Base):
    """API keys for external callers to trigger agents."""
    __tablename__ = "api_keys"

    id = Column(String(64), primary_key=True, default=gen_id)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # human-readable label
    key_hash = Column(String(255), nullable=False)  # SHA-256 hash of the key
    prefix = Column(String(12), nullable=False)  # first 8 chars for identification
    scopes = Column(JSON, default=list)  # ["agents:read", "agents:run", "agents:write"]
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User")


class DataSource(Base):
    __tablename__ = "data_sources"

    id = Column(String(64), primary_key=True, default=gen_id)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    source_type = Column(String(32), default="api")  # api | database | file | webhook | graphql | grpc | custom
    endpoint = Column(String(512), default="")
    auth_type = Column(String(32), default="none")  # api_key | oauth2 | bearer | basic | connection_string | iam | none
    auth_value = Column(String(512), default="")  # encrypted in production
    refresh = Column(String(32), default="manual")  # realtime | 5m | 1h | 1d | manual

    created_at = Column(DateTime(timezone=True), default=utcnow)

    # Relationships
    agent = relationship("Agent", back_populates="data_sources")


class AgentRelease(Base):
    """Which config version is live in one of the client's environments.

    CORTEX does not host agents — they run wherever the customer runs them.
    So a "deployment" here is not a push to a machine; it is a pointer. This
    row says "production is on v4", and the customer's running agent asks
    CORTEX what it should be using.

    That direction matters. A push model needs every customer to build and
    operate a config receiver before CORTEX does anything for them. A pull
    model costs them one HTTP call, and it means this table records what an
    environment is actually running rather than what we last tried to send it.
    """
    __tablename__ = "agent_releases"

    id = Column(String(64), primary_key=True, default=gen_id)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)

    environment = Column(String(32), nullable=False)   # staging | production | <custom>
    active_version = Column(Integer, nullable=False)   # -> agent_versions.version

    released_by = Column(String(64), nullable=True)    # user_id
    released_by_email = Column(String(255), default="")
    note = Column(String(512), default="")

    # Set whenever the environment actually fetches its config, so you can tell
    # "released" from "picked up" — an environment that never polls is a real
    # failure mode and should be visible rather than assumed away.
    last_fetched_at = Column(DateTime(timezone=True), nullable=True)
    fetch_count = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_release_agent_env", "agent_id", "environment"),
    )

    def to_dict(self):
        return {
            "environment": self.environment,
            "active_version": self.active_version,
            "released_by_email": self.released_by_email or "",
            "note": self.note or "",
            "released_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_fetched_at": self.last_fetched_at.isoformat() if self.last_fetched_at else None,
            "fetch_count": self.fetch_count or 0,
        }


class Setting(Base):
    """Key-value settings table — one row per setting."""
    __tablename__ = "settings"

    key = Column(String(128), primary_key=True)
    value = Column(JSON, default=dict)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Immutable audit trail for agent events."""
    __tablename__ = "audit_log"

    id = Column(String(64), primary_key=True, default=gen_id)
    agent_id = Column(String(64), index=True, nullable=True)
    user_id = Column(String(64), nullable=True)
    event = Column(String(128), nullable=False)  # e.g. "run.start", "config.changed"
    data = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_audit_agent_time", "agent_id", "created_at"),
    )


class OAuthState(Base):
    """Temporary OAuth state storage for CSRF protection."""
    __tablename__ = "oauth_states"

    state = Column(String(128), primary_key=True)
    provider = Column(String(32), nullable=False)
    redirect_url = Column(String(512), default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Webhook(Base):
    """Webhook subscriptions for agent events."""
    __tablename__ = "webhooks"

    id = Column(String(64), primary_key=True, default=gen_id)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    agent_id = Column(String(64), nullable=True, index=True)  # null = all agents
    url = Column(String(1024), nullable=False)
    secret = Column(String(255), default="")  # HMAC-SHA256 signing secret
    events = Column(JSON, default=list)  # ["run.completed","run.escalated","run.error","agent.status_changed"]
    is_active = Column(Boolean, default=True)
    failure_count = Column(Integer, default=0)  # auto-disable after repeated failures
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User")


class AgentTemplate(Base):
    """Reusable agent configuration templates."""
    __tablename__ = "agent_templates"

    id = Column(String(64), primary_key=True, default=gen_id)
    created_by = Column(String(64), ForeignKey("users.id"), nullable=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    category = Column(String(64), default="custom")  # customer-support | data-processing | monitoring | automation | custom
    icon = Column(String(8), default="")  # emoji icon
    config = Column(JSON, default=dict)  # full agent config snapshot
    is_public = Column(Boolean, default=True)  # visible to all users on this instance
    use_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    creator = relationship("User")


class Notification(Base):
    """In-app notifications for users."""
    __tablename__ = "notifications"

    id = Column(String(64), primary_key=True, default=gen_id)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    agent_id = Column(String(64), nullable=True)
    event = Column(String(128), nullable=False)  # run.completed | run.error | agent.stopped
    title = Column(String(255), nullable=False)
    body = Column(Text, default="")
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_notif_user_read", "user_id", "is_read"),
    )


class Attestation(Base):
    """Immutable provenance record for every agent action. Hash-chained for tamper evidence."""
    __tablename__ = "attestations"

    id = Column(String(64), primary_key=True, default=gen_id)
    run_id = Column(String(64), nullable=True, index=True)
    agent_id = Column(String(64), nullable=False, index=True)
    agent_name = Column(String(255), default="")
    agent_version = Column(Integer, default=1)

    # Who
    authorized_by = Column(String(64), nullable=True)  # user_id who triggered
    authorizer_email = Column(String(255), default="")
    auth_method = Column(String(32), default="session")  # session | api_key | webhook | schedule

    # What intelligence
    provider = Column(String(32), default="")
    model = Column(String(128), default="")

    # What happened
    action = Column(String(128), default="")  # run.execute | config.change | agent.start | agent.stop
    action_input = Column(Text, default="")  # truncated input/claim
    action_result = Column(String(32), default="")  # COMPLETED | ESCALATED | ERROR
    action_summary = Column(Text, default="")  # truncated output

    # Data accessed
    data_sources_accessed = Column(JSON, default=list)  # list of data source names/types

    # Policy
    policy_checked = Column(Boolean, default=False)
    policy_passed = Column(Boolean, default=True)
    policy_details = Column(JSON, default=dict)  # which policies were evaluated

    # Human approval
    human_approval_required = Column(Boolean, default=False)
    human_approval_granted = Column(Boolean, default=False)
    human_approver_id = Column(String(64), nullable=True)

    # Tokens
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)

    # Integrity
    prev_hash = Column(String(64), default="")  # SHA-256 of previous attestation for this agent
    record_hash = Column(String(64), default="")  # SHA-256 of this record's contents

    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_attest_agent_time", "agent_id", "created_at"),
        Index("ix_attest_user_time", "authorized_by", "created_at"),
    )


class UserRole(Base):
    """Per-user role assignments. Roles: viewer, operator, admin."""
    __tablename__ = "user_roles"

    id = Column(String(64), primary_key=True, default=gen_id)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="operator")  # viewer | operator | admin
    scope = Column(String(64), default="global")  # global | agent:{agent_id}
    granted_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User")

    __table_args__ = (
        Index("ix_userrole_user_scope", "user_id", "scope", unique=True),
    )


class ApprovalRequest(Base):
    """Pending human approval for an agent action."""
    __tablename__ = "approval_requests"

    id = Column(String(64), primary_key=True, default=gen_id)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)
    run_id = Column(String(64), nullable=True)
    requested_by = Column(String(64), nullable=True)  # user or system that triggered
    action = Column(String(128), default="")  # what the agent wants to do
    context = Column(JSON, default=dict)  # input, proposed action details
    status = Column(String(32), default="pending")  # pending | approved | rejected | expired
    decided_by = Column(String(64), nullable=True)  # user_id who approved/rejected
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, default="")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    agent = relationship("Agent")

    __table_args__ = (
        Index("ix_approval_status", "status", "created_at"),
    )


# ═══════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_or_create_setting(db: Session, key: str, default=None):
    """Get a setting value, creating it with the default if it doesn't exist."""
    row = db.query(Setting).filter(Setting.key == key).first()
    if row is None:
        row = Setting(key=key, value=default or {})
        db.add(row)
        db.commit()
        db.refresh(row)
    return row.value


def set_setting(db: Session, key: str, value):
    """Set a setting value (upsert)."""
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
        db.add(row)
    db.commit()
    return value


def log_audit(db: Session, agent_id: str = None, user_id: str = None,
              event: str = "", data: dict = None):
    """Write an audit log entry."""
    entry = AuditLog(
        agent_id=agent_id, user_id=user_id,
        event=event, data=data or {}
    )
    db.add(entry)
    db.commit()


# ═══════════════════════════════════════════════════════════════
#  VERSIONING & RECOVERY
# ═══════════════════════════════════════════════════════════════

import hashlib as _hashlib
import json as _json
from datetime import timedelta as _timedelta

# How long a deleted agent stays recoverable in the recycle bin
RECYCLE_BIN_RETENTION_DAYS = 30


def _hash_version(agent_id: str, version: int, config: dict, prev_hash: str) -> str:
    """Deterministic hash of a version snapshot for the per-agent hash chain."""
    payload = _json.dumps({
        "agent_id": agent_id, "version": version,
        "config": config, "prev_hash": prev_hash,
    }, sort_keys=True, default=str)
    return _hashlib.sha256(payload.encode()).hexdigest()


def _diff_config(old: dict, new: dict) -> dict:
    """Shallow diff between two config blobs — returns {field: {from, to}}."""
    diff = {}
    keys = set((old or {}).keys()) | set((new or {}).keys())
    for k in keys:
        ov, nv = (old or {}).get(k), (new or {}).get(k)
        if ov != nv:
            diff[k] = {"from": ov, "to": nv}
    return diff


def snapshot_agent_version(db: Session, agent, changed_by: str = None,
                           changer_email: str = "", change_type: str = "update",
                           change_summary: str = "", prev_config: dict = None) -> "AgentVersion":
    """Write an immutable version snapshot for an agent. Call on every config change.

    Increments the agent's version counter and hash-chains the snapshot.
    Returns the created AgentVersion row.
    """
    # Determine next version number
    last = (db.query(AgentVersion)
            .filter(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.version.desc()).first())
    next_version = (last.version + 1) if last else 1
    prev_hash = last.record_hash if last else ""

    config = agent.config or {}
    diff = _diff_config(prev_config, config) if prev_config is not None else {}
    record_hash = _hash_version(agent.id, next_version, config, prev_hash)

    snap = AgentVersion(
        agent_id=agent.id, version=next_version,
        name=agent.name, slug=agent.slug, description=agent.description or "",
        config=config, endpoint=agent.endpoint or {},
        changed_by=changed_by, changer_email=changer_email,
        change_summary=change_summary or f"{change_type} — v{next_version}",
        change_type=change_type, diff=diff,
        prev_hash=prev_hash, record_hash=record_hash,
    )
    db.add(snap)
    # Keep the agent's denormalized version counter in sync
    agent.version = next_version
    db.commit()
    db.refresh(snap)
    return snap


def list_agent_versions(db: Session, agent_id: str, limit: int = 100) -> list:
    """List version history for an agent, newest first."""
    rows = (db.query(AgentVersion)
            .filter(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc()).limit(limit).all())
    return rows


def verify_version_chain(db: Session, agent_id: str) -> dict:
    """Verify the per-agent hash chain is intact (tamper detection)."""
    rows = (db.query(AgentVersion)
            .filter(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.asc()).all())
    prev_hash = ""
    broken = []
    for r in rows:
        expected = _hash_version(r.agent_id, r.version, r.config or {}, prev_hash)
        if r.record_hash != expected or r.prev_hash != prev_hash:
            broken.append(r.version)
        prev_hash = r.record_hash
    return {
        "agent_id": agent_id, "versions": len(rows),
        "intact": len(broken) == 0, "broken_versions": broken,
    }


def soft_delete_agent(db: Session, agent, deleted_by: str = None) -> dict:
    """Move an agent to the recycle bin (recoverable). Does NOT destroy data."""
    agent.is_deleted = True
    agent.status = "stopped"
    agent.live = False
    agent.deleted_at = utcnow()
    agent.deleted_by = deleted_by
    agent.purge_after = utcnow() + _timedelta(days=RECYCLE_BIN_RETENTION_DAYS)
    db.commit()
    log_audit(db, agent_id=agent.id, user_id=deleted_by, event="agent.deleted",
              data={"recoverable_until": agent.purge_after.isoformat(),
                    "name": agent.name})
    return {"ok": True, "recoverable_until": agent.purge_after.isoformat()}


def restore_agent(db: Session, agent, restored_by: str = None) -> dict:
    """Restore an agent from the recycle bin."""
    if not agent.is_deleted:
        return {"ok": False, "error": "agent is not deleted"}
    agent.is_deleted = False
    agent.deleted_at = None
    agent.deleted_by = None
    agent.purge_after = None
    db.commit()
    log_audit(db, agent_id=agent.id, user_id=restored_by, event="agent.restored",
              data={"name": agent.name})
    return {"ok": True, "agent_id": agent.id}


def restore_agent_version(db: Session, agent, version: int, restored_by: str = None,
                          changer_email: str = "") -> dict:
    """Roll an agent's config back to a prior version. Records a new version snapshot."""
    target = (db.query(AgentVersion)
              .filter(AgentVersion.agent_id == agent.id,
                      AgentVersion.version == version).first())
    if not target:
        return {"ok": False, "error": f"version {version} not found"}
    prev_config = agent.config or {}
    agent.name = target.name or agent.name
    agent.description = target.description
    agent.config = target.config
    agent.endpoint = target.endpoint or {}
    db.commit()
    snapshot_agent_version(
        db, agent, changed_by=restored_by, changer_email=changer_email,
        change_type="restore", prev_config=prev_config,
        change_summary=f"Restored config from v{version}",
    )
    return {"ok": True, "restored_from": version, "new_version": agent.version}


def list_recycle_bin(db: Session, owner_id: str = None) -> list:
    """List soft-deleted agents still within the retention window."""
    q = db.query(Agent).filter(Agent.is_deleted == True)  # noqa: E712
    if owner_id:
        q = q.filter(Agent.owner_id == owner_id)
    return q.order_by(Agent.deleted_at.desc()).all()


def purge_expired_agents(db: Session) -> int:
    """Hard-delete agents whose retention window has elapsed. Returns count purged."""
    now = utcnow()
    expired = (db.query(Agent)
               .filter(Agent.is_deleted == True,  # noqa: E712
                       Agent.purge_after != None,  # noqa: E711
                       Agent.purge_after < now).all())
    count = len(expired)
    for a in expired:
        db.delete(a)
    if count:
        db.commit()
    return count
