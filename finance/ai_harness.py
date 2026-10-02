"""Agent Harness App API adapter. Talks only over HTTP; never imports harness code."""

from __future__ import annotations

import time
from urllib.parse import urljoin

from django.conf import settings

from .ai_http import HarnessHttpError, json_request
from .ai_types import (
    AUTHORIZATION_REQUIRED,
    HOSTED_BACKENDS,
    LIMIT_REACHED,
    LOCAL_BACKEND,
    PROVIDER_ERROR,
    UNAVAILABLE,
    BackendInfo,
    ModelStatus,
    ProviderResult,
    Usage,
    map_harness_failure,
)

HOSTED_UNAVAILABLE_REASON = "Hosted backends stay unavailable until Agent Harness app-tools-only sessions land."
_POLL_INITIAL_DELAY_SECONDS = 0.5
_POLL_MAX_DELAY_SECONDS = 5.0
_DEFAULT_SESSION_TIMEOUT_SECONDS = 600


def hosted_sessions_enabled():
    return bool(getattr(settings, "AGENT_HARNESS_HOSTED_SESSIONS", False))


def discover(base_url, token):
    root = json_request(urljoin(base_url + "/", "api/v1"), token=token)
    backends = json_request(urljoin(base_url + "/", "api/v1/backends"), token=token)
    projects = []
    try:
        projects = json_request(urljoin(base_url + "/", "api/v1/projects"), token=token)
    except HarnessHttpError:
        projects = root.get("projects") or []
    return root, backends, projects


def list_backends(base_url, token):
    _root, backends, _projects = discover(base_url, token)
    return [describe_backend(item) for item in _as_list(backends)]


def describe_backend(item):
    name = str(item.get("name") or "")
    policy = item.get("provider_policy") or {}
    policy_allowed = True
    if isinstance(policy, dict) and "allowed" in policy:
        policy_allowed = bool(policy.get("allowed"))
    logged_in = bool(item.get("logged_in", item.get("available", False)))
    harness_available = bool(item.get("available")) and policy_allowed and logged_in
    hosted = name in HOSTED_BACKENDS
    selectable = harness_available and (not hosted or hosted_sessions_enabled())
    if hosted and harness_available and not hosted_sessions_enabled():
        status = HOSTED_UNAVAILABLE_REASON
    elif not harness_available:
        status = str(item.get("notice") or "Unavailable")
    else:
        status = str(item.get("notice") or "Available")
    slow = name == LOCAL_BACKEND
    return BackendInfo(
        id=name,
        label=_label(name),
        available=selectable,
        logged_in=logged_in,
        supports_structured=True,
        supports_conversation=True,
        suits_live=True,
        suits_background=True,
        slow_to_start=slow,
        status=status,
        model=str(item.get("model") or ""),
    )


def default_backends(backends):
    available = [item for item in backends if item.available]
    hosted = [item for item in available if item.id in HOSTED_BACKENDS]
    local = next((item for item in available if item.id == LOCAL_BACKEND), None)
    chat = hosted[0].id if hosted else (local.id if local else "")
    background = local.id if local else (chat if chat else "")
    return chat, background


def model_status(base_url, token):
    payload = json_request(urljoin(base_url + "/", "api/v1/models/status"), token=token)
    rows = _as_list(payload)
    return [
        ModelStatus(
            name=str(item.get("name") or ""),
            state=str(item.get("state") or "unreachable"),
            waking_seconds=int(item.get("waking_seconds") or 0),
        )
        for item in rows
    ]


def local_model_ready(statuses):
    local = [item for item in statuses if item.name]
    if not local:
        return False
    return any(item.state == "ready" for item in local)


def warm_local_model(base_url, token):
    try:
        json_request(
            urljoin(base_url + "/", "api/v1/models/warm"),
            token=token,
            method="POST",
            body={},
        )
    except HarnessHttpError as exc:
        if exc.status in {401, 403}:
            return model_status(base_url, token)
        raise
    return model_status(base_url, token)


def run_session(
    base_url,
    token,
    *,
    prompt,
    backend,
    project,
    model="",
    tools=None,
    tool_runner=None,
    on_session=None,
    sleep=None,
    monotonic=None,
):
    payload = {
        "prompt": prompt,
        "backend": backend,
        "project": project,
        "tools": [_tool_payload(spec) for spec in (tools or ())],
    }
    if model:
        payload["model"] = model
    created = json_request(
        urljoin(base_url + "/", "api/v1/sessions"),
        token=token,
        method="POST",
        body=payload,
    )
    session_id = str(created.get("id") or "")
    if not session_id:
        return _failed(created)
    if on_session is not None:
        on_session(session_id)
    return wait_for_session(
        base_url,
        token,
        session_id,
        created,
        tool_runner=tool_runner,
        sleep=sleep,
        monotonic=monotonic,
    )


def wait_for_session(
    base_url,
    token,
    session_id,
    session=None,
    *,
    tool_runner=None,
    sleep=None,
    monotonic=None,
):
    sleeper = time.sleep if sleep is None else sleep
    clock = time.monotonic if monotonic is None else monotonic
    timeout = int(getattr(settings, "AGENT_HARNESS_SESSION_TIMEOUT_SECONDS", _DEFAULT_SESSION_TIMEOUT_SECONDS))
    deadline = clock() + timeout
    delay = _POLL_INITIAL_DELAY_SECONDS
    path = f"api/v1/sessions/{session_id}"
    current = session if session is not None else json_request(urljoin(base_url + "/", path), token=token)
    while True:
        status = str(current.get("status") or "")
        if status in {"done", "failed", "cancelled"}:
            return _result_from_session(current)
        if status == "waiting_app" and tool_runner is not None:
            if _answer_tool_calls(base_url, token, session_id, tool_runner):
                delay = _POLL_INITIAL_DELAY_SECONDS
        if clock() >= deadline:
            return ProviderResult(ok=False, failure_code=UNAVAILABLE, session_id=session_id)
        sleeper(delay)
        delay = min(delay * 2, _POLL_MAX_DELAY_SECONDS)
        current = json_request(urljoin(base_url + "/", path), token=token)


def _answer_tool_calls(base_url, token, session_id, tool_runner):
    pending = _as_list(
        json_request(
            urljoin(base_url + "/", f"api/v1/sessions/{session_id}/tool_calls?status=pending"),
            token=token,
        )
    )
    for call in pending:
        call_id = str(call.get("call_id") or call.get("id") or "")
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        output, ok = tool_runner(name, args)
        json_request(
            urljoin(base_url + "/", f"api/v1/sessions/{session_id}/tool_calls/{call_id}"),
            token=token,
            method="POST",
            body={"output": output, "ok": ok},
        )
    return len(pending)


def _result_from_session(session):
    session_id = str(session.get("id") or "")
    usage = Usage(
        prompt_tokens=_int_or_none(session.get("prompt_tokens")),
        completion_tokens=_int_or_none(session.get("completion_tokens")),
    )
    if str(session.get("status") or "") == "done":
        return ProviderResult(
            ok=True,
            answer=str(session.get("answer") or ""),
            usage=usage,
            session_id=session_id,
        )
    return _failed(session, usage=usage, session_id=session_id)


def _failed(session, usage=None, session_id=None):
    failure = session.get("failure") if isinstance(session.get("failure"), dict) else {}
    code = map_harness_failure(failure.get("code"))
    return ProviderResult(
        ok=False,
        failure_code=code or PROVIDER_ERROR,
        usage=usage or Usage(),
        session_id=session_id or str(session.get("id") or ""),
    )


def failure_from_http(exc: HarnessHttpError) -> str:
    if exc.status in {401, 403}:
        return AUTHORIZATION_REQUIRED
    if exc.status == 429:
        return LIMIT_REACHED
    payload = exc.payload
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error.get("code"):
        return map_harness_failure(error.get("code"))
    failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    if failure.get("code"):
        return map_harness_failure(failure.get("code"))
    if exc.status == 0:
        return UNAVAILABLE
    return PROVIDER_ERROR


def _tool_payload(spec):
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def _label(name):
    labels = {
        LOCAL_BACKEND: "Local model",
        "claude": "Claude",
        "codex": "Codex",
        "cursor": "Cursor",
    }
    return labels.get(name, name or "Unknown backend")


def _as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("backends", "items", "projects", "tool_calls"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
