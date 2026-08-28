"""
CORTEX Adaptive Runtime (CAR)
=============================
The proprietary intelligence engine that powers Cortex.

CAR sits between the developer's agent calls and the upstream LLM providers,
observing every run to build statistical profiles, detect drift, route to the
optimal model, and predict outcomes before execution. It is the reason Cortex
is infrastructure you cannot remove once adopted: without it, you lose
behavioral visibility, cost optimization, and predictive guardrails — all of
which accumulate value the longer CAR runs.

Architecture
------------
CAR is four interlocking systems, each drawn from battle-tested statistical
methods across Evan Prawda's prior engines:

1. BEHAVIORAL FINGERPRINTING  (from Sendero's two-level outlier detection
   + Equity Lens invariance envelopes)
   Per-agent statistical profiles that update with every run. Drift detection
   uses coefficient-of-variation analysis and MAD-based fencing so you know
   the moment an agent starts behaving differently — before users complain.

2. ADAPTIVE ROUTING  (from NWC Quant's portfolio optimization + Sendero's
   eta-squared explanatory decomposition)
   Thompson-sampling multi-armed bandit that learns which provider/model
   combination is optimal for each task type, balancing success rate, latency,
   and cost. Selection-bias aware: the routing signal is deflated so a model
   that "looks best" after N trials is held to the same statistical bar as
   NWC Quant's deflated Sharpe ratio.

3. RUN PREDICTION  (from Arbiter's deterministic multi-lever scoring)
   Pre-execution estimates of success probability, expected token cost, and
   latency. Three scored levers (prompt complexity, historical fit, provider
   health) combine into a composite confidence score with governed weights —
   the same architecture that powers Arbiter's contract integrity analysis.

4. GOVERNANCE & AUDIT  (from Arbiter's versioned policy + resolution ledger)
   Every routing decision and prediction is logged to an immutable audit
   trail with SHA-256 signatures. Policy weights are tunable but every change
   is versioned. Platform-wide monitoring surfaces where failures cluster,
   which agents drift, and what to fix first — the same observability pattern
   as Arbiter's monitoring.py.

Integration
-----------
CAR exposes a clean internal API that cortex.py calls:

    from car import CAR
    engine = CAR(db_session)
    engine.record_run(agent_id, run_data)          # after every run
    fp = engine.fingerprint(agent_id)              # behavioral profile
    route = engine.route(agent_id, task_context)   # optimal provider
    pred = engine.predict(agent_id, task_context)  # pre-execution estimate

For external consumers, cortex.py wraps these as REST endpoints. CAR itself
has zero web dependencies — it is pure computation over data.

Patent-relevant prior art
-------------------------
- Sendero classification engine (two-level outlier detection, eta-squared
  decomposition, permutation-based null distribution testing)
- NWC Quant validation suite (deflated Sharpe ratio, walk-forward, Newey-West
  alpha regression)
- Arbiter resolution-integrity engine (multi-lever deterministic scoring,
  governed policy, auditable resolution trails)
- Equity Lens invariance methodology (Type A/B/C counterfactual taxonomy,
  per-case invariance specifications, drift envelopes)
"""

import hashlib
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — STATISTICAL PRIMITIVES
# Ported from Sendero classify.py and NWC Quant metrics.py. These are the
# low-level math functions every higher system depends on.
# ═══════════════════════════════════════════════════════════════════════════

def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0

def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))

def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def _mad(xs: List[float]) -> float:
    """Median absolute deviation — robust spread measure immune to outliers."""
    if not xs:
        return 0.0
    med = _median(xs)
    return _median([abs(x - med) for x in xs])

def _cv(xs: List[float]) -> float:
    """Coefficient of variation: std/mean. From Sendero's dispersion test."""
    m = _mean(xs)
    return _std(xs) / m if m else 0.0

def _eta_squared(values: List[Optional[float]], groups: List[str]) -> float:
    """Fraction of variance explained by group membership.
    Ported from Sendero classify.py — the core of the explanatory test.
    One-way ANOVA decomposition: SS_between / SS_total. Range [0,1].
    """
    paired = [(v, g) for v, g in zip(values, groups)
              if v is not None and g not in (None, "")]
    if len(paired) < 3:
        return 0.0
    vals = [v for v, _ in paired]
    grand = _mean(vals)
    ss_total = sum((v - grand) ** 2 for v in vals)
    if ss_total == 0:
        return 0.0
    buckets: Dict[str, List[float]] = defaultdict(list)
    for v, g in paired:
        buckets[g].append(v)
    if len(buckets) < 2:
        return 0.0
    ss_between = sum(len(b) * (_mean(b) - grand) ** 2 for b in buckets.values())
    return max(0.0, min(1.0, ss_between / ss_total))

def _tail_share(values: List[float], fence_k: float = 2.5) -> float:
    """Fraction of values beyond median + k*MAD fence.
    From Sendero's tail_share — detects isolated outlier populations.
    """
    if len(values) < 3:
        return 0.0
    med = _median(values)
    mad = _mad(values) or 1e-9
    fence = med + fence_k * mad
    return sum(1 for v in values if v > fence) / len(values)

def _ewma(prev: float, new: float, alpha: float = 0.15) -> float:
    """Exponentially weighted moving average for streaming updates."""
    return alpha * new + (1 - alpha) * prev

def _percentile(xs: List[float], p: float) -> float:
    """p in [0,100]."""
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — BEHAVIORAL FINGERPRINTING
# From Sendero's two-level classification + Equity Lens invariance envelopes.
#
# Every agent accumulates a statistical fingerprint: distributions of token
# usage, latency, success rate, and cost. The fingerprint defines "normal"
# for that agent. Drift detection fires when recent behavior departs from
# the established envelope — the same idea as Sendero's dispersion test
# (is the population uniformly affected, or is there a long tail?) and
# Equity Lens's invariance spec (must_be_invariant vs may_legitimately_diverge).
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BehavioralEnvelope:
    """Invariance envelope for one metric dimension of an agent.
    Inspired by Equity Lens per-case invariance_spec: defines the bounds
    within which behavior is expected, and the threshold beyond which
    drift is flagged.
    """
    metric: str
    median: float = 0.0
    mad: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    p5: float = 0.0
    p95: float = 0.0
    cv: float = 0.0
    n: int = 0
    # Invariance bounds (from Equity Lens methodology):
    # must_be_invariant — hard bounds that should never be crossed
    # may_diverge — soft bounds where some variation is expected
    fence_hard: float = 0.0    # median + 3.5 * MAD — "must be invariant"
    fence_soft: float = 0.0    # median + 2.0 * MAD — "may diverge, watch"

    @classmethod
    def from_values(cls, metric: str, values: List[float]) -> "BehavioralEnvelope":
        if len(values) < 3:
            return cls(metric=metric, n=len(values))
        med = _median(values)
        mad = _mad(values) or 1e-9
        return cls(
            metric=metric,
            median=med,
            mad=mad,
            mean=_mean(values),
            std=_std(values),
            p5=_percentile(values, 5),
            p95=_percentile(values, 95),
            cv=_cv(values),
            n=len(values),
            fence_hard=med + 3.5 * mad,
            fence_soft=med + 2.0 * mad,
        )

    def check(self, value: float) -> str:
        """Returns 'normal', 'elevated', or 'violation'."""
        if self.n < 5:
            return "normal"  # insufficient data for judgment
        if value > self.fence_hard:
            return "violation"
        if value > self.fence_soft:
            return "elevated"
        return "normal"


@dataclass
class AgentFingerprint:
    """The complete behavioral profile of one agent.
    Built incrementally from every run. This is the accumulated intelligence
    that makes Cortex irreplaceable — remove it and you lose months of
    learned behavioral baselines.
    """
    agent_id: str
    envelopes: Dict[str, BehavioralEnvelope] = field(default_factory=dict)
    # Running history (bounded circular buffers for memory efficiency)
    token_history: List[float] = field(default_factory=list)
    latency_history: List[float] = field(default_factory=list)
    cost_history: List[float] = field(default_factory=list)
    success_history: List[int] = field(default_factory=list)  # 1/0
    # Streaming aggregates (don't need full history for these)
    total_runs: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_successes: int = 0
    ewma_latency: float = 0.0
    ewma_tokens: float = 0.0
    ewma_cost: float = 0.0
    last_updated: Optional[str] = None
    # Drift state
    drift_score: float = 0.0    # 0=stable, 1=fully drifted
    drift_direction: str = "stable"
    consecutive_violations: int = 0

    HISTORY_CAP = 500  # keep last N runs per dimension

    def ingest(self, tokens: int, latency_ms: float, cost: float, success: bool):
        """Record one run's telemetry into the fingerprint."""
        self.total_runs += 1
        self.total_tokens += tokens
        self.total_cost += cost
        if success:
            self.total_successes += 1

        # Append to bounded history
        for hist, val in [
            (self.token_history, float(tokens)),
            (self.latency_history, latency_ms),
            (self.cost_history, cost),
        ]:
            hist.append(val)
            if len(hist) > self.HISTORY_CAP:
                hist.pop(0)
        self.success_history.append(1 if success else 0)
        if len(self.success_history) > self.HISTORY_CAP:
            self.success_history.pop(0)

        # Update EWMA
        self.ewma_latency = _ewma(self.ewma_latency, latency_ms) if self.total_runs > 1 else latency_ms
        self.ewma_tokens = _ewma(self.ewma_tokens, float(tokens)) if self.total_runs > 1 else float(tokens)
        self.ewma_cost = _ewma(self.ewma_cost, cost) if self.total_runs > 1 else cost
        self.last_updated = datetime.now(timezone.utc).isoformat()

        # Rebuild envelopes periodically (every 10 runs, or first 50)
        if self.total_runs <= 50 or self.total_runs % 10 == 0:
            self._rebuild_envelopes()

        # Check drift on this new data point
        self._check_drift(tokens, latency_ms, cost, success)

    def _rebuild_envelopes(self):
        """Recompute invariance envelopes from accumulated history."""
        if len(self.token_history) >= 5:
            self.envelopes["tokens"] = BehavioralEnvelope.from_values("tokens", self.token_history)
        if len(self.latency_history) >= 5:
            self.envelopes["latency"] = BehavioralEnvelope.from_values("latency", self.latency_history)
        if len(self.cost_history) >= 5:
            self.envelopes["cost"] = BehavioralEnvelope.from_values("cost", self.cost_history)

    def _check_drift(self, tokens: int, latency_ms: float, cost: float, success: bool):
        """Drift detection inspired by Sendero's two-level test.

        Level 1 (dispersion): Are recent values spreading wider than the
        established envelope? High CV in a recent window = instability.

        Level 2 (direction): Is the EWMA trending away from the median?
        A sustained directional shift (not just noise) = real drift.
        """
        signals = []

        # Check each dimension against its envelope
        for metric, value in [("tokens", float(tokens)), ("latency", latency_ms), ("cost", cost)]:
            env = self.envelopes.get(metric)
            if env and env.n >= 10:
                status = env.check(value)
                if status == "violation":
                    signals.append(1.0)
                elif status == "elevated":
                    signals.append(0.5)
                else:
                    signals.append(0.0)

        # Recent success rate drift (last 20 vs overall)
        if len(self.success_history) >= 30:
            recent_rate = _mean([float(x) for x in self.success_history[-20:]])
            overall_rate = self.total_successes / self.total_runs
            if overall_rate > 0:
                rate_delta = abs(recent_rate - overall_rate) / overall_rate
                signals.append(min(1.0, rate_delta / 0.25))  # 25% relative change = full signal

        # Recent window CV check (Sendero Level 1 analog)
        if len(self.latency_history) >= 20:
            recent_cv = _cv(self.latency_history[-20:])
            baseline_cv = self.envelopes.get("latency", BehavioralEnvelope(metric="latency")).cv
            if baseline_cv > 0:
                cv_ratio = recent_cv / baseline_cv
                if cv_ratio > 2.0:
                    signals.append(min(1.0, (cv_ratio - 1.0) / 2.0))

        if signals:
            raw_drift = _mean(signals)
            # EWMA the drift score so transient spikes don't cause false alarms
            self.drift_score = _ewma(self.drift_score, raw_drift, alpha=0.2)
        else:
            self.drift_score = _ewma(self.drift_score, 0.0, alpha=0.05)

        # Classify drift direction
        if self.drift_score < 0.15:
            self.drift_direction = "stable"
            self.consecutive_violations = 0
        elif self.drift_score < 0.40:
            self.drift_direction = "elevated"
            self.consecutive_violations = 0
        else:
            self.drift_direction = "drifting"
            self.consecutive_violations += 1

    def snapshot(self) -> Dict[str, Any]:
        """Serializable fingerprint summary for API responses."""
        return {
            "agent_id": self.agent_id,
            "total_runs": self.total_runs,
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 4),
            "success_rate": round(self.total_successes / self.total_runs, 4) if self.total_runs else 0,
            "ewma": {
                "latency_ms": round(self.ewma_latency, 1),
                "tokens": round(self.ewma_tokens, 1),
                "cost": round(self.ewma_cost, 6),
            },
            "drift": {
                "score": round(self.drift_score, 3),
                "direction": self.drift_direction,
                "consecutive_violations": self.consecutive_violations,
            },
            "envelopes": {
                k: {
                    "median": round(e.median, 2),
                    "mad": round(e.mad, 2),
                    "p5": round(e.p5, 2),
                    "p95": round(e.p95, 2),
                    "cv": round(e.cv, 3),
                    "fence_soft": round(e.fence_soft, 2),
                    "fence_hard": round(e.fence_hard, 2),
                    "n": e.n,
                }
                for k, e in self.envelopes.items()
            },
            "last_updated": self.last_updated,
        }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — ADAPTIVE ROUTING
# From NWC Quant's portfolio optimization (risk-adjusted selection) + Sendero's
# eta-squared decomposition (which factor explains the outcome).
#
# The router learns which provider/model combination works best for each
# task type. It uses Thompson sampling (a multi-armed bandit algorithm) to
# balance exploration vs exploitation — exactly the explore/exploit tradeoff
# a quant faces when allocating capital across strategies.
#
# Selection-bias correction: after N routing trials, the "best" arm looks
# better than it really is (the same trap NWC Quant's deflated Sharpe
# catches in backtesting). The router deflates its confidence in the
# top-performing arm proportionally to how many arms were tried.
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ArmStats:
    """One arm of the multi-armed bandit = one provider/model combination."""
    provider: str
    model: str
    # Beta distribution parameters for Thompson sampling
    alpha: float = 1.0   # successes + prior
    beta: float = 1.0    # failures + prior
    # Performance tracking
    total_pulls: int = 0
    total_successes: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    latency_sum: float = 0.0
    # Streaming averages
    avg_latency: float = 0.0
    avg_cost: float = 0.0
    avg_tokens: float = 0.0
    last_pulled: Optional[str] = None

    @property
    def success_rate(self) -> float:
        return self.total_successes / self.total_pulls if self.total_pulls else 0.5

    @property
    def estimated_quality(self) -> float:
        """Point estimate: alpha / (alpha + beta)."""
        return self.alpha / (self.alpha + self.beta)

    def sample(self, rng: random.Random) -> float:
        """Thompson sample from the posterior Beta distribution."""
        return rng.betavariate(self.alpha, self.beta)

    def update(self, success: bool, tokens: int, cost: float, latency_ms: float):
        self.total_pulls += 1
        if success:
            self.alpha += 1.0
            self.total_successes += 1
        else:
            self.beta += 1.0
        self.total_tokens += tokens
        self.total_cost += cost
        self.latency_sum += latency_ms
        self.avg_latency = self.latency_sum / self.total_pulls
        self.avg_cost = self.total_cost / self.total_pulls
        self.avg_tokens = self.total_tokens / self.total_pulls
        self.last_pulled = datetime.now(timezone.utc).isoformat()


@dataclass
class TaskTypeRouter:
    """Router for one task type — each task type has its own bandit."""
    task_type: str
    arms: Dict[str, ArmStats] = field(default_factory=dict)
    total_routes: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random(42))

    def arm_key(self, provider: str, model: str) -> str:
        return f"{provider}:{model}"

    def ensure_arm(self, provider: str, model: str) -> ArmStats:
        key = self.arm_key(provider, model)
        if key not in self.arms:
            self.arms[key] = ArmStats(provider=provider, model=model)
        return self.arms[key]

    def select(self, available_providers: List[Dict[str, str]],
               optimize_for: str = "quality") -> Dict[str, Any]:
        """Pick the best arm via Thompson sampling.

        optimize_for:
            "quality"  — maximize success probability
            "cost"     — minimize cost per token (still weighted by quality)
            "latency"  — minimize latency (still weighted by quality)
            "balanced" — Pareto-optimal across all three
        """
        if not available_providers:
            return {"error": "no providers available"}

        candidates = []
        for p in available_providers:
            arm = self.ensure_arm(p["provider"], p["model"])
            # Thompson sample for quality
            q_sample = arm.sample(self.rng)
            candidates.append((arm, q_sample))

        # Score each candidate based on optimization target
        scored = []
        for arm, q_sample in candidates:
            if optimize_for == "quality":
                score = q_sample
            elif optimize_for == "cost":
                cost_factor = 1.0 / (arm.avg_cost + 1e-9) if arm.total_pulls > 0 else 1.0
                score = q_sample * 0.4 + min(1.0, cost_factor / 100) * 0.6
            elif optimize_for == "latency":
                lat_factor = 1.0 / (arm.avg_latency + 1e-9) if arm.total_pulls > 0 else 1.0
                score = q_sample * 0.4 + min(1.0, lat_factor * 100) * 0.6
            else:  # balanced
                cost_norm = 1.0 / (1.0 + (arm.avg_cost * 1000)) if arm.total_pulls else 0.5
                lat_norm = 1.0 / (1.0 + (arm.avg_latency / 1000)) if arm.total_pulls else 0.5
                score = 0.50 * q_sample + 0.25 * cost_norm + 0.25 * lat_norm
            scored.append((arm, score, q_sample))

        scored.sort(key=lambda x: x[1], reverse=True)
        winner = scored[0][0]
        self.total_routes += 1

        # Selection-bias deflation (from NWC Quant / Sendero validation)
        # If we've tried many arms, the "best" looks better than it is.
        n_arms_tried = sum(1 for a in self.arms.values() if a.total_pulls > 0)
        deflation = self._deflation_factor(winner, n_arms_tried)

        return {
            "provider": winner.provider,
            "model": winner.model,
            "estimated_quality": round(winner.estimated_quality, 4),
            "thompson_sample": round(scored[0][2], 4),
            "composite_score": round(scored[0][1], 4),
            "selection_deflation": round(deflation, 4),
            "deflated_confidence": round(winner.estimated_quality * (1 - deflation), 4),
            "arm_pulls": winner.total_pulls,
            "n_arms_explored": n_arms_tried,
            "optimize_for": optimize_for,
        }

    def _deflation_factor(self, best_arm: ArmStats, n_tried: int) -> float:
        """How much to distrust the top arm's apparent lead.

        Adapted from NWC Quant's deflated Sharpe: the more arms you tried,
        the more likely the winner is just the luckiest of N noisy draws.
        Uses the analytic approximation rather than full permutation (Sendero
        validation.py), because the bandit updates continuously rather than
        running a one-shot classification.
        """
        if n_tried <= 1 or best_arm.total_pulls < 5:
            return 0.0
        # Expected max of N Beta(alpha, beta) draws under the null (all arms equal)
        # Approximation: E[max] ≈ mean + std * sqrt(2 * ln(N))
        null_mean = 0.5
        # Variance of Beta(a,b) estimate with this many pulls
        a, b = best_arm.alpha, best_arm.beta
        var_estimate = (a * b) / ((a + b) ** 2 * (a + b + 1))
        std_estimate = math.sqrt(var_estimate) if var_estimate > 0 else 0.01
        expected_max = null_mean + std_estimate * math.sqrt(2 * math.log(max(n_tried, 2)))
        # How much of the observed quality is explained by selection luck
        observed = best_arm.estimated_quality
        if observed <= null_mean:
            return 0.0
        luck_share = min(1.0, (expected_max - null_mean) / (observed - null_mean + 1e-9))
        return max(0.0, min(0.6, luck_share * 0.5))  # cap at 60% deflation

    def record_outcome(self, provider: str, model: str,
                       success: bool, tokens: int, cost: float, latency_ms: float):
        arm = self.ensure_arm(provider, model)
        arm.update(success, tokens, cost, latency_ms)

    def leaderboard(self) -> List[Dict[str, Any]]:
        """Rank all explored arms. Same pattern as NWC Quant's summary()."""
        rows = []
        for key, arm in self.arms.items():
            if arm.total_pulls == 0:
                continue
            rows.append({
                "provider": arm.provider,
                "model": arm.model,
                "pulls": arm.total_pulls,
                "success_rate": round(arm.success_rate, 4),
                "estimated_quality": round(arm.estimated_quality, 4),
                "avg_latency_ms": round(arm.avg_latency, 1),
                "avg_cost": round(arm.avg_cost, 6),
                "avg_tokens": round(arm.avg_tokens, 1),
                "last_pulled": arm.last_pulled,
            })
        rows.sort(key=lambda x: x["estimated_quality"], reverse=True)
        return rows


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — RUN PREDICTION
# From Arbiter's deterministic multi-lever scoring. Before a run executes,
# CAR estimates success probability, expected token usage, and latency by
# scoring the request against three levers — the same three-lever composite
# architecture that drives Arbiter's resolution-integrity engine.
#
# The three levers for run prediction:
#   - prompt_complexity : how hard is this request? (token count, instruction
#     density, tool requirements)
#   - historical_fit    : how well does this agent/model combination perform
#     on tasks like this? (from the fingerprint + router data)
#   - provider_health   : is the target provider healthy right now? (recent
#     error rates, latency trends)
# ═══════════════════════════════════════════════════════════════════════════

# Default prediction policy — governed and versioned, just like Arbiter's policy.py
DEFAULT_PREDICTION_POLICY = {
    "version": "1.0.0",
    "weights": {
        "prompt_complexity": 0.30,
        "historical_fit": 0.45,
        "provider_health": 0.25,
    },
    "thresholds": {
        "high_confidence": 75,    # composite >= this: go ahead
        "medium_confidence": 50,  # composite >= this: proceed with monitoring
        # below medium: warn the caller
    },
    "changelog": [],
}


def _score_prompt_complexity(prompt_tokens: int, tool_count: int,
                             system_tokens: int = 0) -> Tuple[int, List[str]]:
    """Score how complex the prompt is. Higher = LESS risk (simpler prompts are safer).
    Scale 0-100 where 100 = trivially simple, 0 = extremely complex.
    """
    flags = []
    score = 80  # start optimistic

    # Token volume (more tokens = more room for confusion)
    if prompt_tokens > 8000:
        score -= 35
        flags.append("very long prompt (>8k tokens)")
    elif prompt_tokens > 3000:
        score -= 18
        flags.append("long prompt (>3k tokens)")
    elif prompt_tokens > 1000:
        score -= 8

    # Tool usage (more tools = more failure modes)
    if tool_count > 5:
        score -= 25
        flags.append(f"{tool_count} tools — high orchestration complexity")
    elif tool_count > 2:
        score -= 12
        flags.append(f"{tool_count} tools")
    elif tool_count > 0:
        score -= 5

    # System prompt overhead
    if system_tokens > 4000:
        score -= 10
        flags.append("heavy system prompt")

    return max(0, min(100, score)), flags


def _score_historical_fit(fingerprint: Optional[AgentFingerprint],
                          arm: Optional[ArmStats]) -> Tuple[int, List[str]]:
    """Score based on this agent's track record and the router arm's performance.
    Higher = better historical fit.
    """
    flags = []
    score = 50  # neutral when we have no data

    if fingerprint and fingerprint.total_runs >= 5:
        sr = fingerprint.total_successes / fingerprint.total_runs
        score = int(sr * 100)
        if sr < 0.5:
            flags.append(f"low historical success rate ({sr:.0%})")
        if fingerprint.drift_direction == "drifting":
            score -= 20
            flags.append("agent is currently drifting")
        elif fingerprint.drift_direction == "elevated":
            score -= 8
            flags.append("agent behavior slightly elevated")

    if arm and arm.total_pulls >= 5:
        arm_sr = arm.success_rate
        arm_contribution = int(arm_sr * 100)
        # Blend agent-level and arm-level signals
        if fingerprint and fingerprint.total_runs >= 5:
            score = int(0.6 * score + 0.4 * arm_contribution)
        else:
            score = arm_contribution
        if arm_sr < 0.5:
            flags.append(f"this model has low success rate ({arm_sr:.0%}) for this task type")

    if (not fingerprint or fingerprint.total_runs < 5) and (not arm or arm.total_pulls < 5):
        flags.append("insufficient history — prediction is low-confidence")

    return max(0, min(100, score)), flags


def _score_provider_health(provider: str, recent_runs: List[Dict],
                           window_size: int = 20) -> Tuple[int, List[str]]:
    """Score the provider's recent health. Higher = healthier.
    Uses a sliding window of recent runs, same concept as NWC Quant's
    walk-forward folds — recent performance matters more than lifetime average.
    """
    flags = []

    if not recent_runs:
        return 60, ["no recent provider data"]

    window = recent_runs[-window_size:]
    successes = sum(1 for r in window if r.get("success"))
    errors = len(window) - successes
    error_rate = errors / len(window)

    # Recent latency trend
    latencies = [r.get("latency_ms", 0) for r in window if r.get("latency_ms")]
    if latencies:
        recent_lat = _mean(latencies[-5:]) if len(latencies) >= 5 else _mean(latencies)
        overall_lat = _mean(latencies)
        if overall_lat > 0 and recent_lat > overall_lat * 1.5:
            flags.append("latency trending up")

    score = int((1 - error_rate) * 100)

    if error_rate > 0.3:
        score -= 15
        flags.append(f"high recent error rate ({error_rate:.0%})")
    if error_rate > 0.5:
        score -= 20
        flags.append("provider may be degraded")

    return max(0, min(100, score)), flags


@dataclass
class RunPrediction:
    """Pre-execution prediction for a single run.
    Structured like Arbiter's analyze() output — levers, composite, verdict.
    """
    agent_id: str
    provider: str
    model: str
    levers: Dict[str, Any] = field(default_factory=dict)
    composite: int = 0
    confidence_tier: str = "unknown"
    predicted_tokens: int = 0
    predicted_latency_ms: float = 0.0
    predicted_cost: float = 0.0
    flags: List[str] = field(default_factory=list)
    policy_version: str = ""
    trail: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "levers": self.levers,
            "composite_confidence": self.composite,
            "confidence_tier": self.confidence_tier,
            "predictions": {
                "tokens": self.predicted_tokens,
                "latency_ms": round(self.predicted_latency_ms, 1),
                "cost": round(self.predicted_cost, 6),
            },
            "flags": self.flags,
            "policy_version": self.policy_version,
            "audit_trail": self.trail,
        }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — GOVERNANCE & AUDIT
# From Arbiter's policy.py and resolve() audit trail.
# Every CAR decision is logged with a SHA-256 signature. Policy changes are
# versioned and immutable. Platform-wide monitoring follows Arbiter's
# monitoring.py pattern: surface where failures cluster, which agents drift,
# what to fix first.
# ═══════════════════════════════════════════════════════════════════════════

def _audit_step(entity_id: str, action: str, detail: str) -> Dict[str, str]:
    """Create one auditable step, signed like Arbiter's resolution trail."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    payload = f"{entity_id}|{action}|{detail}|{ts}"
    sig = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return {"action": action, "detail": detail, "ts": ts, "sig": f"0x{sig}"}


def platform_health(fingerprints: Dict[str, AgentFingerprint]) -> Dict[str, Any]:
    """Platform-wide health summary.
    Same pattern as Arbiter's monitoring.summarize() — aggregate view of where
    risk concentrates across the fleet.
    """
    total_agents = len(fingerprints)
    total_runs = sum(fp.total_runs for fp in fingerprints.values())
    total_cost = sum(fp.total_cost for fp in fingerprints.values())
    drifting = [aid for aid, fp in fingerprints.items() if fp.drift_direction == "drifting"]
    elevated = [aid for aid, fp in fingerprints.items() if fp.drift_direction == "elevated"]

    # Success rate distribution across agents (Sendero-style dispersion check)
    rates = []
    for fp in fingerprints.values():
        if fp.total_runs >= 5:
            rates.append(fp.total_successes / fp.total_runs)
    fleet_cv = _cv(rates) if len(rates) >= 3 else 0.0
    tail = _tail_share([1.0 - r for r in rates]) if len(rates) >= 5 else 0.0  # tail of failure rates

    return {
        "total_agents": total_agents,
        "total_runs": total_runs,
        "total_cost": round(total_cost, 4),
        "fleet_success_rate": round(_mean(rates), 4) if rates else None,
        "fleet_cv": round(fleet_cv, 3),
        "failure_tail_share": round(tail, 3),
        "drifting_agents": drifting,
        "elevated_agents": elevated,
        "health": (
            "degraded" if len(drifting) > total_agents * 0.2 else
            "elevated" if drifting or fleet_cv > 0.4 else
            "healthy"
        ),
    }


def lever_pressure(fingerprints: Dict[str, AgentFingerprint]) -> Dict[str, Any]:
    """Which dimension drives the most drift fleet-wide?
    Direct analog of Arbiter's monitoring.lever_pressure().
    """
    dimension_scores = {"tokens": [], "latency": [], "cost": []}
    for fp in fingerprints.values():
        for dim in dimension_scores:
            env = fp.envelopes.get(dim)
            if env and env.n >= 10:
                # Normalized distance from median (how "stretched" is this agent)
                dimension_scores[dim].append(env.cv)
    averages = {k: round(_mean(v), 3) if v else 0.0 for k, v in dimension_scores.items()}
    worst = max(averages, key=averages.get) if averages else "unknown"
    return {"averages": averages, "primary_pressure": worst}


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — THE CAR ORCHESTRATOR
# The top-level class that ties all four systems together.
# ═══════════════════════════════════════════════════════════════════════════

class CAR:
    """CORTEX Adaptive Runtime — the main entry point.

    Usage:
        engine = CAR()
        engine.record_run(agent_id, run_data)
        fp = engine.fingerprint(agent_id)
        route = engine.route(agent_id, task_type, available_providers)
        pred = engine.predict(agent_id, provider, model, prompt_tokens, tool_count)
    """

    def __init__(self):
        self.fingerprints: Dict[str, AgentFingerprint] = {}
        self.routers: Dict[str, TaskTypeRouter] = {}
        self.provider_runs: Dict[str, List[Dict]] = defaultdict(list)
        self.prediction_policy: Dict[str, Any] = dict(DEFAULT_PREDICTION_POLICY)
        self.audit_log: List[Dict[str, str]] = []
        self._run_counter = 0

    # ── Fingerprinting ────────────────────────────────────────────────

    def record_run(self, agent_id: str, run_data: Dict[str, Any]):
        """Ingest one completed run into all CAR subsystems.

        run_data should contain:
            provider, model, task_type, tokens, latency_ms, cost, success
        """
        if agent_id not in self.fingerprints:
            self.fingerprints[agent_id] = AgentFingerprint(agent_id=agent_id)

        fp = self.fingerprints[agent_id]
        tokens = run_data.get("tokens", 0)
        latency = run_data.get("latency_ms", 0.0)
        cost = run_data.get("cost", 0.0)
        success = run_data.get("success", True)
        provider = run_data.get("provider", "unknown")
        model = run_data.get("model", "unknown")
        task_type = run_data.get("task_type", "general")

        # 1. Update fingerprint
        fp.ingest(tokens, latency, cost, success)

        # 2. Update router
        if task_type not in self.routers:
            self.routers[task_type] = TaskTypeRouter(task_type=task_type)
        self.routers[task_type].record_outcome(provider, model, success, tokens, cost, latency)

        # 3. Track provider health
        self.provider_runs[provider].append({
            "success": success, "tokens": tokens, "latency_ms": latency,
            "cost": cost, "ts": time.time(),
        })
        # Keep provider history bounded
        if len(self.provider_runs[provider]) > 1000:
            self.provider_runs[provider] = self.provider_runs[provider][-500:]

        # 4. Audit trail
        self._run_counter += 1
        step = _audit_step(
            agent_id,
            "run_recorded",
            f"run #{self._run_counter} | {provider}:{model} | "
            f"{'ok' if success else 'fail'} | {tokens} tok | {latency:.0f}ms | ${cost:.4f}"
        )
        self.audit_log.append(step)
        if len(self.audit_log) > 5000:
            self.audit_log = self.audit_log[-2500:]

    def fingerprint(self, agent_id: str) -> Dict[str, Any]:
        """Get the behavioral fingerprint for an agent."""
        fp = self.fingerprints.get(agent_id)
        if not fp:
            return {"error": f"no fingerprint for agent {agent_id}", "agent_id": agent_id}
        return fp.snapshot()

    # ── Routing ───────────────────────────────────────────────────────

    def route(self, agent_id: str, task_type: str,
              available_providers: List[Dict[str, str]],
              optimize_for: str = "balanced") -> Dict[str, Any]:
        """Select the optimal provider/model for this task.

        available_providers: [{"provider": "openai", "model": "gpt-4o"}, ...]
        optimize_for: "quality" | "cost" | "latency" | "balanced"
        """
        if task_type not in self.routers:
            self.routers[task_type] = TaskTypeRouter(task_type=task_type)

        router = self.routers[task_type]
        result = router.select(available_providers, optimize_for)

        # Audit the routing decision
        if "error" not in result:
            step = _audit_step(
                agent_id,
                "route_selected",
                f"task_type={task_type} | routed to {result['provider']}:{result['model']} | "
                f"score={result['composite_score']} | deflation={result['selection_deflation']}"
            )
            self.audit_log.append(step)

        return result

    def routing_leaderboard(self, task_type: str) -> List[Dict[str, Any]]:
        """Get the ranked provider/model leaderboard for a task type."""
        router = self.routers.get(task_type)
        if not router:
            return []
        return router.leaderboard()

    # ── Prediction ────────────────────────────────────────────────────

    def predict(self, agent_id: str, provider: str, model: str,
                prompt_tokens: int = 500, tool_count: int = 0,
                system_tokens: int = 0, task_type: str = "general") -> Dict[str, Any]:
        """Pre-execution prediction: will this run succeed? What will it cost?

        Returns a RunPrediction with Arbiter-style lever scoring.
        """
        trail = []

        # Lever 1: Prompt complexity
        s_complexity, f_complexity = _score_prompt_complexity(prompt_tokens, tool_count, system_tokens)
        trail.append(_audit_step(agent_id, "lever:prompt_complexity",
                                 f"score={s_complexity} | {'; '.join(f_complexity) or 'nominal'}"))

        # Lever 2: Historical fit
        fp = self.fingerprints.get(agent_id)
        router = self.routers.get(task_type)
        arm = router.arms.get(f"{provider}:{model}") if router else None
        s_historical, f_historical = _score_historical_fit(fp, arm)
        trail.append(_audit_step(agent_id, "lever:historical_fit",
                                 f"score={s_historical} | {'; '.join(f_historical) or 'solid track record'}"))

        # Lever 3: Provider health
        recent = self.provider_runs.get(provider, [])
        s_health, f_health = _score_provider_health(provider, recent)
        trail.append(_audit_step(agent_id, "lever:provider_health",
                                 f"score={s_health} | {'; '.join(f_health) or 'healthy'}"))

        # Composite score (weighted sum, Arbiter-style)
        w = self.prediction_policy["weights"]
        composite = round(
            w["prompt_complexity"] * s_complexity +
            w["historical_fit"] * s_historical +
            w["provider_health"] * s_health
        )

        t = self.prediction_policy["thresholds"]
        if composite >= t["high_confidence"]:
            tier = "high"
        elif composite >= t["medium_confidence"]:
            tier = "medium"
        else:
            tier = "low"

        # Predict resource usage from fingerprint/arm history
        pred_tokens = int(fp.ewma_tokens) if fp and fp.total_runs >= 3 else prompt_tokens * 2
        pred_latency = fp.ewma_latency if fp and fp.total_runs >= 3 else 2000.0
        pred_cost = fp.ewma_cost if fp and fp.total_runs >= 3 else 0.0

        if arm and arm.total_pulls >= 3:
            # Blend agent-level and arm-level predictions
            pred_tokens = int(0.5 * pred_tokens + 0.5 * arm.avg_tokens)
            pred_latency = 0.5 * pred_latency + 0.5 * arm.avg_latency
            pred_cost = 0.5 * pred_cost + 0.5 * arm.avg_cost

        all_flags = f_complexity + f_historical + f_health
        trail.append(_audit_step(agent_id, "prediction_complete",
                                 f"composite={composite} | tier={tier} | "
                                 f"est_tokens={pred_tokens} | est_latency={pred_latency:.0f}ms"))

        prediction = RunPrediction(
            agent_id=agent_id,
            provider=provider,
            model=model,
            levers={
                "prompt_complexity": {"score": s_complexity, "flags": f_complexity},
                "historical_fit": {"score": s_historical, "flags": f_historical},
                "provider_health": {"score": s_health, "flags": f_health},
            },
            composite=composite,
            confidence_tier=tier,
            predicted_tokens=pred_tokens,
            predicted_latency_ms=pred_latency,
            predicted_cost=pred_cost,
            flags=all_flags,
            policy_version=self.prediction_policy["version"],
            trail=trail,
        )

        return prediction.to_dict()

    # ── Platform monitoring ───────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        """Platform-wide health report."""
        return platform_health(self.fingerprints)

    def pressure(self) -> Dict[str, Any]:
        """Which metric dimension is driving the most drift fleet-wide?"""
        return lever_pressure(self.fingerprints)

    # ── Governance ────────────────────────────────────────────────────

    def update_prediction_policy(self, weights: Optional[Dict] = None,
                                 thresholds: Optional[Dict] = None,
                                 by: str = "admin") -> Dict[str, Any]:
        """Update prediction policy with versioned changelog.
        Same governed-configurability pattern as Arbiter's policy.py.
        """
        if weights:
            self.prediction_policy["weights"].update(weights)
        if thresholds:
            self.prediction_policy["thresholds"].update(thresholds)

        # Bump version
        parts = self.prediction_policy["version"].split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        self.prediction_policy["version"] = ".".join(parts)

        entry = {
            "version": self.prediction_policy["version"],
            "at": datetime.now(timezone.utc).isoformat(),
            "by": by,
            "weights": dict(self.prediction_policy["weights"]),
            "thresholds": dict(self.prediction_policy["thresholds"]),
        }
        self.prediction_policy["changelog"].append(entry)

        step = _audit_step("policy", "policy_updated",
                           f"v{entry['version']} by {by} | w={entry['weights']}")
        self.audit_log.append(step)

        return self.prediction_policy

    def get_audit_log(self, limit: int = 50, agent_id: Optional[str] = None) -> List[Dict]:
        """Retrieve recent audit entries, optionally filtered by agent."""
        log = self.audit_log
        if agent_id:
            log = [e for e in log if agent_id in e.get("detail", "")]
        return log[-limit:]

    # ── Serialization ─────────────────────────────────────────────────

    def state_snapshot(self) -> Dict[str, Any]:
        """Full engine state for persistence. Called by cortex.py on shutdown
        and periodically to save CAR state to the database.
        """
        return {
            "fingerprints": {aid: fp.snapshot() for aid, fp in self.fingerprints.items()},
            "routers": {
                tt: {
                    "task_type": r.task_type,
                    "total_routes": r.total_routes,
                    "arms": {
                        k: {
                            "provider": a.provider, "model": a.model,
                            "alpha": a.alpha, "beta": a.beta,
                            "total_pulls": a.total_pulls,
                            "total_successes": a.total_successes,
                            "avg_latency": round(a.avg_latency, 1),
                            "avg_cost": round(a.avg_cost, 6),
                            "avg_tokens": round(a.avg_tokens, 1),
                        }
                        for k, a in r.arms.items()
                    },
                }
                for tt, r in self.routers.items()
            },
            "prediction_policy": self.prediction_policy,
            "run_counter": self._run_counter,
            "audit_log_size": len(self.audit_log),
        }

    def load_arm_state(self, task_type: str, arm_data: Dict[str, Any]):
        """Restore a router arm from persisted state."""
        if task_type not in self.routers:
            self.routers[task_type] = TaskTypeRouter(task_type=task_type)
        router = self.routers[task_type]
        key = f"{arm_data['provider']}:{arm_data['model']}"
        arm = ArmStats(
            provider=arm_data["provider"],
            model=arm_data["model"],
            alpha=arm_data.get("alpha", 1.0),
            beta=arm_data.get("beta", 1.0),
            total_pulls=arm_data.get("total_pulls", 0),
            total_successes=arm_data.get("total_successes", 0),
            avg_latency=arm_data.get("avg_latency", 0.0),
            avg_cost=arm_data.get("avg_cost", 0.0),
            avg_tokens=arm_data.get("avg_tokens", 0.0),
        )
        if arm.total_pulls > 0:
            arm.latency_sum = arm.avg_latency * arm.total_pulls
            arm.total_cost = arm.avg_cost * arm.total_pulls
            arm.total_tokens = int(arm.avg_tokens * arm.total_pulls)
        router.arms[key] = arm
