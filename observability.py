"""
CORTEX Observability Engine
════════════════════════════
Enterprise-grade observability: metrics, traces, logs, alerts, SLOs.

Inspired by Datadog / Splunk / Honeycomb — purpose-built for AI agent ops.

Components:
  - MetricsEngine:  time-series counters, gauges, histograms with rollup windows
  - TraceCollector: distributed tracing with spans, parent-child, waterfall
  - LogAggregator:  structured log ingestion, search, pattern detection
  - AlertEngine:    threshold + anomaly rules, escalation, silencing
  - SLOTracker:     service-level objectives with burn-rate alerts
  - HealthScorer:   composite agent health score (0-100)

Usage:
    from observability import obs
    obs.metric("agent.run.latency", 3.42, tags={"agent": "pr-reviewer"})
    span = obs.trace_start("run-123", "llm_call", tags={"model": "claude-4"})
    obs.trace_end(span, status="ok")
    obs.log("info", "Run completed", {"agent_id": "a1", "tokens": 1540})
    obs.alert_check()  # evaluates all rules
"""

import json
import time
import math
import threading
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════
#  METRICS ENGINE — time-series counters, gauges, histograms
# ═══════════════════════════════════════════════════════════════

@dataclass
class MetricPoint:
    timestamp: float
    value: float
    tags: Dict[str, str]


class Histogram:
    """DDSketch-inspired histogram for percentile estimation."""

    def __init__(self, relative_accuracy: float = 0.01):
        self._gamma = (1 + relative_accuracy) / (1 - relative_accuracy)
        self._buckets: Dict[int, int] = defaultdict(int)
        self._count = 0
        self._sum = 0.0
        self._min = float("inf")
        self._max = float("-inf")
        self._zero_count = 0

    def add(self, value: float):
        self._count += 1
        self._sum += value
        self._min = min(self._min, value)
        self._max = max(self._max, value)
        if value == 0:
            self._zero_count += 1
        elif value > 0:
            idx = int(math.ceil(math.log(value) / math.log(self._gamma)))
            self._buckets[idx] += 1

    def quantile(self, q: float) -> float:
        if self._count == 0:
            return 0.0
        rank = int(q * self._count)
        running = self._zero_count
        if running >= rank:
            return 0.0
        for idx in sorted(self._buckets.keys()):
            running += self._buckets[idx]
            if running >= rank:
                return 2.0 * self._gamma ** idx / (self._gamma + 1)
        return self._max

    def stats(self) -> dict:
        if self._count == 0:
            return {"count": 0, "sum": 0, "min": 0, "max": 0, "avg": 0,
                    "p50": 0, "p75": 0, "p90": 0, "p95": 0, "p99": 0}
        return {
            "count": self._count, "sum": round(self._sum, 4),
            "min": round(self._min, 4), "max": round(self._max, 4),
            "avg": round(self._sum / self._count, 4),
            "p50": round(self.quantile(0.50), 4),
            "p75": round(self.quantile(0.75), 4),
            "p90": round(self.quantile(0.90), 4),
            "p95": round(self.quantile(0.95), 4),
            "p99": round(self.quantile(0.99), 4),
        }

    def merge(self, other: "Histogram"):
        for idx, count in other._buckets.items():
            self._buckets[idx] += count
        self._count += other._count
        self._sum += other._sum
        self._min = min(self._min, other._min)
        self._max = max(self._max, other._max)
        self._zero_count += other._zero_count


class RollupBucket:
    """Aggregated metrics over a time window."""

    def __init__(self, window_start: float, window_seconds: int):
        self.window_start = window_start
        self.window_seconds = window_seconds
        self.counters: Dict[str, float] = defaultdict(float)
        self.gauges: Dict[str, Tuple[float, float]] = {}  # name -> (last_value, timestamp)
        self.histograms: Dict[str, Histogram] = defaultdict(Histogram)

    def to_dict(self) -> dict:
        return {
            "window_start": datetime.fromtimestamp(self.window_start, tz=timezone.utc).isoformat(),
            "window_seconds": self.window_seconds,
            "counters": dict(self.counters),
            "gauges": {k: v[0] for k, v in self.gauges.items()},
            "histograms": {k: h.stats() for k, h in self.histograms.items()},
        }


class MetricsEngine:
    """Time-series metrics with automatic rollup into 1m, 5m, 1h, 1d windows."""

    WINDOWS = [60, 300, 3600, 86400]  # 1m, 5m, 1h, 1d

    def __init__(self, max_raw_points: int = 50000):
        self._raw: deque = deque(maxlen=max_raw_points)
        self._rollups: Dict[int, List[RollupBucket]] = {w: [] for w in self.WINDOWS}
        self._current_gauges: Dict[str, Tuple[float, float, Dict]] = {}
        self._lock = threading.Lock()
        self._tag_index: Dict[str, set] = defaultdict(set)  # tag_key:tag_val -> set of metric names
        self._max_rollups = {60: 1440, 300: 2016, 3600: 720, 86400: 365}

    def _tag_key(self, tags: dict) -> str:
        return "|".join(f"{k}={v}" for k, v in sorted(tags.items())) if tags else ""

    def counter(self, name: str, value: float = 1.0, tags: dict = None):
        """Increment a counter metric."""
        tags = tags or {}
        ts = time.time()
        with self._lock:
            self._raw.append(MetricPoint(ts, value, tags))
            full_name = f"counter:{name}:{self._tag_key(tags)}"
            for w in self.WINDOWS:
                bucket = self._get_or_create_bucket(w, ts)
                bucket.counters[full_name] += value
            for k, v in tags.items():
                self._tag_index[f"{k}:{v}"].add(name)

    def gauge(self, name: str, value: float, tags: dict = None):
        """Set a gauge metric (point-in-time value)."""
        tags = tags or {}
        ts = time.time()
        with self._lock:
            self._raw.append(MetricPoint(ts, value, tags))
            full_name = f"gauge:{name}:{self._tag_key(tags)}"
            self._current_gauges[full_name] = (value, ts, tags)
            for w in self.WINDOWS:
                bucket = self._get_or_create_bucket(w, ts)
                bucket.gauges[full_name] = (value, ts)

    def histogram(self, name: str, value: float, tags: dict = None):
        """Record a histogram metric (distributions, percentiles)."""
        tags = tags or {}
        ts = time.time()
        with self._lock:
            self._raw.append(MetricPoint(ts, value, tags))
            full_name = f"hist:{name}:{self._tag_key(tags)}"
            for w in self.WINDOWS:
                bucket = self._get_or_create_bucket(w, ts)
                bucket.histograms[full_name].add(value)
            for k, v in tags.items():
                self._tag_index[f"{k}:{v}"].add(name)

    def _get_or_create_bucket(self, window: int, ts: float) -> RollupBucket:
        bucket_start = ts - (ts % window)
        buckets = self._rollups[window]
        if not buckets or buckets[-1].window_start != bucket_start:
            b = RollupBucket(bucket_start, window)
            buckets.append(b)
            max_b = self._max_rollups[window]
            if len(buckets) > max_b:
                self._rollups[window] = buckets[-max_b:]
            return b
        return buckets[-1]

    def query(self, name: str = None, window: int = 300, last_n: int = 12,
              tags: dict = None, metric_type: str = None) -> List[dict]:
        """Query rollup buckets. Returns time-series data."""
        with self._lock:
            buckets = self._rollups.get(window, [])[-last_n:]
        results = []
        for b in buckets:
            entry = {"timestamp": datetime.fromtimestamp(b.window_start, tz=timezone.utc).isoformat(),
                     "window": window}
            if metric_type in (None, "counter"):
                for k, v in b.counters.items():
                    if name and name not in k:
                        continue
                    if tags and not all(f"{tk}={tv}" in k for tk, tv in tags.items()):
                        continue
                    entry.setdefault("counters", {})[k] = v
            if metric_type in (None, "gauge"):
                for k, (v, _) in b.gauges.items():
                    if name and name not in k:
                        continue
                    entry.setdefault("gauges", {})[k] = v
            if metric_type in (None, "histogram"):
                for k, h in b.histograms.items():
                    if name and name not in k:
                        continue
                    entry.setdefault("histograms", {})[k] = h.stats()
            if len(entry) > 2:
                results.append(entry)
        return results

    def current_gauges(self, prefix: str = None) -> dict:
        with self._lock:
            if prefix:
                return {k: v[0] for k, v in self._current_gauges.items() if prefix in k}
            return {k: v[0] for k, v in self._current_gauges.items()}

    def summary(self) -> dict:
        with self._lock:
            return {
                "raw_points": len(self._raw),
                "rollup_buckets": {w: len(b) for w, b in self._rollups.items()},
                "unique_tags": len(self._tag_index),
                "active_gauges": len(self._current_gauges),
            }


# ═══════════════════════════════════════════════════════════════
#  DISTRIBUTED TRACING — spans, parent-child, waterfall
# ═══════════════════════════════════════════════════════════════

@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    operation: str
    service: str
    resource: str
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    status: str = "in_progress"  # in_progress, ok, error
    tags: Dict[str, str] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    error_message: Optional[str] = None

    def finish(self, status: str = "ok", error: str = None):
        self.end_time = time.time()
        self.duration_ms = round((self.end_time - self.start_time) * 1000, 2)
        self.status = status
        if error:
            self.error_message = error

    def add_event(self, name: str, attributes: dict = None):
        self.events.append({
            "name": name, "timestamp": time.time(),
            "attributes": attributes or {},
        })

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id, "span_id": self.span_id,
            "parent_id": self.parent_id, "operation": self.operation,
            "service": self.service, "resource": self.resource,
            "start_time": datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat(),
            "end_time": datetime.fromtimestamp(self.end_time, tz=timezone.utc).isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms, "status": self.status,
            "tags": self.tags, "events": self.events,
            "error": self.error_message,
        }


class TraceCollector:
    """Collects and indexes distributed traces across agent executions."""

    def __init__(self, max_traces: int = 5000):
        self._traces: Dict[str, List[Span]] = {}
        self._span_index: Dict[str, Span] = {}
        self._trace_order: deque = deque(maxlen=max_traces)
        self._lock = threading.Lock()
        self._span_counter = 0

    def _gen_span_id(self) -> str:
        self._span_counter += 1
        raw = f"{time.time()}-{self._span_counter}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def start_span(self, trace_id: str, operation: str, service: str = "cortex",
                   resource: str = "", parent_id: str = None,
                   tags: dict = None) -> Span:
        """Start a new span in a trace."""
        span = Span(
            trace_id=trace_id, span_id=self._gen_span_id(),
            parent_id=parent_id, operation=operation,
            service=service, resource=resource or operation,
            start_time=time.time(), tags=tags or {},
        )
        with self._lock:
            if trace_id not in self._traces:
                self._traces[trace_id] = []
                self._trace_order.append(trace_id)
                # Evict old traces
                while len(self._traces) > self._trace_order.maxlen:
                    old = self._trace_order.popleft()
                    for s in self._traces.pop(old, []):
                        self._span_index.pop(s.span_id, None)
            self._traces[trace_id].append(span)
            self._span_index[span.span_id] = span
        return span

    def end_span(self, span: Span, status: str = "ok", error: str = None):
        """End a span."""
        span.finish(status, error)

    def get_trace(self, trace_id: str) -> Optional[dict]:
        """Get a full trace with waterfall structure."""
        with self._lock:
            spans = self._traces.get(trace_id, [])
            if not spans:
                return None
        span_list = [s.to_dict() for s in spans]
        # Build tree
        root_spans = [s for s in span_list if not s["parent_id"]]
        trace_start = min(s["start_time"] for s in span_list)
        trace_end = max((s["end_time"] or s["start_time"]) for s in span_list)
        total_duration = sum(s["duration_ms"] or 0 for s in span_list if not s["parent_id"])
        error_count = sum(1 for s in span_list if s["status"] == "error")
        return {
            "trace_id": trace_id,
            "span_count": len(span_list),
            "start_time": trace_start,
            "end_time": trace_end,
            "total_duration_ms": total_duration,
            "error_count": error_count,
            "has_errors": error_count > 0,
            "services": list(set(s["service"] for s in span_list)),
            "spans": span_list,
            "root_spans": root_spans,
        }

    def list_traces(self, service: str = None, status: str = None,
                    has_errors: bool = None, limit: int = 50) -> List[dict]:
        """List recent traces with summary info."""
        with self._lock:
            trace_ids = list(self._trace_order)[-limit:]
        results = []
        for tid in reversed(trace_ids):
            trace = self.get_trace(tid)
            if not trace:
                continue
            if service and service not in trace["services"]:
                continue
            if has_errors is not None and trace["has_errors"] != has_errors:
                continue
            results.append({
                "trace_id": tid,
                "span_count": trace["span_count"],
                "start_time": trace["start_time"],
                "total_duration_ms": trace["total_duration_ms"],
                "has_errors": trace["has_errors"],
                "services": trace["services"],
            })
        return results[:limit]

    def get_span(self, span_id: str) -> Optional[dict]:
        with self._lock:
            span = self._span_index.get(span_id)
        return span.to_dict() if span else None

    def service_map(self) -> dict:
        """Build a service dependency map from traces."""
        edges: Dict[Tuple[str, str], int] = defaultdict(int)
        services: Dict[str, dict] = defaultdict(lambda: {"span_count": 0, "error_count": 0})
        with self._lock:
            for spans in self._traces.values():
                parent_service = {}
                for s in spans:
                    services[s.service]["span_count"] += 1
                    if s.status == "error":
                        services[s.service]["error_count"] += 1
                    parent_service[s.span_id] = s.service
                    if s.parent_id and s.parent_id in parent_service:
                        src = parent_service[s.parent_id]
                        if src != s.service:
                            edges[(src, s.service)] += 1
        return {
            "services": dict(services),
            "edges": [{"from": e[0], "to": e[1], "count": c} for e, c in edges.items()],
        }

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_traces": len(self._traces),
                "total_spans": len(self._span_index),
                "active_spans": sum(1 for s in self._span_index.values() if s.status == "in_progress"),
            }


# ═══════════════════════════════════════════════════════════════
#  LOG AGGREGATOR — structured logs, search, pattern detection
# ═══════════════════════════════════════════════════════════════

@dataclass
class LogEntry:
    timestamp: float
    level: str  # debug, info, warn, error, fatal
    message: str
    attributes: Dict[str, Any]
    source: str
    trace_id: Optional[str] = None
    span_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "timestamp": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "level": self.level, "message": self.message,
            "attributes": self.attributes, "source": self.source,
            "trace_id": self.trace_id, "span_id": self.span_id,
        }


class LogAggregator:
    """Structured log collection with search and pattern detection."""

    LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3, "fatal": 4}

    def __init__(self, max_entries: int = 100000):
        self._entries: deque = deque(maxlen=max_entries)
        self._level_counts: Dict[str, int] = defaultdict(int)
        self._source_counts: Dict[str, int] = defaultdict(int)
        self._pattern_counts: Dict[str, int] = defaultdict(int)
        self._error_fingerprints: Dict[str, dict] = {}  # fingerprint -> {count, first, last, message}
        self._lock = threading.Lock()

    def ingest(self, level: str, message: str, attributes: dict = None,
               source: str = "cortex", trace_id: str = None, span_id: str = None):
        """Ingest a structured log entry."""
        entry = LogEntry(
            timestamp=time.time(), level=level, message=message,
            attributes=attributes or {}, source=source,
            trace_id=trace_id, span_id=span_id,
        )
        with self._lock:
            self._entries.append(entry)
            self._level_counts[level] += 1
            self._source_counts[source] += 1
            # Error fingerprinting — group similar errors
            if level in ("error", "fatal"):
                fp = self._fingerprint(message)
                if fp in self._error_fingerprints:
                    self._error_fingerprints[fp]["count"] += 1
                    self._error_fingerprints[fp]["last_seen"] = entry.timestamp
                else:
                    self._error_fingerprints[fp] = {
                        "count": 1, "message": message[:200],
                        "first_seen": entry.timestamp, "last_seen": entry.timestamp,
                        "source": source,
                    }
        return entry

    def _fingerprint(self, message: str) -> str:
        """Generate a fingerprint for error grouping (strip numbers, hashes, IDs)."""
        import re
        normalized = re.sub(r'[0-9a-f]{8,}', '<ID>', message)
        normalized = re.sub(r'\d+', '<N>', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def search(self, query: str = None, level: str = None, source: str = None,
               trace_id: str = None, since: float = None, until: float = None,
               limit: int = 100, offset: int = 0) -> dict:
        """Search logs with filters."""
        min_level = self.LEVELS.get(level, 0) if level else 0
        with self._lock:
            entries = list(self._entries)
        results = []
        for e in reversed(entries):
            if since and e.timestamp < since:
                continue
            if until and e.timestamp > until:
                continue
            if self.LEVELS.get(e.level, 0) < min_level:
                continue
            if source and e.source != source:
                continue
            if trace_id and e.trace_id != trace_id:
                continue
            if query and query.lower() not in e.message.lower():
                attr_str = json.dumps(e.attributes).lower()
                if query.lower() not in attr_str:
                    continue
            results.append(e.to_dict())
        total = len(results)
        return {
            "total": total,
            "entries": results[offset:offset + limit],
            "has_more": total > offset + limit,
        }

    def error_groups(self, limit: int = 50) -> List[dict]:
        """Get grouped error patterns, sorted by frequency."""
        with self._lock:
            groups = list(self._error_fingerprints.values())
        groups.sort(key=lambda g: g["count"], reverse=True)
        for g in groups:
            g["first_seen"] = datetime.fromtimestamp(g["first_seen"], tz=timezone.utc).isoformat()
            g["last_seen"] = datetime.fromtimestamp(g["last_seen"], tz=timezone.utc).isoformat()
        return groups[:limit]

    def level_distribution(self, since: float = None) -> dict:
        """Get log level distribution."""
        if since:
            counts = defaultdict(int)
            with self._lock:
                for e in self._entries:
                    if e.timestamp >= since:
                        counts[e.level] += 1
            return dict(counts)
        with self._lock:
            return dict(self._level_counts)

    def throughput(self, window_seconds: int = 60, buckets: int = 30) -> List[dict]:
        """Get log throughput over time (logs per window)."""
        now = time.time()
        result = []
        with self._lock:
            entries = list(self._entries)
        for i in range(buckets):
            start = now - (buckets - i) * window_seconds
            end = start + window_seconds
            count = sum(1 for e in entries if start <= e.timestamp < end)
            result.append({
                "timestamp": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                "count": count,
            })
        return result

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_entries": len(self._entries),
                "level_counts": dict(self._level_counts),
                "source_counts": dict(self._source_counts),
                "error_groups": len(self._error_fingerprints),
            }


# ═══════════════════════════════════════════════════════════════
#  ALERT ENGINE — rules, thresholds, anomaly detection
# ═══════════════════════════════════════════════════════════════

@dataclass
class AlertRule:
    id: str
    name: str
    description: str
    metric: str
    condition: str  # gt, lt, gte, lte, eq, anomaly
    threshold: float
    window_seconds: int  # evaluation window
    severity: str  # info, warning, critical, fatal
    tags_filter: Dict[str, str]  # only evaluate metrics matching these tags
    enabled: bool = True
    notification_channels: List[str] = field(default_factory=list)  # slack, email, webhook, pagerduty
    cooldown_seconds: int = 300
    last_triggered: Optional[float] = None
    consecutive_breaches: int = 0
    min_breaches: int = 1  # must breach N times before firing
    created_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "metric": self.metric, "condition": self.condition,
            "threshold": self.threshold, "window_seconds": self.window_seconds,
            "severity": self.severity, "tags_filter": self.tags_filter,
            "enabled": self.enabled, "notification_channels": self.notification_channels,
            "cooldown_seconds": self.cooldown_seconds,
            "last_triggered": datetime.fromtimestamp(self.last_triggered, tz=timezone.utc).isoformat() if self.last_triggered else None,
            "consecutive_breaches": self.consecutive_breaches,
            "min_breaches": self.min_breaches, "created_by": self.created_by,
        }


@dataclass
class Alert:
    id: str
    rule_id: str
    rule_name: str
    severity: str
    message: str
    metric_value: float
    threshold: float
    triggered_at: float
    resolved_at: Optional[float] = None
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[float] = None
    status: str = "firing"  # firing, acknowledged, resolved, silenced

    def to_dict(self) -> dict:
        return {
            "id": self.id, "rule_id": self.rule_id, "rule_name": self.rule_name,
            "severity": self.severity, "message": self.message,
            "metric_value": self.metric_value, "threshold": self.threshold,
            "triggered_at": datetime.fromtimestamp(self.triggered_at, tz=timezone.utc).isoformat(),
            "resolved_at": datetime.fromtimestamp(self.resolved_at, tz=timezone.utc).isoformat() if self.resolved_at else None,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": datetime.fromtimestamp(self.acknowledged_at, tz=timezone.utc).isoformat() if self.acknowledged_at else None,
            "status": self.status,
        }


class AlertEngine:
    """Threshold and anomaly-based alerting with escalation policies."""

    def __init__(self, metrics_engine: MetricsEngine):
        self._metrics = metrics_engine
        self._rules: Dict[str, AlertRule] = {}
        self._alerts: List[Alert] = []
        self._silences: Dict[str, dict] = {}  # rule_id -> {until, reason, created_by}
        self._alert_counter = 0
        self._rule_counter = 0
        self._lock = threading.Lock()

        # Anomaly detection — track rolling baselines
        self._baselines: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

        # Default rules
        self._install_defaults()

    def _install_defaults(self):
        defaults = [
            ("High Error Rate", "agent.run.errors", "gt", 5, 300, "critical",
             "Agent error rate exceeds 5 errors per 5 minutes"),
            ("High Latency", "agent.run.latency", "gt", 30.0, 300, "warning",
             "Agent run latency exceeds 30 seconds (p95)"),
            ("Token Budget Exceeded", "agent.run.tokens", "gt", 100000, 3600, "warning",
             "Agent consumed over 100k tokens in the past hour"),
            ("Daemon Consecutive Errors", "daemon.consecutive_errors", "gte", 5, 60, "critical",
             "Daemon has 5+ consecutive errors"),
        ]
        for name, metric, cond, thresh, window, sev, desc in defaults:
            self.create_rule(name, metric, cond, thresh, window, sev, desc, created_by="system")

    def create_rule(self, name: str, metric: str, condition: str, threshold: float,
                    window_seconds: int, severity: str, description: str = "",
                    tags_filter: dict = None, notification_channels: list = None,
                    cooldown: int = 300, min_breaches: int = 1,
                    created_by: str = None) -> dict:
        self._rule_counter += 1
        rule_id = f"rule-{self._rule_counter:04d}"
        rule = AlertRule(
            id=rule_id, name=name, description=description, metric=metric,
            condition=condition, threshold=threshold, window_seconds=window_seconds,
            severity=severity, tags_filter=tags_filter or {},
            notification_channels=notification_channels or [],
            cooldown_seconds=cooldown, min_breaches=min_breaches,
            created_by=created_by,
        )
        self._rules[rule_id] = rule
        return {"ok": True, "rule_id": rule_id, "rule": rule.to_dict()}

    def update_rule(self, rule_id: str, **kwargs) -> dict:
        rule = self._rules.get(rule_id)
        if not rule:
            return {"ok": False, "error": "rule not found"}
        for k, v in kwargs.items():
            if hasattr(rule, k):
                setattr(rule, k, v)
        return {"ok": True, "rule": rule.to_dict()}

    def delete_rule(self, rule_id: str) -> dict:
        if rule_id in self._rules:
            del self._rules[rule_id]
            return {"ok": True}
        return {"ok": False, "error": "not found"}

    def evaluate(self) -> List[dict]:
        """Evaluate all alert rules against current metrics. Returns newly fired alerts."""
        now = time.time()
        new_alerts = []
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            # Check silence
            silence = self._silences.get(rule.id)
            if silence and silence["until"] > now:
                continue
            # Check cooldown
            if rule.last_triggered and (now - rule.last_triggered) < rule.cooldown_seconds:
                continue
            # Get metric value
            value = self._get_metric_value(rule)
            if value is None:
                rule.consecutive_breaches = 0
                continue
            # Evaluate condition
            breached = self._check_condition(rule.condition, value, rule.threshold)
            if breached:
                rule.consecutive_breaches += 1
                if rule.consecutive_breaches >= rule.min_breaches:
                    alert = self._fire_alert(rule, value)
                    new_alerts.append(alert.to_dict())
            else:
                if rule.consecutive_breaches > 0:
                    # Auto-resolve any firing alerts for this rule
                    self._resolve_rule_alerts(rule.id, now)
                rule.consecutive_breaches = 0
            # Feed anomaly baseline
            if value is not None:
                self._baselines[rule.metric].append(value)
        return new_alerts

    def _get_metric_value(self, rule: AlertRule) -> Optional[float]:
        """Get the current metric value for evaluation."""
        data = self._metrics.query(name=rule.metric, window=rule.window_seconds, last_n=1)
        if not data:
            return None
        bucket = data[-1]
        # Check counters
        for k, v in bucket.get("counters", {}).items():
            if rule.metric in k:
                return v
        # Check gauges
        for k, v in bucket.get("gauges", {}).items():
            if rule.metric in k:
                return v
        # Check histograms (use p95 by default)
        for k, h in bucket.get("histograms", {}).items():
            if rule.metric in k:
                return h.get("p95", 0)
        return None

    def _check_condition(self, condition: str, value: float, threshold: float) -> bool:
        if condition == "gt":
            return value > threshold
        elif condition == "lt":
            return value < threshold
        elif condition == "gte":
            return value >= threshold
        elif condition == "lte":
            return value <= threshold
        elif condition == "eq":
            return value == threshold
        elif condition == "anomaly":
            return self._is_anomaly(value, threshold)
        return False

    def _is_anomaly(self, value: float, sensitivity: float) -> bool:
        """Simple anomaly detection using z-score against rolling baseline."""
        # Sensitivity is the z-score threshold (e.g. 3.0 = 3 standard deviations)
        # This is a simplified version; a production system would use more sophisticated methods
        return False  # placeholder — need enough baseline data

    def _fire_alert(self, rule: AlertRule, value: float) -> Alert:
        self._alert_counter += 1
        alert = Alert(
            id=f"alert-{self._alert_counter:05d}",
            rule_id=rule.id, rule_name=rule.name,
            severity=rule.severity,
            message=f"{rule.name}: {rule.metric} = {value:.2f} (threshold: {rule.threshold})",
            metric_value=value, threshold=rule.threshold,
            triggered_at=time.time(),
        )
        rule.last_triggered = time.time()
        with self._lock:
            self._alerts.append(alert)
        return alert

    def _resolve_rule_alerts(self, rule_id: str, now: float):
        with self._lock:
            for a in self._alerts:
                if a.rule_id == rule_id and a.status == "firing":
                    a.status = "resolved"
                    a.resolved_at = now

    def acknowledge(self, alert_id: str, user_id: str) -> dict:
        with self._lock:
            for a in self._alerts:
                if a.id == alert_id:
                    a.status = "acknowledged"
                    a.acknowledged_by = user_id
                    a.acknowledged_at = time.time()
                    return {"ok": True, "alert": a.to_dict()}
        return {"ok": False, "error": "not found"}

    def resolve(self, alert_id: str) -> dict:
        with self._lock:
            for a in self._alerts:
                if a.id == alert_id:
                    a.status = "resolved"
                    a.resolved_at = time.time()
                    return {"ok": True}
        return {"ok": False, "error": "not found"}

    def silence(self, rule_id: str, duration_seconds: int, reason: str = "",
                created_by: str = None) -> dict:
        if rule_id not in self._rules:
            return {"ok": False, "error": "rule not found"}
        self._silences[rule_id] = {
            "until": time.time() + duration_seconds,
            "reason": reason, "created_by": created_by,
        }
        return {"ok": True, "silenced_until": datetime.fromtimestamp(
            self._silences[rule_id]["until"], tz=timezone.utc).isoformat()}

    def list_alerts(self, status: str = None, severity: str = None,
                    limit: int = 100) -> List[dict]:
        with self._lock:
            alerts = list(self._alerts)
        if status:
            alerts = [a for a in alerts if a.status == status]
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        return [a.to_dict() for a in reversed(alerts[-limit:])]

    def list_rules(self) -> List[dict]:
        return [r.to_dict() for r in self._rules.values()]

    def stats(self) -> dict:
        with self._lock:
            firing = sum(1 for a in self._alerts if a.status == "firing")
            acknowledged = sum(1 for a in self._alerts if a.status == "acknowledged")
        return {
            "total_rules": len(self._rules),
            "enabled_rules": sum(1 for r in self._rules.values() if r.enabled),
            "total_alerts": len(self._alerts),
            "firing": firing, "acknowledged": acknowledged,
            "silenced_rules": len([s for s in self._silences.values() if s["until"] > time.time()]),
        }


# ═══════════════════════════════════════════════════════════════
#  SLO TRACKER — service-level objectives with burn-rate
# ═══════════════════════════════════════════════════════════════

@dataclass
class SLO:
    id: str
    name: str
    description: str
    metric: str
    target: float  # e.g. 0.999 for 99.9% success rate
    window_days: int  # e.g. 30 for a 30-day rolling window
    sli_good_condition: str  # condition that defines "good" — e.g. "latency < 5.0"
    created_at: float = field(default_factory=time.time)
    created_by: Optional[str] = None

    # Tracking
    total_events: int = 0
    good_events: int = 0

    @property
    def current_sli(self) -> float:
        if self.total_events == 0:
            return 1.0
        return self.good_events / self.total_events

    @property
    def error_budget_remaining(self) -> float:
        """How much of the error budget is left (0.0 - 1.0)."""
        allowed_bad = (1 - self.target) * self.total_events
        actual_bad = self.total_events - self.good_events
        if allowed_bad == 0:
            return 0.0 if actual_bad > 0 else 1.0
        return max(0.0, 1.0 - (actual_bad / allowed_bad))

    @property
    def burn_rate(self) -> float:
        """Current burn rate — >1.0 means burning error budget faster than sustainable."""
        if self.total_events == 0:
            return 0.0
        actual_error_rate = 1 - self.current_sli
        allowed_error_rate = 1 - self.target
        if allowed_error_rate == 0:
            return float("inf") if actual_error_rate > 0 else 0.0
        return actual_error_rate / allowed_error_rate

    def record(self, is_good: bool):
        self.total_events += 1
        if is_good:
            self.good_events += 1

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "metric": self.metric, "target": self.target,
            "target_pct": f"{self.target * 100:.2f}%",
            "window_days": self.window_days,
            "current_sli": round(self.current_sli, 6),
            "current_sli_pct": f"{self.current_sli * 100:.3f}%",
            "error_budget_remaining": round(self.error_budget_remaining, 4),
            "error_budget_remaining_pct": f"{self.error_budget_remaining * 100:.1f}%",
            "burn_rate": round(self.burn_rate, 2),
            "is_healthy": self.burn_rate <= 1.0,
            "total_events": self.total_events,
            "good_events": self.good_events,
            "bad_events": self.total_events - self.good_events,
            "created_at": datetime.fromtimestamp(self.created_at, tz=timezone.utc).isoformat(),
        }


class SLOTracker:
    """Track service-level objectives with error budget and burn rate."""

    def __init__(self):
        self._slos: Dict[str, SLO] = {}
        self._slo_counter = 0
        self._history: Dict[str, List[dict]] = defaultdict(list)  # slo_id -> snapshots
        self._lock = threading.Lock()
        self._install_defaults()

    def _install_defaults(self):
        self.create("Agent Run Success Rate", "agent.run.success",
                     0.995, 30, "outcome == success",
                     "99.5% of agent runs complete successfully over 30 days",
                     created_by="system")
        self.create("Agent Latency SLO", "agent.run.latency",
                     0.99, 30, "latency < 10.0",
                     "99% of agent runs complete under 10 seconds",
                     created_by="system")
        self.create("API Availability", "api.request.success",
                     0.999, 30, "status < 500",
                     "99.9% of API requests succeed over 30 days",
                     created_by="system")

    def create(self, name: str, metric: str, target: float, window_days: int,
               sli_good_condition: str, description: str = "",
               created_by: str = None) -> dict:
        self._slo_counter += 1
        slo_id = f"slo-{self._slo_counter:04d}"
        slo = SLO(
            id=slo_id, name=name, description=description,
            metric=metric, target=target, window_days=window_days,
            sli_good_condition=sli_good_condition, created_by=created_by,
        )
        self._slos[slo_id] = slo
        return {"ok": True, "slo_id": slo_id, "slo": slo.to_dict()}

    def record_event(self, slo_id: str, is_good: bool) -> dict:
        slo = self._slos.get(slo_id)
        if not slo:
            return {"ok": False, "error": "not found"}
        slo.record(is_good)
        # Snapshot for history
        with self._lock:
            history = self._history[slo_id]
            history.append({
                "timestamp": time.time(),
                "sli": slo.current_sli,
                "error_budget": slo.error_budget_remaining,
                "burn_rate": slo.burn_rate,
            })
            if len(history) > 10000:
                self._history[slo_id] = history[-10000:]
        return {"ok": True, "slo": slo.to_dict()}

    def record_by_metric(self, metric: str, is_good: bool):
        """Record an event for all SLOs tracking this metric."""
        for slo in self._slos.values():
            if slo.metric == metric:
                slo.record(is_good)

    def get(self, slo_id: str) -> Optional[dict]:
        slo = self._slos.get(slo_id)
        return slo.to_dict() if slo else None

    def list_slos(self) -> List[dict]:
        return [s.to_dict() for s in self._slos.values()]

    def burn_rate_alerts(self) -> List[dict]:
        """Get SLOs with burn rate > 1.0 (consuming error budget too fast)."""
        return [s.to_dict() for s in self._slos.values() if s.burn_rate > 1.0]

    def history(self, slo_id: str, limit: int = 100) -> List[dict]:
        with self._lock:
            return self._history.get(slo_id, [])[-limit:]

    def stats(self) -> dict:
        healthy = sum(1 for s in self._slos.values() if s.burn_rate <= 1.0)
        return {
            "total_slos": len(self._slos),
            "healthy": healthy,
            "at_risk": len(self._slos) - healthy,
        }


# ═══════════════════════════════════════════════════════════════
#  HEALTH SCORER — composite agent health (0-100)
# ═══════════════════════════════════════════════════════════════

class HealthScorer:
    """Composite health scoring for agents (0-100 scale)."""

    # Weights for each health dimension
    WEIGHTS = {
        "success_rate": 30,    # % of successful runs
        "latency": 20,         # p95 latency vs threshold
        "error_trend": 15,     # declining error rate = good
        "uptime": 15,          # daemon uptime / reliability
        "throughput": 10,      # runs per hour vs expected
        "slo_compliance": 10,  # SLO error budget health
    }

    def __init__(self, metrics: MetricsEngine, slo_tracker: SLOTracker):
        self._metrics = metrics
        self._slos = slo_tracker
        self._scores: Dict[str, dict] = {}  # agent_id -> score breakdown
        self._history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

    def score(self, agent_id: str, run_stats: dict = None) -> dict:
        """Calculate composite health score for an agent."""
        run_stats = run_stats or {}
        dimensions = {}

        # Success rate (0-100)
        total_runs = run_stats.get("total_runs", 0)
        success_runs = run_stats.get("success_runs", 0)
        if total_runs > 0:
            dimensions["success_rate"] = min(100, (success_runs / total_runs) * 100)
        else:
            dimensions["success_rate"] = 100  # no runs = healthy by default

        # Latency score (lower is better; 100 = under 2s, 0 = over 60s)
        p95_latency = run_stats.get("p95_latency", 0)
        if p95_latency <= 2:
            dimensions["latency"] = 100
        elif p95_latency >= 60:
            dimensions["latency"] = 0
        else:
            dimensions["latency"] = max(0, 100 - ((p95_latency - 2) / 58) * 100)

        # Error trend (fewer recent errors than past = improving)
        recent_errors = run_stats.get("recent_errors", 0)
        past_errors = run_stats.get("past_errors", 0)
        if past_errors == 0 and recent_errors == 0:
            dimensions["error_trend"] = 100
        elif past_errors == 0:
            dimensions["error_trend"] = max(0, 100 - recent_errors * 20)
        else:
            ratio = recent_errors / max(past_errors, 1)
            dimensions["error_trend"] = max(0, min(100, (1 - ratio) * 100 + 50))

        # Uptime
        uptime_pct = run_stats.get("uptime_pct", 100)
        dimensions["uptime"] = min(100, uptime_pct)

        # Throughput (runs/hour vs expected)
        actual_rph = run_stats.get("runs_per_hour", 0)
        expected_rph = run_stats.get("expected_runs_per_hour", 1)
        if expected_rph > 0:
            ratio = actual_rph / expected_rph
            dimensions["throughput"] = min(100, ratio * 100)
        else:
            dimensions["throughput"] = 100

        # SLO compliance
        slos = self._slos.list_slos()
        if slos:
            avg_budget = sum(s["error_budget_remaining"] for s in slos) / len(slos)
            dimensions["slo_compliance"] = min(100, avg_budget * 100)
        else:
            dimensions["slo_compliance"] = 100

        # Weighted composite
        total_score = sum(
            dimensions.get(dim, 100) * weight / 100
            for dim, weight in self.WEIGHTS.items()
        )
        total_score = round(min(100, max(0, total_score)), 1)

        # Determine status
        if total_score >= 90:
            status = "healthy"
        elif total_score >= 70:
            status = "degraded"
        elif total_score >= 50:
            status = "at_risk"
        else:
            status = "critical"

        result = {
            "agent_id": agent_id,
            "score": total_score,
            "status": status,
            "dimensions": {k: round(v, 1) for k, v in dimensions.items()},
            "weights": self.WEIGHTS,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._scores[agent_id] = result
        self._history[agent_id].append({
            "timestamp": time.time(), "score": total_score, "status": status,
        })
        return result

    def get_score(self, agent_id: str) -> Optional[dict]:
        return self._scores.get(agent_id)

    def fleet_health(self) -> dict:
        """Get fleet-wide health summary."""
        if not self._scores:
            return {"agents": 0, "avg_score": 100, "status": "healthy",
                    "by_status": {}, "scores": []}
        scores = list(self._scores.values())
        avg = sum(s["score"] for s in scores) / len(scores)
        by_status = defaultdict(int)
        for s in scores:
            by_status[s["status"]] += 1
        if avg >= 90:
            fleet_status = "healthy"
        elif avg >= 70:
            fleet_status = "degraded"
        else:
            fleet_status = "critical"
        return {
            "agents": len(scores),
            "avg_score": round(avg, 1),
            "status": fleet_status,
            "by_status": dict(by_status),
            "scores": sorted(scores, key=lambda s: s["score"]),
        }

    def score_history(self, agent_id: str, limit: int = 100) -> List[dict]:
        return list(self._history.get(agent_id, []))[-limit:]


# ═══════════════════════════════════════════════════════════════
#  UNIFIED OBSERVABILITY FACADE
# ═══════════════════════════════════════════════════════════════

class Observability:
    """Unified facade — single entry point for all observability."""

    def __init__(self):
        self.metrics = MetricsEngine()
        self.traces = TraceCollector()
        self.logs = LogAggregator()
        self.slos = SLOTracker()
        self.alerts = AlertEngine(self.metrics)
        self.health = HealthScorer(self.metrics, self.slos)

    # ── Convenience methods ──

    def metric(self, name: str, value: float, tags: dict = None, metric_type: str = "histogram"):
        """Record a metric (auto-selects type)."""
        if metric_type == "counter":
            self.metrics.counter(name, value, tags)
        elif metric_type == "gauge":
            self.metrics.gauge(name, value, tags)
        else:
            self.metrics.histogram(name, value, tags)

    def log(self, level: str, message: str, attributes: dict = None,
            source: str = "cortex", trace_id: str = None):
        """Ingest a log entry."""
        self.logs.ingest(level, message, attributes, source, trace_id)

    def trace_start(self, trace_id: str, operation: str, service: str = "cortex",
                    resource: str = "", parent_id: str = None, tags: dict = None) -> Span:
        """Start a trace span."""
        return self.traces.start_span(trace_id, operation, service, resource, parent_id, tags)

    def trace_end(self, span: Span, status: str = "ok", error: str = None):
        """End a trace span."""
        self.traces.end_span(span, status, error)
        # Auto-record metrics from span
        self.metrics.histogram(
            f"{span.service}.{span.operation}.duration",
            span.duration_ms / 1000.0 if span.duration_ms else 0,
            tags={"service": span.service, "operation": span.operation, "status": status},
        )
        if status == "error":
            self.metrics.counter(
                f"{span.service}.{span.operation}.errors", 1,
                tags={"service": span.service, "operation": span.operation},
            )

    def record_run(self, agent_id: str, run_id: str, duration_seconds: float,
                   outcome: str, tokens_used: int, user_id: str = None):
        """Record a complete agent run — updates metrics, SLOs, logs."""
        tags = {"agent_id": agent_id, "outcome": outcome}
        if user_id:
            tags["user_id"] = user_id

        self.metrics.histogram("agent.run.latency", duration_seconds, tags)
        self.metrics.histogram("agent.run.tokens", tokens_used, tags)
        self.metrics.counter("agent.run.count", 1, tags)

        is_success = outcome in ("success", "completed")
        if not is_success:
            self.metrics.counter("agent.run.errors", 1, tags)

        # SLO tracking
        self.slos.record_by_metric("agent.run.success", is_success)
        self.slos.record_by_metric("agent.run.latency", duration_seconds < 10.0)

        # Log
        level = "info" if is_success else "error"
        self.logs.ingest(level, f"Run {run_id} {outcome} in {duration_seconds:.1f}s ({tokens_used} tokens)",
                         {"agent_id": agent_id, "run_id": run_id, "duration": duration_seconds,
                          "outcome": outcome, "tokens": tokens_used, "user_id": user_id},
                         source=f"agent:{agent_id}", trace_id=run_id)

    def record_api_request(self, method: str, path: str, status_code: int,
                           duration_ms: float, user_id: str = None):
        """Record an API request for metrics and SLOs."""
        tags = {"method": method, "path": path, "status": str(status_code)}
        if user_id:
            tags["user_id"] = user_id
        self.metrics.histogram("api.request.duration", duration_ms, tags)
        self.metrics.counter("api.request.count", 1, tags)
        self.slos.record_by_metric("api.request.success", status_code < 500)

    def check_alerts(self) -> List[dict]:
        """Evaluate all alert rules."""
        return self.alerts.evaluate()

    def dashboard_summary(self) -> dict:
        """Get a full observability summary for the dashboard."""
        return {
            "metrics": self.metrics.summary(),
            "traces": self.traces.stats(),
            "logs": self.logs.stats(),
            "alerts": self.alerts.stats(),
            "slos": {
                "summary": self.slos.stats(),
                "slos": self.slos.list_slos(),
                "burn_rate_alerts": self.slos.burn_rate_alerts(),
            },
            "health": self.health.fleet_health(),
        }


# Singleton
obs = Observability()
