"""Anthropic and OpenAI HTTP adapters for a member's own API key.

Both are stateless: every call resends the conversation, so there is no provider
session to resume. Keys travel only in a request header and never reach a log,
a result or an error message; failures are reduced to the normalized codes.
"""

from __future__ import annotations

import json

from django.conf import settings

from .ai_http import HarnessHttpError, json_request
from .ai_types import (
    ANTHROPIC_API,
    API_KINDS,
    AUTHORIZATION_REQUIRED,
    LIMIT_REACHED,
    OPENAI_API,
    PROVIDER_ERROR,
    UNAVAILABLE,
    ProviderResult,
    Usage,
)

# Allow-lists checked against each provider's model docs on 2026-10-05. The first
# entry is the chat default; BACKGROUND_DEFAULT is the cheaper background default.
MODELS = {
    ANTHROPIC_API: ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5-20251001", "claude-fable-5-1"),
    OPENAI_API: ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"),
}
BACKGROUND_DEFAULT = {ANTHROPIC_API: "claude-haiku-4-5-20251001", OPENAI_API: "gpt-6-luna"}
LABELS = {ANTHROPIC_API: "Anthropic API", OPENAI_API: "OpenAI API"}
KEY_PREFIX = {ANTHROPIC_API: "sk-ant-", OPENAI_API: "sk-"}
ANTHROPIC_VERSION = "2023-06-01"
_QUOTA_ERRORS = frozenset({"insufficient_quota", "billing_error", "billing_not_active"})


def label(kind):
    return LABELS.get(kind, kind)


def default_model(kind, *, use_chat):
    return MODELS[kind][0] if use_chat else BACKGROUND_DEFAULT[kind]


def model_for(kind, requested, *, use_chat):
    """The requested model when allow-listed, else the feature default."""
    requested = (requested or "").strip()
    return requested if requested in MODELS[kind] else default_model(kind, use_chat=use_chat)


def looks_like_key(kind, secret):
    # The key goes into an HTTP header, so a bad paste must be refused up front.
    return (
        secret.startswith(KEY_PREFIX[kind])
        and len(secret) >= 20
        and secret.isascii()
        and secret.isprintable()
        and not any(ch.isspace() for ch in secret)
    )


def failure_from_api_http(exc: HarnessHttpError) -> str:
    if exc.status in {401, 403}:
        return AUTHORIZATION_REQUIRED
    if exc.status in {402, 429}:
        return LIMIT_REACHED
    payload = exc.payload or {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if str(error.get("type") or "") in _QUOTA_ERRORS or str(error.get("code") or "") in _QUOTA_ERRORS:
        return LIMIT_REACHED
    if exc.status == 400 and "credit balance" in str(error.get("message") or "").lower():
        return LIMIT_REACHED
    if exc.status == 0 or exc.status >= 500:
        return UNAVAILABLE
    return PROVIDER_ERROR


class _BadReply(Exception):
    pass


def run_api(kind, key, *, model, prompt, system="", history=(), tools=(), tool_runner=None):
    """One request, or a tool loop, against the provider. Returns a ProviderResult."""
    if kind not in API_KINDS:
        return ProviderResult(ok=False, failure_code=PROVIDER_ERROR)
    adapter = _anthropic if kind == ANTHROPIC_API else _openai
    totals = [0, 0]
    seen = [False]
    try:
        answer = adapter(key, model, prompt, system, history, tools, tool_runner, totals, seen)
    except HarnessHttpError as exc:
        return ProviderResult(ok=False, failure_code=failure_from_api_http(exc), usage=_usage(totals, seen))
    except _BadReply:
        return ProviderResult(ok=False, failure_code=PROVIDER_ERROR, usage=_usage(totals, seen))
    return ProviderResult(ok=True, answer=answer, usage=_usage(totals, seen))


def _usage(totals, seen):
    if not seen[0]:
        return Usage()
    return Usage(prompt_tokens=totals[0], completion_tokens=totals[1])


def _add_usage(totals, seen, stats, prompt_key, completion_key):
    stats = stats if isinstance(stats, dict) else {}
    seen[0] = True
    totals[0] += _count(stats.get(prompt_key))
    totals[1] += _count(stats.get(completion_key))


def _count(value):
    return value if isinstance(value, int) and value > 0 else 0


def _max_rounds():
    return max(1, int(getattr(settings, "AI_API_MAX_ROUNDS", 12)))


def _max_output():
    return max(256, int(getattr(settings, "AI_API_MAX_OUTPUT_TOKENS", 8192)))


def _timeout():
    return max(5, int(getattr(settings, "AI_API_TIMEOUT_SECONDS", 120)))


def _base(setting, default):
    return str(getattr(settings, setting, default) or default).rstrip("/")


def _anthropic(key, model, prompt, system, history, tools, tool_runner, totals, seen):
    url = _base("AI_ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    messages = [{"role": item["role"], "content": item["content"]} for item in history]
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "max_tokens": _max_output(), "messages": messages}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [
            {"name": spec.name, "description": spec.description, "input_schema": spec.parameters}
            for spec in tools
        ]
    for _round in range(_max_rounds()):
        reply = json_request(url, token=None, method="POST", body=body, timeout=_timeout(), headers=headers)
        if not isinstance(reply, dict) or not isinstance(reply.get("content"), list):
            raise _BadReply
        _add_usage(totals, seen, reply.get("usage"), "input_tokens", "output_tokens")
        blocks = [item for item in reply["content"] if isinstance(item, dict)]
        calls = [item for item in blocks if item.get("type") == "tool_use"]
        if not calls or tool_runner is None:
            return "".join(str(item.get("text") or "") for item in blocks if item.get("type") == "text")
        messages.append({"role": "assistant", "content": blocks})
        results = []
        for call in calls:
            args = call.get("input") if isinstance(call.get("input"), dict) else {}
            output, ok = tool_runner(str(call.get("name") or ""), args)
            results.append(
                {"type": "tool_result", "tool_use_id": call.get("id"), "content": output, "is_error": not ok}
            )
        messages.append({"role": "user", "content": results})
    raise _BadReply


def _openai(key, model, prompt, system, history, tools, tool_runner, totals, seen):
    url = _base("AI_OPENAI_BASE_URL", "https://api.openai.com") + "/v1/responses"
    headers = {"Authorization": f"Bearer {key}"}
    items = [{"role": item["role"], "content": item["content"]} for item in history]
    items.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "input": items,
        "store": False,
        "max_output_tokens": _max_output(),
        "include": ["reasoning.encrypted_content"],
    }
    if system:
        body["instructions"] = system
    if tools:
        body["tools"] = [
            {"type": "function", "name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in tools
        ]
    for _round in range(_max_rounds()):
        reply = json_request(url, token=None, method="POST", body=body, timeout=_timeout(), headers=headers)
        if not isinstance(reply, dict) or not isinstance(reply.get("output"), list):
            raise _BadReply
        _add_usage(totals, seen, reply.get("usage"), "input_tokens", "output_tokens")
        output = [item for item in reply["output"] if isinstance(item, dict)]
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls or tool_runner is None:
            return "".join(
                str(part.get("text") or "")
                for item in output
                if item.get("type") == "message"
                for part in (item.get("content") or ())
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
        items.extend(output)
        for call in calls:
            text, _ok = tool_runner(str(call.get("name") or ""), _arguments(call.get("arguments")))
            items.append({"type": "function_call_output", "call_id": call.get("call_id"), "output": text})
    raise _BadReply


def _arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
