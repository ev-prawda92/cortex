"""
CORTEX Phase 2 — Learning Foundation
═════════════════════════════════════
Relationship discovery, workflow-run persistence, and pattern recognition.

Phase 1 answers "what did this agent do?". Phase 2 answers "which agents
belong together, and which combinations actually work?".

Three layers, each usable on its own:

  1. Relationships  — who hands off to whom, derived from agent config and,
                      once workflows persist, from what actually co-executes.
  2. Workflow runs  — durable history for multi-agent runs. agent_comm's
                      MessageBus keeps workflows in memory only; nothing there
                      survives a restart, so there is nothing to learn from.
                      WorkflowRun/WorkflowStepRun are that missing substrate.
  3. Patterns       — recurring agent sequences scored by success rate,
                      latency and cost, built on top of layer 2.

Tables are declared against db.Base, so `Base.metadata.create_all` (app startup)
and `migrate.py` both pick them up with no changes to either.

Multi-tenancy follows the rest of CORTEX: rows are scoped by `owner_id`
(-> users.id). There is no tenant table and IDs are String(64) from gen_id().

Usage:
    from phase2 import discovery, patterns
    discovery.discover_from_config(db, owner_id="u1")
    patterns.analyze(db, owner_id="u1")
"""

import json
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

from sqlalchemy import (
    Column, String, Text, Boolean, Integer, Float, DateTime, JSON,
    ForeignKey, Index,
)
from sqlalchemy.orm import Session, relationship

from db import Base, gen_id, utcnow, Agent, Run


# ═══════════════════════════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════════════════════════

class AgentRelationship(Base):
    """A discovered edge between two agents.

    Edges are derived, never hand-authored: config analysis finds declared
    handoffs and shared data sources; runtime analysis finds what actually
    co-executes. Re-running discovery updates strength in place rather than
    duplicating the edge.
    """
    __tablename__ = "agent_relationships"

    id = Column(String(64), primary_key=True, default=gen_id)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)

    source_agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)
    target_agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)

    # escalation | shared_data_source | workflow_dependency | co_execution
    rel_type = Column(String(32), nullable=False)
    # config | runtime
    discovered_from = Column(String(16), nullable=False, default="config")

    strength = Column(Integer, default=50)        # 0-100
    confidence = Column(Float, default=0.5)       # 0.0-1.0
    evidence = Column(JSON, default=dict)         # why we think this: {source_key, sample_ids, ...}

    observation_count = Column(Integer, default=1)
    last_observed_at = Column(DateTime(timezone=True), default=utcnow)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_agent_rel_pair", "source_agent_id", "target_agent_id", "rel_type"),
        Index("ix_agent_rel_owner_strength", "owner_id", "strength"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "source": self.source_agent_id,
            "target": self.target_agent_id,
            "type": self.rel_type,
            "discovered_from": self.discovered_from,
            "strength": self.strength,
            "confidence": round(self.confidence or 0.0, 3),
            "observations": self.observation_count,
            "evidence": self.evidence or {},
        }


class WorkflowRun(Base):
    """One execution of a multi-agent workflow — the durable record.

    agent_comm.MessageBus holds workflows in a process dict with counter-based
    IDs (wf-0001), so history dies with the process and IDs collide across
    restarts. A WorkflowRun row is written when a workflow starts and updated
    as it advances, giving Phase 2 something to learn from and giving users a
    workflow history that survives a deploy.
    """
    __tablename__ = "workflow_runs"

    id = Column(String(64), primary_key=True, default=gen_id)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)

    # The in-memory MessageBus id (wf-0001), kept for correlation while a
    # process is alive. Not unique across restarts — never use it as a key.
    bus_workflow_id = Column(String(64), default="", index=True)

    name = Column(String(255), default="")
    definition = Column(JSON, default=dict)   # the step DAG as submitted
    status = Column(String(16), default="running")  # running | completed | failed | partial

    # Denormalized rollups, filled in on completion
    agent_sequence = Column(JSON, default=list)   # agent ids in completion order
    pattern_hash = Column(String(64), default="", index=True)
    step_count = Column(Integer, default=0)
    steps_succeeded = Column(Integer, default=0)
    steps_failed = Column(Integer, default=0)
    latency_ms = Column(Float, default=0.0)
    cost_usd = Column(Float, default=0.0)
    total_tokens = Column(Integer, default=0)

    started_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    steps = relationship("WorkflowStepRun", back_populates="workflow_run",
                         cascade="all, delete-orphan",
                         order_by="WorkflowStepRun.started_at")

    __table_args__ = (
        Index("ix_wfrun_owner_started", "owner_id", "started_at"),
    )

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.steps_failed == 0

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "agents": self.agent_sequence or [],
            "steps": self.step_count,
            "succeeded": self.steps_succeeded,
            "failed": self.steps_failed,
            "latency_ms": round(self.latency_ms or 0.0, 1),
            "cost_usd": round(self.cost_usd or 0.0, 6),
            "total_tokens": self.total_tokens or 0,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class WorkflowStepRun(Base):
    """One step of a workflow run, linked to the Run row it produced.

    This link is what makes multi-agent history queryable: without it, a
    workflow's agent executions are indistinguishable from unrelated runs.
    """
    __tablename__ = "workflow_step_runs"

    id = Column(String(64), primary_key=True, default=gen_id)
    workflow_run_id = Column(String(64), ForeignKey("workflow_runs.id"),
                             nullable=False, index=True)
    # The Run this step produced. Nullable: a skipped step never runs.
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=True, index=True)

    step_key = Column(String(64), default="")      # step id within the DAG
    agent_id = Column(String(64), ForeignKey("agents.id"), nullable=False, index=True)
    depends_on = Column(JSON, default=list)        # step keys this waited on
    depth = Column(Integer, default=0)             # DAG depth — steps at equal depth ran concurrently

    status = Column(String(16), default="pending")  # pending|running|completed|failed|skipped
    error = Column(Text, default="")

    latency_ms = Column(Float, default=0.0)
    cost_usd = Column(Float, default=0.0)
    total_tokens = Column(Integer, default=0)

    started_at = Column(DateTime(timezone=True), default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    workflow_run = relationship("WorkflowRun", back_populates="steps")

    def to_dict(self):
        return {
            "step": self.step_key,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "status": self.status,
            "depends_on": self.depends_on or [],
            "depth": self.depth,
            "latency_ms": round(self.latency_ms or 0.0, 1),
            "cost_usd": round(self.cost_usd or 0.0, 6),
            "error": self.error or "",
        }


class AgentPattern(Base):
    """A recurring agent sequence, scored across its executions."""
    __tablename__ = "agent_patterns"

    id = Column(String(64), primary_key=True, default=gen_id)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)

    pattern_hash = Column(String(64), nullable=False, index=True)
    name = Column(String(255), default="")
    agent_sequence = Column(JSON, default=list)

    execution_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)
    avg_latency_ms = Column(Float, default=0.0)
    avg_cost_usd = Column(Float, default=0.0)

    # "improving" | "declining" | "stable" | "insufficient_data"
    trend = Column(String(24), default="insufficient_data")
    # Groups of step keys observed running at the same DAG depth
    parallel_groups = Column(JSON, default=list)

    first_seen_at = Column(DateTime(timezone=True), default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_pattern_owner_hash", "owner_id", "pattern_hash"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "agents": self.agent_sequence or [],
            "executions": self.execution_count,
            "success_rate": round(self.success_rate or 0.0, 3),
            "avg_latency_ms": round(self.avg_latency_ms or 0.0, 1),
            "avg_cost_usd": round(self.avg_cost_usd or 0.0, 6),
            "trend": self.trend,
            "parallel_groups": self.parallel_groups or [],
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class RunFeedback(Base):
    """Human judgment on a run or workflow run.

    Metrics say a workflow completed; only a person says it was correct.
    Feedback is what keeps pattern scores honest when a run "succeeds" with a
    bad answer.
    """
    __tablename__ = "run_feedback"

    id = Column(String(64), primary_key=True, default=gen_id)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)

    run_id = Column(String(64), ForeignKey("runs.id"), nullable=True, index=True)
    workflow_run_id = Column(String(64), ForeignKey("workflow_runs.id"),
                             nullable=True, index=True)
    pattern_id = Column(String(64), ForeignKey("agent_patterns.id"),
                        nullable=True, index=True)

    verdict = Column(String(16), nullable=False)   # correct | incorrect | partial
    rating = Column(Integer, nullable=True)        # optional 1-5
    comment = Column(Text, default="")
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "run_id": self.run_id,
            "workflow_run_id": self.workflow_run_id,
            "verdict": self.verdict,
            "rating": self.rating,
            "comment": self.comment,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def as_aware(dt):
    """Return dt as timezone-aware UTC.

    SQLite does not preserve tzinfo on DateTime(timezone=True) columns, so a
    value written aware reads back naive. Postgres round-trips it correctly.
    CORTEX supports both, so every datetime subtraction goes through here.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def sequence_hash(agent_ids: List[str]) -> str:
    """Stable hash of an agent sequence. Order matters — A->B is not B->A."""
    return hashlib.sha256("|".join(agent_ids).encode()).hexdigest()[:32]


def run_latency_ms(run) -> float:
    """Derive latency from a Run's timestamps. Runs store no latency column."""
    if not run or not run.started_at or not run.finished_at:
        return 0.0
    return max(0.0, (as_aware(run.finished_at)
                     - as_aware(run.started_at)).total_seconds() * 1000.0)


_PRICING_WARNED = False


def run_cost_usd(run) -> float:
    """Price a Run from its token counts using the usage module's price table.

    A pricing failure returns 0.0 but warns once. Silently returning 0.0 would
    render a dashboard full of confident, wrong "$0.00" costs — a broken price
    lookup should be visible, not free.
    """
    global _PRICING_WARNED
    if not run:
        return 0.0
    try:
        from usage import usage_tracker
        return usage_tracker.calculator.cost(run.model or "",
                                             run.input_tokens or 0,
                                             run.output_tokens or 0)
    except Exception as e:
        if not _PRICING_WARNED:
            _PRICING_WARNED = True
            print(f"[phase2] cost lookup unavailable, reporting 0.0: {e}")
        return 0.0


# ═══════════════════════════════════════════════════════════════
#  RELATIONSHIP DISCOVERY
# ═══════════════════════════════════════════════════════════════

class RelationshipDiscovery:
    """Derives agent relationships from config and from execution history.

    Config discovery reads three signals that actually exist in a CORTEX
    agent config:

      escalation.route_to  — an explicit handoff target. The strongest signal
                             available without running anything.
      data_sources         — two agents pointed at the same endpoint are
                             coupled through that data whether or not they
                             ever call each other.
      tools                — shared tool names hint at overlapping capability.
                             Weak on its own; useful as a tiebreaker.

    Runtime discovery reads persisted WorkflowRun history. It stays quiet
    until workflows are actually being persisted — no history, no edges,
    rather than invented ones.
    """

    # ── config ────────────────────────────────────────────────

    def discover_from_config(self, db: Session, owner_id: str = None) -> dict:
        """Scan every agent's config for declared relationships.

        Returns {"created": n, "updated": n, "edges": [...]}.
        """
        q = db.query(Agent).filter(Agent.is_deleted == False)  # noqa: E712
        if owner_id:
            q = q.filter(Agent.owner_id == owner_id)
        agents = q.all()

        if len(agents) < 2:
            return {"created": 0, "updated": 0, "edges": [],
                    "note": "need at least 2 agents to find relationships"}

        # Resolve a route_to value against id / slug / name (case-insensitive).
        lookup = {}
        for a in agents:
            for key in (a.id, a.slug, a.name):
                if key:
                    lookup[str(key).strip().lower()] = a.id

        found: List[Tuple[str, str, str, int, float, dict]] = []

        for a in agents:
            cfg = a.config or {}

            # 1. Explicit escalation handoff.
            target_raw = ((cfg.get("escalation") or {}).get("route_to") or "")
            target_id = lookup.get(str(target_raw).strip().lower()) if target_raw else None
            if target_id and target_id != a.id:
                found.append((a.id, target_id, "escalation", 90, 0.95,
                              {"config_key": "escalation.route_to",
                               "value": str(target_raw)}))

            # 2. Shared data sources — compare declared endpoints.
            for src in (cfg.get("data_sources") or []):
                if not isinstance(src, dict):
                    continue
                ident = (src.get("endpoint") or src.get("name") or "").strip().lower()
                if ident:
                    found.append((a.id, ident, "__datasource__", 0, 0.0, {}))

        # Shared data sources from the DataSource table as well as config.
        try:
            from db import DataSource
            agent_ids = {a.id for a in agents}
            for ds in db.query(DataSource).all():
                if ds.agent_id in agent_ids:
                    ident = (ds.endpoint or ds.name or "").strip().lower()
                    if ident:
                        found.append((ds.agent_id, ident, "__datasource__", 0, 0.0, {}))
        except Exception:
            pass

        # Fold the datasource markers into pairwise edges.
        by_source: Dict[str, set] = {}
        real_edges = []
        for src, tgt, kind, strength, conf, ev in found:
            if kind == "__datasource__":
                by_source.setdefault(tgt, set()).add(src)
            else:
                real_edges.append((src, tgt, kind, strength, conf, ev))

        for ident, sharers in by_source.items():
            if len(sharers) < 2:
                continue
            ordered = sorted(sharers)
            for i, a_id in enumerate(ordered):
                for b_id in ordered[i + 1:]:
                    ev = {"shared_data_source": ident}
                    # Symmetric coupling — record both directions.
                    real_edges.append((a_id, b_id, "shared_data_source", 45, 0.6, ev))
                    real_edges.append((b_id, a_id, "shared_data_source", 45, 0.6, ev))

        created = updated = 0
        for src, tgt, kind, strength, conf, ev in real_edges:
            if self._upsert(db, owner_id, src, tgt, kind, "config",
                            strength, conf, ev):
                created += 1
            else:
                updated += 1
        db.commit()

        return {"created": created, "updated": updated,
                "edges": len(real_edges), "agents_scanned": len(agents)}

    # ── runtime ───────────────────────────────────────────────

    def discover_from_runtime(self, db: Session, owner_id: str = None,
                              lookback_days: int = 30) -> dict:
        """Derive edges from persisted workflow history.

        Two signals: a step that depends on another (an explicit DAG edge that
        actually executed), and agents that repeatedly appear in the same
        workflow run (co-execution).
        """
        cutoff = utcnow() - timedelta(days=lookback_days)
        q = (db.query(WorkflowRun)
             .filter(WorkflowRun.started_at >= cutoff))
        if owner_id:
            q = q.filter(WorkflowRun.owner_id == owner_id)
        runs = q.all()

        if not runs:
            return {"created": 0, "updated": 0, "workflow_runs": 0,
                    "note": "no persisted workflow runs yet — "
                            "runtime discovery needs workflow history"}

        dep_edges: Dict[Tuple[str, str], int] = {}
        co_edges: Dict[Tuple[str, str], int] = {}

        for wr in runs:
            steps = wr.steps
            by_key = {s.step_key: s for s in steps if s.step_key}

            # Explicit dependency edges that actually ran.
            for s in steps:
                for dep_key in (s.depends_on or []):
                    upstream = by_key.get(dep_key)
                    if upstream and upstream.agent_id != s.agent_id:
                        k = (upstream.agent_id, s.agent_id)
                        dep_edges[k] = dep_edges.get(k, 0) + 1

            # Co-execution within one workflow run.
            agent_ids = sorted({s.agent_id for s in steps if s.agent_id})
            for i, a_id in enumerate(agent_ids):
                for b_id in agent_ids[i + 1:]:
                    co_edges[(a_id, b_id)] = co_edges.get((a_id, b_id), 0) + 1

        total = len(runs)
        created = updated = 0

        for (src, tgt), count in dep_edges.items():
            # A dependency observed every time is near-certain.
            conf = min(0.98, 0.5 + (count / max(total, 1)) * 0.5)
            strength = min(100, 60 + int((count / max(total, 1)) * 40))
            if self._upsert(db, owner_id, src, tgt, "workflow_dependency",
                            "runtime", strength, conf,
                            {"observed_in_runs": count, "of_total": total},
                            observation_count=count):
                created += 1
            else:
                updated += 1

        for (a_id, b_id), count in co_edges.items():
            freq = count / max(total, 1)
            if freq < 0.3:
                continue  # too rare to mean anything
            strength = min(100, int(freq * 80))
            conf = min(0.9, 0.3 + freq * 0.6)
            ev = {"co_executed_in_runs": count, "of_total": total}
            for s, t in ((a_id, b_id), (b_id, a_id)):
                if self._upsert(db, owner_id, s, t, "co_execution", "runtime",
                                strength, conf, ev, observation_count=count):
                    created += 1
                else:
                    updated += 1

        db.commit()
        return {"created": created, "updated": updated, "workflow_runs": total}

    # ── persistence ───────────────────────────────────────────

    def _upsert(self, db: Session, owner_id, source_id, target_id, rel_type,
                discovered_from, strength, confidence, evidence,
                observation_count: int = 1) -> bool:
        """Insert or update one edge. Returns True if newly created.

        An existing edge is reinforced, not replaced: strength moves toward the
        new reading instead of jumping to it, so one odd observation cannot
        erase a long history.
        """
        existing = (db.query(AgentRelationship)
                    .filter(AgentRelationship.source_agent_id == source_id,
                            AgentRelationship.target_agent_id == target_id,
                            AgentRelationship.rel_type == rel_type)
                    .first())

        if existing:
            existing.strength = int((existing.strength + strength) / 2)
            existing.confidence = (existing.confidence + confidence) / 2
            existing.observation_count = (existing.observation_count or 0) + observation_count
            existing.discovered_from = discovered_from
            existing.evidence = evidence or existing.evidence
            existing.last_observed_at = utcnow()
            return False

        db.add(AgentRelationship(
            owner_id=owner_id,
            source_agent_id=source_id,
            target_agent_id=target_id,
            rel_type=rel_type,
            discovered_from=discovered_from,
            strength=strength,
            confidence=confidence,
            evidence=evidence or {},
            observation_count=observation_count,
        ))
        return True

    # ── reads ─────────────────────────────────────────────────

    def for_agent(self, db: Session, agent_id: str) -> dict:
        """Edges touching one agent, split by direction."""
        out = (db.query(AgentRelationship)
               .filter(AgentRelationship.source_agent_id == agent_id)
               .order_by(AgentRelationship.strength.desc()).all())
        inc = (db.query(AgentRelationship)
               .filter(AgentRelationship.target_agent_id == agent_id)
               .order_by(AgentRelationship.strength.desc()).all())
        return {"agent_id": agent_id,
                "outgoing": [r.to_dict() for r in out],
                "incoming": [r.to_dict() for r in inc]}

    def graph(self, db: Session, owner_id: str = None,
              min_strength: int = 0) -> dict:
        """The whole relationship graph, shaped for a force/DAG layout."""
        q = db.query(AgentRelationship).filter(
            AgentRelationship.strength >= min_strength)
        if owner_id:
            q = q.filter(AgentRelationship.owner_id == owner_id)
        edges = q.all()

        agent_ids = set()
        for e in edges:
            agent_ids.add(e.source_agent_id)
            agent_ids.add(e.target_agent_id)

        nodes = []
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                nodes.append({"id": a.id, "name": a.name, "slug": a.slug,
                              "status": a.status, "live": bool(a.live)})

        return {"nodes": nodes, "edges": [e.to_dict() for e in edges],
                "generated_at": utcnow().isoformat()}


# ═══════════════════════════════════════════════════════════════
#  WORKFLOW RUN RECORDER
# ═══════════════════════════════════════════════════════════════

class WorkflowRecorder:
    """Writes durable history for multi-agent workflow executions.

    agent_comm.MessageBus advances a workflow one wave of ready steps per
    run_workflow() call and keeps everything in memory. The recorder mirrors
    that into WorkflowRun/WorkflowStepRun so the history outlives the process
    and each step points at the Run row it produced.

    Wire-up in cortex.py's /api/comms/workflows/{id}/run endpoint:

        wr = recorder.begin(db, wf_dict, owner_id=user_id)
        result = message_bus.run_workflow(workflow_id)
        recorder.record_wave(db, wr, result, wf_dict)
        recorder.finalize(db, wr)
    """

    def begin(self, db: Session, wf: dict, owner_id: str = None,
              definition: list = None) -> WorkflowRun:
        """Open (or reuse) the WorkflowRun row for a bus workflow.

        `definition` is the original step list, untruncated. Storing it is what
        makes a run replayable — the dict from to_dict() clips instructions to
        200 chars, so replaying from that would silently alter the workflow.
        """
        bus_id = wf.get("id", "")
        existing = None
        if bus_id:
            existing = (db.query(WorkflowRun)
                        .filter(WorkflowRun.bus_workflow_id == bus_id,
                                WorkflowRun.status == "running")
                        .order_by(WorkflowRun.started_at.desc())
                        .first())
        if existing:
            return existing

        wr = WorkflowRun(
            owner_id=owner_id,
            bus_workflow_id=bus_id,
            name=wf.get("name", ""),
            definition=(definition if definition else wf.get("steps", {})),
            status="running",
            step_count=len(wf.get("steps", {}) or {}),
        )
        db.add(wr)
        db.commit()
        return wr

    def record_wave(self, db: Session, wr: WorkflowRun, result: dict,
                    wf: dict) -> None:
        """Persist the steps that just executed in one run_workflow() call."""
        executed = (result or {}).get("executed", {}) or {}
        steps_def = (wf or {}).get("steps", {}) or {}

        for step_key, outcome in executed.items():
            sdef = steps_def.get(step_key, {}) or {}
            agent_id = sdef.get("agent_id", "")
            if not agent_id:
                continue

            depends_on = sdef.get("depends_on", []) or []
            depth = self._depth(step_key, steps_def)
            ok = bool(outcome.get("ok"))

            # Pair the step with the Run row the agent just produced.
            # Constrained to runs that began at or after this workflow run did,
            # and to ones not already claimed by another step, so unrelated
            # concurrent activity on the same agent cannot be mislinked. This
            # is still a heuristic: the bus does not hand back a run id. The
            # durable fix is for run_workflow() to return one per step.
            claimed = {r[0] for r in db.query(WorkflowStepRun.run_id)
                       .filter(WorkflowStepRun.run_id.isnot(None)).all()}
            rq = (db.query(Run)
                  .filter(Run.agent_id == agent_id)
                  .order_by(Run.started_at.desc()))
            if wr.started_at:
                rq = rq.filter(Run.started_at >= wr.started_at)
            run_row = next((r for r in rq.limit(25).all()
                            if r.id not in claimed), None)

            step = WorkflowStepRun(
                workflow_run_id=wr.id,
                run_id=run_row.id if run_row else None,
                step_key=step_key,
                agent_id=agent_id,
                depends_on=depends_on,
                depth=depth,
                status="completed" if ok else "failed",
                error=str(outcome.get("error", ""))[:2000],
                latency_ms=run_latency_ms(run_row),
                cost_usd=run_cost_usd(run_row),
                total_tokens=(run_row.total_tokens or 0) if run_row else 0,
                finished_at=utcnow(),
            )
            db.add(step)

        db.commit()

    def finalize(self, db: Session, wr: WorkflowRun) -> WorkflowRun:
        """Close out a workflow run and roll up its metrics."""
        steps = (db.query(WorkflowStepRun)
                 .filter(WorkflowStepRun.workflow_run_id == wr.id)
                 .order_by(WorkflowStepRun.started_at).all())

        wr.steps_succeeded = sum(1 for s in steps if s.status == "completed")
        wr.steps_failed = sum(1 for s in steps if s.status == "failed")
        wr.step_count = max(wr.step_count or 0, len(steps))
        wr.agent_sequence = [s.agent_id for s in steps if s.agent_id]
        wr.pattern_hash = sequence_hash(wr.agent_sequence) if wr.agent_sequence else ""
        wr.cost_usd = sum(s.cost_usd or 0.0 for s in steps)
        wr.total_tokens = sum(s.total_tokens or 0 for s in steps)
        wr.finished_at = utcnow()

        if wr.started_at and wr.finished_at:
            wr.latency_ms = max(
                0.0, (as_aware(wr.finished_at)
                      - as_aware(wr.started_at)).total_seconds() * 1000.0)

        if wr.steps_failed and wr.steps_succeeded:
            wr.status = "partial"
        elif wr.steps_failed:
            wr.status = "failed"
        else:
            wr.status = "completed"

        db.commit()
        return wr

    def _depth(self, step_key: str, steps_def: dict, _seen=None) -> int:
        """DAG depth of a step. Steps at equal depth could have run together."""
        _seen = _seen or set()
        if step_key in _seen:
            return 0  # cycle guard
        _seen.add(step_key)
        deps = (steps_def.get(step_key, {}) or {}).get("depends_on", []) or []
        if not deps:
            return 0
        return 1 + max((self._depth(d, steps_def, _seen) for d in deps),
                       default=-1)

    def replay_payload(self, wr: WorkflowRun) -> list:
        """Rebuild a create_workflow() step list from a stored definition.

        Handles both shapes: the raw list captured at creation, and the older
        dict-of-steps form from to_dict() for runs recorded before replay
        existed. The dict form has truncated instructions — accurate to what
        was recorded, but not necessarily to what was authored.
        """
        d = wr.definition
        if isinstance(d, list):
            return [dict(s) for s in d]
        if isinstance(d, dict):
            out = []
            for key, s in d.items():
                if not isinstance(s, dict):
                    continue
                out.append({"id": key,
                            "agent_id": s.get("agent_id", ""),
                            "instruction": s.get("instruction", "") or "Proceed.",
                            "depends_on": s.get("depends_on", []) or []})
            return out
        return []

    def history(self, db: Session, owner_id: str = None, limit: int = 50) -> list:
        """Recent workflow runs, newest first."""
        q = db.query(WorkflowRun)
        if owner_id:
            q = q.filter(WorkflowRun.owner_id == owner_id)
        runs = q.order_by(WorkflowRun.started_at.desc()).limit(limit).all()
        return [r.to_dict() for r in runs]


# ═══════════════════════════════════════════════════════════════
#  PATTERN ANALYSIS
# ═══════════════════════════════════════════════════════════════

class PatternAnalyzer:
    """Scores recurring agent sequences from persisted workflow history.

    A pattern needs repetition to mean anything. Sequences seen once are
    tracked but reported as insufficient_data rather than dressed up with a
    100% success rate — one run is an anecdote, not a rate.
    """

    MIN_EXECUTIONS_FOR_TREND = 4

    def analyze(self, db: Session, owner_id: str = None,
                lookback_days: int = 30, min_executions: int = 2) -> dict:
        """Group workflow runs by agent sequence and score each group."""
        cutoff = utcnow() - timedelta(days=lookback_days)
        q = (db.query(WorkflowRun)
             .filter(WorkflowRun.started_at >= cutoff,
                     WorkflowRun.status.in_(["completed", "partial", "failed"])))
        if owner_id:
            q = q.filter(WorkflowRun.owner_id == owner_id)
        runs = q.order_by(WorkflowRun.started_at).all()

        if not runs:
            return {"patterns": 0, "created": 0, "updated": 0,
                    "note": "no completed workflow runs in window"}

        groups: Dict[str, List[WorkflowRun]] = {}
        for wr in runs:
            seq = wr.agent_sequence or []
            if not seq:
                continue
            groups.setdefault(wr.pattern_hash or sequence_hash(seq), []).append(wr)

        created = updated = skipped = 0
        for phash, group in groups.items():
            if len(group) < min_executions:
                skipped += 1
                continue
            if self._upsert_pattern(db, owner_id, phash, group):
                created += 1
            else:
                updated += 1

        db.commit()
        return {"patterns": created + updated, "created": created,
                "updated": updated, "below_threshold": skipped,
                "runs_analyzed": len(runs)}

    def _upsert_pattern(self, db: Session, owner_id, phash: str,
                        group: List[WorkflowRun]) -> bool:
        seq = group[0].agent_sequence or []
        n = len(group)
        successes = sum(1 for r in group if r.succeeded)

        pat = (db.query(AgentPattern)
               .filter(AgentPattern.pattern_hash == phash,
                       AgentPattern.owner_id == owner_id).first())
        is_new = pat is None
        if is_new:
            pat = AgentPattern(owner_id=owner_id, pattern_hash=phash,
                               first_seen_at=group[0].started_at or utcnow())
            db.add(pat)

        pat.agent_sequence = seq
        pat.name = self._name_for(db, seq)
        pat.execution_count = n
        pat.success_count = successes
        pat.success_rate = successes / n if n else 0.0
        pat.avg_latency_ms = sum(r.latency_ms or 0.0 for r in group) / n
        pat.avg_cost_usd = sum(r.cost_usd or 0.0 for r in group) / n
        pat.trend = self._trend(group)
        pat.parallel_groups = self._parallel_groups(db, group)
        pat.last_seen_at = group[-1].started_at or utcnow()
        return is_new

    def _name_for(self, db: Session, seq: List[str]) -> str:
        """Human-readable name using agent names rather than raw ids."""
        if not seq:
            return "(empty)"
        names = {}
        for a in db.query(Agent).filter(Agent.id.in_(set(seq))).all():
            names[a.id] = a.name or a.slug or a.id
        labels = [names.get(a_id, a_id[:8]) for a_id in seq]
        if len(labels) <= 3:
            return " → ".join(labels)
        return f"{labels[0]} → … → {labels[-1]} ({len(labels)} agents)"

    def _trend(self, group: List[WorkflowRun]) -> str:
        """Compare success in the older half against the newer half."""
        if len(group) < self.MIN_EXECUTIONS_FOR_TREND:
            return "insufficient_data"
        ordered = sorted(group, key=lambda r: as_aware(r.started_at) or utcnow())
        mid = len(ordered) // 2
        first, second = ordered[:mid], ordered[mid:]
        if not first or not second:
            return "insufficient_data"
        a = sum(1 for r in first if r.succeeded) / len(first)
        b = sum(1 for r in second if r.succeeded) / len(second)
        if b - a > 0.1:
            return "improving"
        if a - b > 0.1:
            return "declining"
        return "stable"

    def _parallel_groups(self, db: Session, group: List[WorkflowRun]) -> list:
        """Agents observed at the same DAG depth — these already ran concurrently.

        This reports what the recorded DAG actually did. It does not infer that
        independent-looking agents *could* be parallelized; that needs data-flow
        analysis we do not have yet.
        """
        wr = group[-1]
        steps = (db.query(WorkflowStepRun)
                 .filter(WorkflowStepRun.workflow_run_id == wr.id).all())
        by_depth: Dict[int, List[str]] = {}
        for s in steps:
            by_depth.setdefault(s.depth or 0, []).append(s.agent_id)
        return [sorted(v) for _, v in sorted(by_depth.items()) if len(v) > 1]

    # ── reads ─────────────────────────────────────────────────

    def top(self, db: Session, owner_id: str = None, limit: int = 10,
            min_success_rate: float = 0.0) -> list:
        """Patterns ranked by success rate weighted by how often they ran."""
        q = db.query(AgentPattern).filter(
            AgentPattern.success_rate >= min_success_rate)
        if owner_id:
            q = q.filter(AgentPattern.owner_id == owner_id)
        pats = q.all()
        pats.sort(key=lambda p: (p.success_rate or 0) * (p.execution_count or 0),
                  reverse=True)
        return [p.to_dict() for p in pats[:limit]]

    def for_agents(self, db: Session, agent_ids: List[str],
                   owner_id: str = None) -> list:
        """Patterns containing every one of these agents, best first."""
        wanted = set(agent_ids)
        q = db.query(AgentPattern)
        if owner_id:
            q = q.filter(AgentPattern.owner_id == owner_id)
        out = []
        for p in q.all():
            if wanted.issubset(set(p.agent_sequence or [])):
                out.append(p)
        out.sort(key=lambda p: (p.success_rate or 0) * (p.execution_count or 0),
                 reverse=True)
        return [p.to_dict() for p in out]


# ── module singletons, matching the rest of CORTEX ────────────
discovery = RelationshipDiscovery()
recorder = WorkflowRecorder()
patterns = PatternAnalyzer()
