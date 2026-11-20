"""Cortex Experience v1 — opt-in agent memory, feedback, and performance review.

Additive extension of the existing Cortex application. This module registers
its own tables with the shared SQLAlchemy metadata. It does not modify agent
execution, inference, or pre-existing database columns.

This is a developer preview, not an autonomous training or billing service.
"""
from collections import Counter
from datetime import timezone
import hashlib
import json
from pathlib import Path
from typing import Literal, Optional
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func, or_

from db import Base, SessionLocal, Agent, Run, utcnow


class ExperienceMemory(Base):
    __tablename__ = "experience_memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=True, index=True)
    kind = Column(String(32), nullable=False, default="knowledge")
    title = Column(String(160), nullable=False)
    content = Column(Text, nullable=False)
    tags = Column(JSON, nullable=False, default=list)
    status = Column(String(16), nullable=False, default="draft")
    verification_note = Column(Text, nullable=False, default="")
    training_opt_in = Column(Boolean, nullable=False, default=False)
    idempotency_key = Column(String(128), nullable=True)
    request_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_memory_owner_idempotency"),
        Index("ix_memories_owner_status", "owner_id", "status"),
    )


class ExperienceFeedback(Base):
    __tablename__ = "experience_feedback"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=False, index=True)
    rating = Column(String(16), nullable=False)
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ExperienceUsage(Base):
    """Informational meter. No charges or invoices are generated in v1."""
    __tablename__ = "experience_usage"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    operation = Column(String(64), nullable=False)
    units = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemoryCreate(BaseModel):
    agent_id: Optional[str] = Field(default=None, max_length=64)
    kind: Literal["knowledge", "preference", "correction", "skill"] = "knowledge"
    title: str = Field(min_length=2, max_length=160)
    content: str = Field(min_length=2, max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    training_opt_in: bool = False


class MemoryReview(BaseModel):
    status: Literal["draft", "verified"]
    verification_note: str = Field(default="", max_length=2000)
    training_opt_in: Optional[bool] = None


class MemoryEdit(BaseModel):
    """Edits reset prior human verification and training consent."""
    kind: Literal["knowledge", "preference", "correction", "skill"]
    title: str = Field(min_length=2, max_length=160)
    content: str = Field(min_length=2, max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=12)


class FeedbackCreate(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    rating: Literal["helpful", "unhelpful"]
    note: str = Field(default="", max_length=2000)


def memory_out(m):
    return {
        "id": m.id, "agent_id": m.agent_id, "kind": m.kind,
        "title": m.title, "content": m.content, "tags": m.tags or [],
        "status": m.status, "verification_note": m.verification_note,
        "training_opt_in": bool(m.training_opt_in),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def version_performance(db, agent_id):
    """Heuristic comparison only; completed runs do not prove correctness.

    Ported from ev-prawda92/cortexlearning with the parent Cortex Run model
    and computed timestamps instead of its demo-only latency_ms property.
    """
    rows = (db.query(Run).filter(Run.agent_id == agent_id)
            .order_by(Run.started_at.asc()).all())
    groups = {}
    for run in rows:
        groups.setdefault(run.config_version or 1, []).append(run)
    versions = []
    for version in sorted(groups, reverse=True):
        runs = groups[version]
        n = len(runs)
        completed = sum((r.outcome or "").upper() == "COMPLETED" for r in runs)
        escalated = sum((r.outcome or "").upper() == "ESCALATED" for r in runs)
        errored = sum((r.outcome or "").upper() == "ERROR" for r in runs)
        times = [(max(0.0, (_utc(r.finished_at) - _utc(r.started_at)).total_seconds()) * 1000)
                 for r in runs if r.started_at and r.finished_at]
        versions.append({
            "version": version, "runs": n, "completed": completed,
            "escalated": escalated, "errors": errored,
            "completion_rate": round(completed / n, 4),
            "escalation_rate": round(escalated / n, 4),
            "average_latency_ms": round(sum(times) / len(times), 1) if times else None,
            "average_tokens": round(sum(r.total_tokens or 0 for r in runs) / n, 1),
        })
    verdict = {"direction": "insufficient_data", "reason": "Two versions with at least five runs each are required."}
    if len(versions) >= 2 and min(versions[0]["runs"], versions[1]["runs"]) >= 5:
        delta = versions[0]["completion_rate"] - versions[1]["completion_rate"]
        direction = "no_clear_change"
        if delta >= 0.15:
            direction = "better"
        elif delta <= -0.15:
            direction = "worse"
        verdict = {
            "direction": direction, "delta": round(delta, 4),
            "reason": "Heuristic threshold only; not a statistical significance test.",
            "current_version": versions[0]["version"],
            "previous_version": versions[1]["version"],
        }
    return {
        "agent_id": agent_id, "versions": versions, "verdict": verdict,
        "caution": "Completion is a workflow signal, not verified task correctness. Compare task mix and held-out evaluations before promotion.",
    }


def install_experience(app, session_for_request, scope_allowed, agent_access_allowed):
    """Attach v1 endpoints using Cortex's existing auth and access checks."""
    router = APIRouter(prefix="/api/v1/experience", tags=["Experience v1"])

    def identity(request, scope):
        sess = session_for_request(request)
        if not sess or not sess.get("user_id"):
            raise HTTPException(401, "Not authenticated")
        if not scope_allowed(sess, scope):
            raise HTTPException(403, "API key lacks required scope: " + scope)
        return sess

    def check_agent(db, sess, agent_id, write=False):
        if not agent_id:
            return None
        agent = db.query(Agent).filter(Agent.id == agent_id, Agent.is_deleted == False).first()
        if not agent:
            raise HTTPException(404, "Agent not found")
        if not agent_access_allowed(sess, agent_id, write=write):
            raise HTTPException(403, "Agent access denied")
        return agent

    def record_usage(db, owner_id, operation):
        db.add(ExperienceUsage(owner_id=owner_id, operation=operation, units=1))

    @router.get("/summary")
    def summary(request: Request):
        sess = identity(request, "experience:read")
        with SessionLocal() as db:
            owner = sess["user_id"]
            memories = db.query(ExperienceMemory).filter(ExperienceMemory.owner_id == owner).all()
            count_feedback = db.query(func.count(ExperienceFeedback.id)).filter(
                ExperienceFeedback.owner_id == owner).scalar() or 0
            return {
                "memory_count": len(memories),
                "verified_count": sum(m.status == "verified" for m in memories),
                "training_eligible": sum(m.status == "verified" and bool(m.training_opt_in) for m in memories),
                "feedback_count": count_feedback,
                "by_kind": dict(Counter(m.kind for m in memories)),
                "billable": False,
            }

    @router.get("/agents")
    def list_owned_agents(request: Request):
        sess = identity(request, "experience:read")
        with SessionLocal() as db:
            rows = (db.query(Agent).filter(Agent.owner_id == sess["user_id"],
                                          Agent.is_deleted == False)
                    .order_by(Agent.created_at.desc()).limit(100).all())
            return {"data": [{"id": a.id, "name": a.name, "version": a.version or 1} for a in rows]}

    @router.post("/memories", status_code=201)
    def create_memory(body: MemoryCreate, request: Request):
        sess = identity(request, "experience:write")
        tags = [tag.strip() for tag in body.tags]
        if any(not tag or len(tag) > 40 for tag in tags):
            raise HTTPException(422, "Tags must be between 1 and 40 characters")
        key = request.headers.get("Idempotency-Key")
        if key and (len(key) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise HTTPException(400, "Invalid Idempotency-Key")
        digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        with SessionLocal() as db:
            if body.agent_id:
                check_agent(db, sess, body.agent_id, write=True)
            if key:
                previous = db.query(ExperienceMemory).filter(
                    ExperienceMemory.owner_id == sess["user_id"],
                    ExperienceMemory.idempotency_key == key).first()
                if previous:
                    if previous.request_hash != digest:
                        raise HTTPException(409, "Idempotency key used with different content")
                    return memory_out(previous)
            memory = ExperienceMemory(
                owner_id=sess["user_id"], agent_id=body.agent_id,
                kind=body.kind, title=body.title.strip(), content=body.content.strip(),
                tags=tags, status="draft", training_opt_in=body.training_opt_in,
                idempotency_key=key, request_hash=digest if key else None)
            db.add(memory)
            record_usage(db, sess["user_id"], "memory.create")
            try:
                db.commit()
            except Exception:
                db.rollback()
                # Covers an idempotency race without duplicating accepted operations.
                if key:
                    prior = db.query(ExperienceMemory).filter(
                        ExperienceMemory.owner_id == sess["user_id"],
                        ExperienceMemory.idempotency_key == key).first()
                    if prior:
                        if prior.request_hash != digest:
                            raise HTTPException(409, "Idempotency key used with different content")
                        return memory_out(prior)
                raise
            db.refresh(memory)
            return memory_out(memory)

    @router.get("/memories")
    def list_memories(request: Request, q: str = Query(default="", max_length=200),
                      kind: Optional[str] = None, status: Optional[str] = None,
                      limit: int = Query(default=40, ge=1, le=100),
                      offset: int = Query(default=0, ge=0, le=10000)):
        sess = identity(request, "experience:read")
        if kind and kind not in {"knowledge", "preference", "correction", "skill"}:
            raise HTTPException(422, "Unsupported memory kind")
        if status and status not in {"draft", "verified"}:
            raise HTTPException(422, "Unsupported memory status")
        with SessionLocal() as db:
            records = db.query(ExperienceMemory).filter(
                ExperienceMemory.owner_id == sess["user_id"])
            if kind:
                records = records.filter(ExperienceMemory.kind == kind)
            if status:
                records = records.filter(ExperienceMemory.status == status)
            if q:
                safe_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                term = "%" + safe_q + "%"
                records = records.filter(or_(
                    ExperienceMemory.title.ilike(term, escape="\\"),
                    ExperienceMemory.content.ilike(term, escape="\\")))
            total = records.count()
            rows = records.order_by(ExperienceMemory.created_at.desc()).offset(offset).limit(limit).all()
            return {"data": [memory_out(m) for m in rows], "total": total, "limit": limit, "offset": offset,
                    "search": "lexical", "note": "Semantic retrieval is not implemented in v1."}

    @router.patch("/memories/{memory_id}/content")
    def edit_memory(memory_id: str, body: MemoryEdit, request: Request):
        sess = identity(request, "experience:write")
        title, content = body.title.strip(), body.content.strip()
        tags = [tag.strip() for tag in body.tags]
        if len(title) < 2 or len(content) < 2:
            raise HTTPException(422, "Title and content must not be blank")
        if any(not tag or len(tag) > 40 for tag in tags):
            raise HTTPException(422, "Tags must be between 1 and 40 characters")
        with SessionLocal() as db:
            memory = db.query(ExperienceMemory).filter(
                ExperienceMemory.id == memory_id,
                ExperienceMemory.owner_id == sess["user_id"]).first()
            if not memory:
                raise HTTPException(404, "Memory not found")
            if (memory.title, memory.content, memory.kind, memory.tags or []) == (
                    title, content, body.kind, tags):
                return memory_out(memory)
            memory.title = title
            memory.content = content
            memory.kind = body.kind
            memory.tags = tags
            # Once edited, the previous approval no longer applies to this content.
            memory.status = "draft"
            memory.verification_note = ""
            memory.training_opt_in = False
            record_usage(db, sess["user_id"], "memory.edit")
            db.commit()
            db.refresh(memory)
            return memory_out(memory)

    @router.patch("/memories/{memory_id}")
    def review_memory(memory_id: str, body: MemoryReview, request: Request):
        sess = identity(request, "experience:write")
        if body.status == "verified" and len(body.verification_note.strip()) < 10:
            raise HTTPException(422, "Verification requires a note of at least 10 characters")
        with SessionLocal() as db:
            memory = db.query(ExperienceMemory).filter(
                ExperienceMemory.id == memory_id,
                ExperienceMemory.owner_id == sess["user_id"]).first()
            if not memory:
                raise HTTPException(404, "Memory not found")
            memory.status = body.status
            memory.verification_note = body.verification_note.strip() if body.status == "verified" else ""
            if body.training_opt_in is not None:
                memory.training_opt_in = body.training_opt_in
            record_usage(db, sess["user_id"], "memory.review")
            db.commit()
            db.refresh(memory)
            return memory_out(memory)

    @router.delete("/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str, request: Request):
        sess = identity(request, "experience:write")
        with SessionLocal() as db:
            memory = db.query(ExperienceMemory).filter(
                ExperienceMemory.id == memory_id,
                ExperienceMemory.owner_id == sess["user_id"]).first()
            if not memory:
                raise HTTPException(404, "Memory not found")
            db.delete(memory)
            record_usage(db, sess["user_id"], "memory.delete")
            db.commit()
            return Response(status_code=204)

    @router.post("/feedback", status_code=201)
    def add_feedback(body: FeedbackCreate, request: Request):
        sess = identity(request, "experience:write")
        with SessionLocal() as db:
            run = db.query(Run).filter(Run.id == body.run_id).first()
            if not run:
                raise HTTPException(404, "Run not found")
            check_agent(db, sess, run.agent_id, write=True)
            fb = ExperienceFeedback(owner_id=sess["user_id"], run_id=run.id,
                                    rating=body.rating, note=body.note.strip())
            db.add(fb)
            record_usage(db, sess["user_id"], "feedback.create")
            db.commit()
            return {"id": fb.id, "run_id": fb.run_id, "rating": fb.rating}

    @router.get("/agents/{agent_id}/performance")
    def get_performance(agent_id: str, request: Request):
        sess = identity(request, "experience:read")
        with SessionLocal() as db:
            check_agent(db, sess, agent_id)
            return version_performance(db, agent_id)

    @router.get("/datasets/preview")
    def dataset_preview(request: Request):
        sess = identity(request, "experience:read")
        with SessionLocal() as db:
            q = db.query(ExperienceMemory).filter(
                ExperienceMemory.owner_id == sess["user_id"],
                ExperienceMemory.status == "verified",
                ExperienceMemory.training_opt_in == True)  # noqa: E712
            return {"eligible": q.count(), "format": "jsonl",
                    "policy": "Only manually verified memories explicitly opted in for training."}

    @router.get("/datasets/export")
    def export_dataset(request: Request):
        sess = identity(request, "experience:export")
        with SessionLocal() as db:
            rows = db.query(ExperienceMemory).filter(
                ExperienceMemory.owner_id == sess["user_id"],
                ExperienceMemory.status == "verified",
                ExperienceMemory.training_opt_in == True).order_by(
                    ExperienceMemory.created_at.asc()).all()  # noqa: E712
            serialized = "".join(json.dumps({
                "id": m.id, "kind": m.kind, "title": m.title, "content": m.content,
                "tags": m.tags or [], "verified_by": "human",
                "verification_note": m.verification_note,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "training_opt_in": True, "schema_version": "1",
            }, ensure_ascii=False) + "\n" for m in rows)
            record_usage(db, sess["user_id"], "dataset.export")
            db.commit()
            return Response(content=serialized, media_type="application/x-ndjson",
                            headers={"Content-Disposition": 'attachment; filename="cortex-experience.jsonl"',
                                     "Cache-Control": "no-store"})

    @router.get("/usage")
    def experience_usage(request: Request):
        sess = identity(request, "experience:read")
        with SessionLocal() as db:
            rows = db.query(ExperienceUsage.operation, func.sum(ExperienceUsage.units)).filter(
                ExperienceUsage.owner_id == sess["user_id"]).group_by(
                    ExperienceUsage.operation).all()
            return {"billable": False, "currency": "USD", "amount_charged": 0,
                    "units": [{"operation": name, "count": int(count)} for name, count in rows],
                    "note": "Diagnostic usage counters, not an invoicing or revenue-grade meter."}

    app.include_router(router)

    @app.get("/experience", response_class=HTMLResponse, include_in_schema=False)
    def experience_console(request: Request):
        if not session_for_request(request):
            return RedirectResponse(url="/", status_code=303)
        html = Path(__file__).with_name("experience_console.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
