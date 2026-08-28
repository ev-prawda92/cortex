"""
CORTEX Usage & Cost Tracking
═════════════════════════════
Per-user, per-agent, per-provider usage metering and cost attribution.

CORTEX does NOT own customer LLM spend — customers bring their own API keys.
This engine gives them visibility and governance over that spend: what each
agent, user, and provider costs, projected monthly burn, and budget guardrails.

Components:
  - PricingTable:   current per-model token pricing (USD per 1M tokens)
  - CostCalculator: convert token counts to cost by provider/model
  - UsageAggregator: roll up usage by user / agent / provider / day
  - BudgetManager:  per-agent and fleet budgets with alerts

Usage:
    from usage import usage_tracker
    usage_tracker.record(agent_id="a1", user_id="u1", provider="anthropic",
                         model="claude-opus-4-6", input_tokens=1200, output_tokens=800)
    usage_tracker.summary_by_agent()
"""

import json
import time
import threading
from datetime import datetime, timezone, date, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict


# ═══════════════════════════════════════════════════════════════
#  PRICING TABLE — USD per 1M tokens (input, output)
#  These are list prices customers can override with their negotiated rates.
# ═══════════════════════════════════════════════════════════════

PRICING = {
    # Anthropic
    "claude-opus-4-6":        {"input": 15.00, "output": 75.00},
    "claude-opus-4-1":        {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-5":      {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4":        {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":       {"input": 1.00,  "output": 5.00},
    "claude-3-5-haiku":       {"input": 0.80,  "output": 4.00},
    # OpenAI
    "gpt-5":                  {"input": 10.00, "output": 30.00},
    "gpt-5-mini":             {"input": 0.50,  "output": 2.00},
    "gpt-4o":                 {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":            {"input": 0.15,  "output": 0.60},
    "o3":                     {"input": 10.00, "output": 40.00},
    # Google
    "gemini-2.5-pro":         {"input": 1.25,  "output": 10.00},
    "gemini-2.5-flash":       {"input": 0.30,  "output": 2.50},
    # xAI
    "grok-4":                 {"input": 5.00,  "output": 15.00},
    "grok-3":                 {"input": 3.00,  "output": 15.00},
    # Mistral
    "mistral-large":          {"input": 2.00,  "output": 6.00},
    "mistral-small":          {"input": 0.20,  "output": 0.60},
    # Cohere
    "command-r-plus":         {"input": 2.50,  "output": 10.00},
    "command-r":              {"input": 0.15,  "output": 0.60},
    # Perplexity
    "sonar-pro":              {"input": 3.00,  "output": 15.00},
    "sonar":                  {"input": 1.00,  "output": 1.00},
    # Meta (self-hosted / via providers — nominal)
    "llama-4-maverick":       {"input": 0.20,  "output": 0.60},
    "llama-3.3-70b":          {"input": 0.10,  "output": 0.30},
}

DEFAULT_PRICE = {"input": 3.00, "output": 15.00}  # fallback for unknown models


class CostCalculator:
    """Converts token usage to cost."""

    def __init__(self, pricing: dict = None):
        self._pricing = dict(PRICING)
        if pricing:
            self._pricing.update(pricing)
        self._lock = threading.Lock()

    def set_price(self, model: str, input_price: float, output_price: float):
        """Override pricing for a model (customer's negotiated rate)."""
        with self._lock:
            self._pricing[model] = {"input": input_price, "output": output_price}

    def price_for(self, model: str) -> dict:
        # Try exact, then prefix match
        if model in self._pricing:
            return self._pricing[model]
        for k, v in self._pricing.items():
            if model.startswith(k) or k.startswith(model):
                return v
        return DEFAULT_PRICE

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self.price_for(model)
        return round(
            (input_tokens / 1_000_000) * price["input"] +
            (output_tokens / 1_000_000) * price["output"],
            6,
        )

    def all_prices(self) -> dict:
        with self._lock:
            return dict(self._pricing)


# ═══════════════════════════════════════════════════════════════
#  USAGE RECORD & AGGREGATION
# ═══════════════════════════════════════════════════════════════

class UsageRecord:
    __slots__ = ("timestamp", "agent_id", "user_id", "provider", "model",
                 "input_tokens", "output_tokens", "cost", "run_id")

    def __init__(self, agent_id: str, user_id: str, provider: str, model: str,
                 input_tokens: int, output_tokens: int, cost: float, run_id: str = None):
        self.timestamp = time.time()
        self.agent_id = agent_id
        self.user_id = user_id
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost = cost
        self.run_id = run_id

    def to_dict(self):
        return {
            "timestamp": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "agent_id": self.agent_id, "user_id": self.user_id,
            "provider": self.provider, "model": self.model,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "cost": self.cost, "run_id": self.run_id,
        }


class UsageTracker:
    """Central usage metering with cost attribution and budget enforcement."""

    def __init__(self, max_records: int = 200000):
        self._records: List[UsageRecord] = []
        self._max_records = max_records
        self._calc = CostCalculator()
        self._lock = threading.Lock()

        # Denormalized running totals for fast reads
        self._by_agent: Dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "runs": 0})
        self._by_user: Dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "runs": 0})
        self._by_provider: Dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "runs": 0})
        self._by_model: Dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "runs": 0})
        self._by_day: Dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "runs": 0})

        # Budgets: agent_id -> {daily_limit, monthly_limit}; "_fleet" for fleet-wide
        self._budgets: Dict[str, dict] = {}

    @property
    def calculator(self) -> CostCalculator:
        return self._calc

    def record(self, agent_id: str, user_id: str, provider: str, model: str,
               input_tokens: int, output_tokens: int, run_id: str = None) -> dict:
        """Record a usage event and return the computed cost."""
        cost = self._calc.cost(model, input_tokens, output_tokens)
        rec = UsageRecord(agent_id, user_id, provider, model,
                          input_tokens, output_tokens, cost, run_id)
        total_tokens = input_tokens + output_tokens
        day = date.fromtimestamp(rec.timestamp).isoformat()

        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]

            for bucket, key in ((self._by_agent, agent_id), (self._by_user, user_id or "unknown"),
                                (self._by_provider, provider), (self._by_model, model),
                                (self._by_day, day)):
                bucket[key]["tokens"] += total_tokens
                bucket[key]["cost"] = round(bucket[key]["cost"] + cost, 6)
                bucket[key]["runs"] += 1

        return {"ok": True, "cost": cost, "total_tokens": total_tokens}

    # ── Summaries ──

    def _sorted_summary(self, bucket: dict, key_name: str, limit: int) -> List[dict]:
        items = [{key_name: k, **v} for k, v in bucket.items()]
        items.sort(key=lambda x: x["cost"], reverse=True)
        return items[:limit]

    def summary_by_agent(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return self._sorted_summary(self._by_agent, "agent_id", limit)

    def summary_by_user(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return self._sorted_summary(self._by_user, "user_id", limit)

    def summary_by_provider(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return self._sorted_summary(self._by_provider, "provider", limit)

    def summary_by_model(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return self._sorted_summary(self._by_model, "model", limit)

    def daily_series(self, days: int = 30) -> List[dict]:
        """Cost/token time-series for the last N days."""
        today = date.today()
        result = []
        with self._lock:
            for i in range(days):
                d = (today - timedelta(days=days - 1 - i)).isoformat()
                entry = self._by_day.get(d, {"tokens": 0, "cost": 0.0, "runs": 0})
                result.append({"date": d, **entry})
        return result

    def totals(self) -> dict:
        with self._lock:
            total_cost = sum(v["cost"] for v in self._by_day.values())
            total_tokens = sum(v["tokens"] for v in self._by_day.values())
            total_runs = sum(v["runs"] for v in self._by_day.values())
        # Projected monthly burn from last 7 days
        recent = self.daily_series(7)
        week_cost = sum(d["cost"] for d in recent)
        projected_monthly = round((week_cost / 7) * 30, 2)
        return {
            "total_cost": round(total_cost, 4),
            "total_tokens": total_tokens,
            "total_runs": total_runs,
            "avg_cost_per_run": round(total_cost / total_runs, 6) if total_runs else 0,
            "projected_monthly_cost": projected_monthly,
        }

    def agent_cost(self, agent_id: str) -> dict:
        with self._lock:
            return dict(self._by_agent.get(agent_id, {"tokens": 0, "cost": 0.0, "runs": 0}))

    def user_cost(self, user_id: str) -> dict:
        with self._lock:
            return dict(self._by_user.get(user_id, {"tokens": 0, "cost": 0.0, "runs": 0}))

    def recent_records(self, agent_id: str = None, user_id: str = None,
                       limit: int = 100) -> List[dict]:
        with self._lock:
            records = self._records
            if agent_id:
                records = [r for r in records if r.agent_id == agent_id]
            if user_id:
                records = [r for r in records if r.user_id == user_id]
            return [r.to_dict() for r in reversed(records[-limit:])]

    # ── Budgets ──

    def set_budget(self, scope: str, daily_limit: float = None,
                   monthly_limit: float = None) -> dict:
        """Set a budget for an agent_id or '_fleet'."""
        b = self._budgets.setdefault(scope, {})
        if daily_limit is not None:
            b["daily_limit"] = daily_limit
        if monthly_limit is not None:
            b["monthly_limit"] = monthly_limit
        return {"ok": True, "scope": scope, "budget": b}

    def get_budget(self, scope: str) -> dict:
        return self._budgets.get(scope, {})

    def budget_status(self, scope: str = "_fleet") -> dict:
        """Check spend against budget for an agent or the fleet."""
        budget = self._budgets.get(scope, {})
        today = date.today().isoformat()
        month_prefix = today[:7]

        if scope == "_fleet":
            with self._lock:
                today_spend = self._by_day.get(today, {}).get("cost", 0.0)
                month_spend = sum(v["cost"] for k, v in self._by_day.items()
                                  if k.startswith(month_prefix))
        else:
            with self._lock:
                recs = [r for r in self._records if r.agent_id == scope]
            today_spend = sum(r.cost for r in recs
                              if date.fromtimestamp(r.timestamp).isoformat() == today)
            month_spend = sum(r.cost for r in recs
                              if date.fromtimestamp(r.timestamp).isoformat().startswith(month_prefix))

        daily_limit = budget.get("daily_limit")
        monthly_limit = budget.get("monthly_limit")
        return {
            "scope": scope,
            "today_spend": round(today_spend, 4),
            "month_spend": round(month_spend, 4),
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "daily_pct": round(today_spend / daily_limit * 100, 1) if daily_limit else None,
            "monthly_pct": round(month_spend / monthly_limit * 100, 1) if monthly_limit else None,
            "daily_exceeded": bool(daily_limit and today_spend >= daily_limit),
            "monthly_exceeded": bool(monthly_limit and month_spend >= monthly_limit),
        }

    def all_budget_statuses(self) -> List[dict]:
        return [self.budget_status(s) for s in list(self._budgets.keys())]

    def dashboard(self) -> dict:
        """Full usage dashboard payload."""
        return {
            "totals": self.totals(),
            "by_agent": self.summary_by_agent(20),
            "by_user": self.summary_by_user(20),
            "by_provider": self.summary_by_provider(),
            "by_model": self.summary_by_model(),
            "daily_series": self.daily_series(30),
            "budgets": self.all_budget_statuses(),
        }


# Singleton
usage_tracker = UsageTracker()
