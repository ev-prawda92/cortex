#!/usr/bin/env python3
"""
Editorial verification agent — a real agent loop.

Given a claim, this agent decides what to look up, searches the live web, reads
sources, and either:
  * CONCLUDES with a grounded verdict + citations, or
  * ESCALATES to a human editor when it can't ground the claim.

It is a genuine agent, not a pipeline: the model chooses each next action based
on what the previous action returned. The loop is think -> act -> observe ->
think again, until a stopping condition is met.

The stopping conditions are governed by CORTEX CONFIG — the same config the
Cortex control plane edits. Change the config in Cortex, and this agent's
behavior actually changes:

  journey.max_retries              -> how many investigation steps it may take
  escalation.confidence_threshold  -> below this confidence it must escalate
  graph.confirm_then_act           -> if true, never auto-publish; always hold
  escalation.route_to              -> who it escalates to

Run standalone:
    export ANTHROPIC_API_KEY=sk-...
    python3 agent.py "The Washington Post was founded in 1877"

Requires: httpx  (pip install httpx)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
API_URL = "https://api.anthropic.com/v1/messages"


# ─────────────────────────────────────────────────────────────── default config
# Mirrors the shape Cortex stores, so a Cortex config drops straight in.
DEFAULT_CONFIG = {
    "posture": "augment",
    "journey": {"max_retries": 6},
    "escalation": {
        "confidence_threshold": 0.75,
        "route_to": "standards editor",
    },
    "graph": {"confirm_then_act": True},
}


# ───────────────────────────────────────────────────────────────────── the tools
# Each tool is a real function with real side effects (network calls). The model
# does not get to invent results — it can only see what these actually return.

def tool_web_search(query: str, max_results: int = 5) -> dict:
    """Live web search via DuckDuckGo's HTML endpoint (no API key needed)."""
    try:
        r = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (editorial-verification-agent)"},
            timeout=15,
            follow_redirects=True,
        )
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return {"error": f"search failed: {e}", "results": []}

    results = []
    # DDG html results: <a class="result__a" href="URL">TITLE</a> ... snippet
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S
    ):
        url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        url = re.sub(r"^//duckduckgo\.com/l/\?uddg=", "", url)
        try:
            from urllib.parse import unquote
            url = unquote(url.split("&rut=")[0])
        except Exception:
            pass
        if url.startswith("http"):
            results.append({"title": title, "url": url})
        if len(results) >= max_results:
            break

    snippets = [
        re.sub(r"<[^>]+>", "", s).strip()
        for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    ]
    for i, s in enumerate(snippets[: len(results)]):
        results[i]["snippet"] = s[:300]

    if not results:
        return {"error": "no results parsed", "results": []}
    return {"results": results}


def tool_fetch_page(url: str, max_chars: int = 4000) -> dict:
    """Fetch a page and return its readable text, truncated."""
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (editorial-verification-agent)"},
            timeout=20,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        return {"error": f"fetch failed: {e}"}

    text = r.text
    text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    truncated = len(text) > max_chars
    return {"url": url, "text": text[:max_chars], "truncated": truncated}


TOOL_SCHEMA = [
    {
        "name": "web_search",
        "description": (
            "Search the live web for sources relevant to the claim. Returns titles, "
            "URLs and snippets. Use this first, and again to check a different angle "
            "or find a second independent source."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Fetch the readable text of a specific URL found via web_search. Use this "
            "to read a source directly rather than relying on a snippet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL to fetch."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "conclude",
        "description": (
            "End the investigation with a grounded verdict. Only use this when the "
            "claim is genuinely supported or contradicted by sources you actually "
            "retrieved. Every citation must be a URL you actually fetched or saw in "
            "search results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["SUPPORTED", "CONTRADICTED", "PARTIALLY_SUPPORTED"],
                },
                "summary": {
                    "type": "string",
                    "description": "What the sources actually establish, in 1-3 sentences.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0. How confident you are, given the sources retrieved.",
                },
                "citations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs actually retrieved that support this verdict.",
                },
            },
            "required": ["verdict", "summary", "confidence", "citations"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Hand off to a human editor. Use this when the claim cannot be grounded "
            "in retrievable sources, sources conflict irreconcilably, the claim is a "
            "matter of opinion or prediction rather than fact, or you would otherwise "
            "have to guess. Escalating is always better than publishing something "
            "unsupported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why a human is needed."},
                "what_was_found": {
                    "type": "string",
                    "description": "What you did establish, so the human isn't starting cold.",
                },
            },
            "required": ["reason", "what_was_found"],
        },
    },
]

TOOL_IMPL: dict[str, Callable[..., dict]] = {
    "web_search": lambda **kw: tool_web_search(kw.get("query", "")),
    "fetch_page": lambda **kw: tool_fetch_page(kw.get("url", "")),
}


def _system_prompt(cfg: dict) -> str:
    thresh = cfg["escalation"]["confidence_threshold"]
    route = cfg["escalation"].get("route_to", "an editor")
    steps = cfg["journey"]["max_retries"]
    return f"""You are an editorial verification agent for a newsroom. You are given a claim. Your job is to establish whether it is supported by real, retrievable sources — or to escalate to a human.

The standard you are held to: a reader-facing claim that is not grounded in a source you actually retrieved is never publishable. You do not have permission to rely on your own memory or prior knowledge as evidence. If you did not retrieve it in this session, it is not grounded.

How to work:
- Start with web_search to find sources.
- Use fetch_page to read a promising source directly rather than trusting a snippet.
- Prefer primary and authoritative sources. Seek a second independent source for anything consequential.
- You have at most {steps} tool-using steps. Spend them well.

How to stop — you MUST end by calling either conclude or escalate:
- conclude: only if sources you actually retrieved establish the answer. Cite the real URLs.
- escalate: if you cannot ground it, sources conflict, the claim is opinion/prediction, or you'd be guessing. Escalation routes to {route}.

Calibrate confidence honestly. Confidence below {thresh} will be held for human review regardless of your verdict, so there is no benefit to inflating it. An honest low number is more useful than a confident wrong answer."""


# ───────────────────────────────────────────────────────────────────── the loop
@dataclass
class AgentRun:
    claim: str
    trace: list = field(default_factory=list)
    outcome: str = "INCOMPLETE"      # SUPPORTED / CONTRADICTED / ... / ESCALATED / HELD
    published: bool = False
    detail: dict = field(default_factory=dict)
    steps_used: int = 0
    config_version: str = ""
    error: str = ""

    def to_dict(self):
        return {
            "claim": self.claim, "outcome": self.outcome, "published": self.published,
            "detail": self.detail, "steps_used": self.steps_used,
            "trace": self.trace, "error": self.error,
            "at": datetime.now(timezone.utc).isoformat(),
        }


def run_agent(claim: str, config: dict | None = None, verbose: bool = True) -> AgentRun:
    """
    The agent loop. The model decides each next action from what the last action
    returned; we execute tools and feed the results back. Config governs the
    stopping conditions.
    """
    cfg = config or DEFAULT_CONFIG
    run = AgentRun(claim=claim)

    if not API_KEY:
        run.error = "ANTHROPIC_API_KEY not set — the agent needs a model to reason with."
        run.outcome = "ERROR"
        return run

    max_steps = int(cfg["journey"]["max_retries"])
    threshold = float(cfg["escalation"]["confidence_threshold"])
    hold_always = bool(cfg["graph"].get("confirm_then_act", True))
    route_to = cfg["escalation"].get("route_to", "an editor")

    messages = [{"role": "user", "content": f"Verify this claim:\n\n{claim}"}]
    client = httpx.Client(timeout=120)
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    def log(kind: str, **data):
        entry = {"step": run.steps_used, "kind": kind, **data}
        run.trace.append(entry)
        if verbose:
            _print_step(entry)

    for _ in range(max_steps):
        body = {
            "model": MODEL,
            "max_tokens": 2000,
            "system": _system_prompt(cfg),
            "tools": TOOL_SCHEMA,
            "messages": messages,
        }
        try:
            resp = client.post(API_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            run.error = f"model call failed: {e}"
            run.outcome = "ERROR"
            return run

        content = data.get("content", [])
        messages.append({"role": "assistant", "content": content})

        thinking = " ".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        ).strip()
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        if thinking:
            log("think", text=thinking)

        if not tool_uses:
            # Model stopped without a terminal tool — treat as ungrounded.
            run.outcome = "ESCALATED"
            run.detail = {
                "reason": "Agent ended without calling conclude or escalate.",
                "what_was_found": thinking[:500],
                "route_to": route_to,
            }
            log("escalate", **run.detail)
            return run

        results = []
        for tu in tool_uses:
            name, args, tid = tu.get("name"), tu.get("input", {}), tu.get("id")

            # ── terminal tools ──
            if name == "conclude":
                conf = float(args.get("confidence", 0.0))
                cites = args.get("citations", []) or []
                run.steps_used += 1
                log("conclude", verdict=args.get("verdict"), confidence=conf,
                    summary=args.get("summary"), citations=cites)

                # ── the gate: config decides whether this can publish ──
                gate_reasons = []
                if conf < threshold:
                    gate_reasons.append(
                        f"confidence {conf:.2f} is below the {threshold:.2f} threshold")
                if not cites:
                    gate_reasons.append("no citations were provided")
                if hold_always:
                    gate_reasons.append("confirm-then-act is ON — nothing publishes unreviewed")

                run.detail = {
                    "verdict": args.get("verdict"),
                    "summary": args.get("summary"),
                    "confidence": conf,
                    "citations": cites,
                    "route_to": route_to,
                    "gate_reasons": gate_reasons,
                }
                if gate_reasons:
                    run.outcome = "HELD"
                    run.published = False
                    log("gate", decision="HELD", reasons=gate_reasons, route_to=route_to)
                else:
                    run.outcome = args.get("verdict", "SUPPORTED")
                    run.published = True
                    log("gate", decision="CLEARED",
                        reasons=["confidence above threshold, citations present, "
                                 "confirm-then-act off"])
                return run

            if name == "escalate":
                run.steps_used += 1
                run.outcome = "ESCALATED"
                run.detail = {**args, "route_to": route_to}
                log("escalate", **run.detail)
                return run

            # ── working tools ──
            run.steps_used += 1
            log("act", tool=name, args=args)
            impl = TOOL_IMPL.get(name)
            out = impl(**args) if impl else {"error": f"unknown tool {name}"}
            log("observe", tool=name, result=_summarize_obs(name, out))
            results.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": json.dumps(out)[:6000],
            })
            time.sleep(0.3)

        if results:
            messages.append({"role": "user", "content": results})

    # Ran out of steps without concluding.
    run.outcome = "HELD"
    run.detail = {
        "reason": f"Step budget ({max_steps}) exhausted before the claim was grounded.",
        "route_to": route_to,
        "gate_reasons": ["investigation incomplete — no grounded verdict reached"],
    }
    log("gate", decision="HELD", reasons=[run.detail["reason"]], route_to=route_to)
    return run


# ───────────────────────────────────────────────────────────────────── printing
def _summarize_obs(tool: str, out: dict) -> str:
    if out.get("error"):
        return f"error: {out['error']}"
    if tool == "web_search":
        rs = out.get("results", [])
        return f"{len(rs)} results: " + "; ".join(r["title"][:60] for r in rs[:3])
    if tool == "fetch_page":
        n = len(out.get("text", ""))
        return f"fetched {n} chars from {out.get('url','')[:70]}"
    return str(out)[:200]


C = {
    "think": "\033[90m", "act": "\033[36m", "observe": "\033[35m",
    "conclude": "\033[32m", "escalate": "\033[33m", "gate": "\033[1m",
    "reset": "\033[0m", "dim": "\033[2m",
}


def _print_step(e: dict):
    k = e["kind"]
    c = C.get(k, "")
    if k == "think":
        txt = e["text"]
        txt = (txt[:400] + "…") if len(txt) > 400 else txt
        print(f"{c}  think   {txt}{C['reset']}")
    elif k == "act":
        args = json.dumps(e["args"])[:120]
        print(f"{c}  act     {e['tool']}({args}){C['reset']}")
    elif k == "observe":
        print(f"{c}  observe {e['result']}{C['reset']}")
    elif k == "conclude":
        print(f"{c}  conclude {e['verdict']} · confidence {e['confidence']:.2f} · "
              f"{len(e['citations'])} citations{C['reset']}")
    elif k == "escalate":
        print(f"{c}  escalate {e.get('reason','')[:200]}{C['reset']}")
    elif k == "gate":
        print(f"\n{c}  GATE → {e['decision']}{C['reset']}")
        for r in e.get("reasons", []):
            print(f"          · {r}")


def print_result(run: AgentRun):
    print("\n" + "─" * 68)
    if run.error:
        print(f"  ERROR: {run.error}")
        return
    d = run.detail
    print(f"  OUTCOME: {run.outcome}   (published: {run.published})")
    if run.outcome in ("SUPPORTED", "CONTRADICTED", "PARTIALLY_SUPPORTED", "HELD"):
        if d.get("summary"):
            print(f"\n  {d['summary']}")
        if d.get("confidence") is not None:
            print(f"\n  confidence: {d['confidence']:.2f}")
        for c in d.get("citations", []):
            print(f"    · {c}")
    if run.outcome == "ESCALATED":
        print(f"\n  reason: {d.get('reason','')}")
        print(f"  found:  {d.get('what_was_found','')[:400]}")
    if d.get("route_to") and not run.published:
        print(f"\n  → routed to {d['route_to']} for human review")
    print(f"\n  {run.steps_used} steps used")
    print("─" * 68)


def main():
    claim = " ".join(sys.argv[1:]).strip()
    if not claim:
        print("usage: python3 agent.py \"<claim to verify>\"")
        sys.exit(1)
    if not API_KEY:
        print("Set ANTHROPIC_API_KEY first:  export ANTHROPIC_API_KEY=sk-...")
        sys.exit(1)

    print("═" * 68)
    print("  EDITORIAL VERIFICATION AGENT")
    print(f"  claim: {claim}")
    cfg = DEFAULT_CONFIG
    print(f"  config: max {cfg['journey']['max_retries']} steps · "
          f"threshold {cfg['escalation']['confidence_threshold']} · "
          f"confirm-then-act {'ON' if cfg['graph']['confirm_then_act'] else 'OFF'}")
    print("═" * 68 + "\n")

    run = run_agent(claim, cfg)
    print_result(run)


if __name__ == "__main__":
    main()
