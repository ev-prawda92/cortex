#!/usr/bin/env python3
"""
Multi-provider tool-calling engine for Cortex agents.

One function — run_tool_loop() — executes a real agentic loop (think -> act ->
observe -> repeat) against any supported provider:

    anthropic  : /v1/messages            (native tool use)
    openai     : /v1/chat/completions    (function calling)
    gemini     : /v1beta ... generateContent (functionDeclarations)
    xai        : /v1/chat/completions    (OpenAI-compatible, Grok models)
    perplexity : /chat/completions       (OpenAI-compatible)
    mistral    : /v1/chat/completions    (OpenAI-compatible w/ tool support)
    cohere     : /v2/chat                (native tool use)
    meta       : /v1/chat/completions    (via Together AI, OpenAI-compatible)

Tools are declared once, in Anthropic format ({name, description, input_schema}),
and converted per provider. The trace it returns matches the Cortex UI's step
kinds: think / act / observe / conclude / escalate.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.6-terra",
    "gemini": "gemini-3.1-pro",
    "xai": "grok-3",
    "perplexity": "sonar-pro",
    "mistral": "mistral-large-latest",
    "cohere": "command-r-plus",
    "meta": "meta-llama/Llama-4-Maverick-17B-128E-Instruct-Turbo",
}

PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "xai": "xAI (Grok)",
    "perplexity": "Perplexity",
    "mistral": "Mistral AI",
    "cohere": "Cohere",
    "meta": "Meta (via Together)",
}

ALL_PROVIDERS = list(PROVIDER_LABELS.keys())


# ────────────────────────────────────────────────────────── schema conversion

def _tools_openai(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in tools]


def _strip_gemini(schema):
    """Gemini's OpenAPI subset rejects some JSON-schema keys."""
    if isinstance(schema, dict):
        return {k: _strip_gemini(v) for k, v in schema.items()
                if k not in ("additionalProperties", "$schema", "default")}
    if isinstance(schema, list):
        return [_strip_gemini(x) for x in schema]
    return schema


def _tools_gemini(tools: list[dict]) -> list[dict]:
    return [{"functionDeclarations": [
        {"name": t["name"], "description": t.get("description", ""),
         "parameters": _strip_gemini(t.get("input_schema", {"type": "object", "properties": {}}))}
        for t in tools]}]


def _tools_cohere(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tools to Cohere v2 format."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in tools]


# ────────────────────────────────────────────────── API base URLs

_API_BASES = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "xai": "https://api.x.ai",
    "perplexity": "https://api.perplexity.ai",
    "mistral": "https://api.mistral.ai",
    "cohere": "https://api.cohere.com",
    "meta": "https://api.together.xyz",
}


# ────────────────────────────────────────────────────────── connection tests

def test_connection(provider: str, api_key: str, model: str) -> dict:
    """One tiny real call. Returns {ok, message}."""
    try:
        with httpx.Client(timeout=20) as c:
            if provider == "anthropic":
                r = c.post("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            elif provider == "openai":
                r = c.post("https://api.openai.com/v1/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            elif provider == "gemini":
                r = c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           params={"key": api_key},
                           json={"contents": [{"parts": [{"text": "ping"}]}],
                                 "generationConfig": {"maxOutputTokens": 8}})
            elif provider == "xai":
                r = c.post("https://api.x.ai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            elif provider == "perplexity":
                r = c.post("https://api.perplexity.ai/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            elif provider == "mistral":
                r = c.post("https://api.mistral.ai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            elif provider == "cohere":
                r = c.post("https://api.cohere.com/v2/chat",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            elif provider == "meta":
                r = c.post("https://api.together.xyz/v1/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
            else:
                return {"ok": False, "message": f"unknown provider: {provider}"}
        if r.status_code == 200:
            return {"ok": True, "message": f"Connected — {model} responded."}
        try:
            err = r.json().get("error", {})
            msg = err.get("message") or str(err)
        except Exception:
            msg = r.text[:200]
        return {"ok": False, "message": f"HTTP {r.status_code}: {msg[:300]}"}
    except Exception as e:
        return {"ok": False, "message": f"connection failed: {e}"}


# ────────────────────────────────────────────────────────── the agentic loop

def run_tool_loop(provider: str, api_key: str, model: str, system: str,
                  tools: list[dict], user_message: str,
                  process_tool_call: Callable[[str, dict], str],
                  max_iterations: int = 10) -> dict:
    """
    Run a full agentic loop on the chosen provider.
    Returns {ok, final_text, trace, steps_used, escalated, error?}.
    """
    trace: list[dict] = []
    escalated = False

    def observe(name: str, args: dict) -> str:
        nonlocal escalated
        trace.append({"kind": "act", "tool": name, "args": args})
        result = process_tool_call(name, args)
        if "escalate" in name:
            escalated = True
            trace.append({"kind": "escalate", "reason": args.get("reason", json.dumps(args)[:200])})
        trace.append({"kind": "observe", "result": str(result)[:400]})
        return result

    try:
        if provider == "anthropic":
            final = _loop_anthropic(api_key, model, system, tools, user_message, observe, trace, max_iterations)
        elif provider == "openai":
            final = _loop_openai(api_key, model, system, tools, user_message, observe, trace, max_iterations)
        elif provider == "gemini":
            final = _loop_gemini(api_key, model, system, tools, user_message, observe, trace, max_iterations)
        elif provider in ("xai", "perplexity", "mistral", "meta"):
            # These providers use OpenAI-compatible API format
            final = _loop_openai_compat(provider, api_key, model, system, tools, user_message, observe, trace, max_iterations)
        elif provider == "cohere":
            final = _loop_cohere(api_key, model, system, tools, user_message, observe, trace, max_iterations)
        else:
            return {"ok": False, "error": f"unknown provider: {provider}", "trace": trace, "steps_used": 0, "escalated": False}
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("error", {}).get("message", e.response.text[:200])
        except Exception:
            msg = e.response.text[:200]
        return {"ok": False, "error": f"{PROVIDER_LABELS.get(provider, provider)} API error {e.response.status_code}: {msg}",
                "trace": trace, "steps_used": len([t for t in trace if t["kind"] == "act"]), "escalated": escalated}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": trace,
                "steps_used": len([t for t in trace if t["kind"] == "act"]), "escalated": escalated}

    return {"ok": True, "final_text": final, "trace": trace,
            "steps_used": len([t for t in trace if t["kind"] == "act"]), "escalated": escalated}


def _loop_anthropic(api_key, model, system, tools, user_message, observe, trace, max_iters):
    messages = [{"role": "user", "content": user_message}]
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    with httpx.Client(timeout=120) as client:
        for _ in range(max_iters):
            r = client.post("https://api.anthropic.com/v1/messages", headers=headers,
                            json={"model": model, "max_tokens": 1500, "system": system,
                                  "tools": tools, "messages": messages})
            r.raise_for_status()
            data = r.json()
            content = data.get("content", [])
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    trace.append({"kind": "think", "text": b["text"][:400]})
            if data.get("stop_reason") != "tool_use":
                return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            messages.append({"role": "assistant", "content": content})
            results = []
            for b in content:
                if b.get("type") == "tool_use":
                    out = observe(b["name"], b.get("input", {}))
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out})
            messages.append({"role": "user", "content": results})
    return "(max steps reached)"


def _loop_openai(api_key, model, system, tools, user_message, observe, trace, max_iters):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
    oa_tools = _tools_openai(tools)
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=120) as client:
        for _ in range(max_iters):
            r = client.post("https://api.openai.com/v1/chat/completions", headers=headers,
                            json={"model": model, "messages": messages, "tools": oa_tools})
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            if msg.get("content"):
                trace.append({"kind": "think", "text": msg["content"][:400]})
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or "(no response)"
            messages.append(msg)
            for tc in calls:
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = observe(tc["function"]["name"], args)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    return "(max steps reached)"


def _loop_openai_compat(provider, api_key, model, system, tools, user_message, observe, trace, max_iters):
    """Generic OpenAI-compatible loop for xAI (Grok), Perplexity, Mistral, Meta (Together)."""
    base_url = _API_BASES[provider]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
    oa_tools = _tools_openai(tools)
    headers = {"Authorization": f"Bearer {api_key}"}

    # Perplexity and some providers don't support tool use — run without tools
    supports_tools = provider in ("xai", "mistral", "meta")

    with httpx.Client(timeout=120) as client:
        for _ in range(max_iters):
            body = {"model": model, "messages": messages}
            if supports_tools and oa_tools:
                body["tools"] = oa_tools

            endpoint = f"{base_url}/v1/chat/completions"
            if provider == "perplexity":
                endpoint = f"{base_url}/chat/completions"

            r = client.post(endpoint, headers=headers, json=body)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            if msg.get("content"):
                trace.append({"kind": "think", "text": msg["content"][:400]})
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or "(no response)"
            messages.append(msg)
            for tc in calls:
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = observe(tc["function"]["name"], args)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    return "(max steps reached)"


def _loop_gemini(api_key, model, system, tools, user_message, observe, trace, max_iters):
    contents = [{"role": "user", "parts": [{"text": user_message}]}]
    gm_tools = _tools_gemini(tools)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    with httpx.Client(timeout=120) as client:
        for _ in range(max_iters):
            r = client.post(url, params={"key": api_key},
                            json={"contents": contents, "tools": gm_tools,
                                  "systemInstruction": {"parts": [{"text": system}]}})
            r.raise_for_status()
            cand = r.json().get("candidates", [{}])[0]
            parts = cand.get("content", {}).get("parts", [])
            fcalls = [p["functionCall"] for p in parts if "functionCall" in p]
            for p in parts:
                if p.get("text", "").strip():
                    trace.append({"kind": "think", "text": p["text"][:400]})
            if not fcalls:
                return "\n".join(p.get("text", "") for p in parts if "text" in p) or "(no response)"
            contents.append({"role": "model", "parts": parts})
            resp_parts = []
            for fc in fcalls:
                out = observe(fc["name"], fc.get("args", {}))
                try:
                    payload = json.loads(out)
                except (json.JSONDecodeError, TypeError):
                    payload = {"result": str(out)}
                resp_parts.append({"functionResponse": {"name": fc["name"], "response": {"content": payload}}})
            contents.append({"role": "user", "parts": resp_parts})
    return "(max steps reached)"


def _loop_cohere(api_key, model, system, tools, user_message, observe, trace, max_iters):
    """Cohere v2 chat API with tool use."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
    co_tools = _tools_cohere(tools)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    with httpx.Client(timeout=120) as client:
        for _ in range(max_iters):
            body = {"model": model, "messages": messages}
            if co_tools:
                body["tools"] = co_tools

            r = client.post("https://api.cohere.com/v2/chat", headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})
            content_parts = msg.get("content", [])

            # Extract text content
            text = ""
            for part in content_parts:
                if part.get("type") == "text":
                    text += part.get("text", "")
            if text.strip():
                trace.append({"kind": "think", "text": text[:400]})

            # Check for tool calls
            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                return text or "(no response)"

            messages.append({"role": "assistant", "tool_calls": tool_calls, "content": content_parts})
            for tc in tool_calls:
                func = tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = observe(func.get("name", ""), args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out})
    return "(max steps reached)"
