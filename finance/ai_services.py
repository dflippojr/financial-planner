"""Per-member AI connections and a backend-neutral request surface."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .ai_harness import (
    default_backends,
    describe_backend,
    discover,
    failure_from_http,
    list_backends,
    model_status,
    run_session,
    wait_for_session,
    warm_local_model,
)
from .ai_http import HarnessHttpError
from .ai_types import AUTHORIZATION_REQUIRED, LOCAL_BACKEND, PROVIDER_ERROR, ProviderResult, Usage
from .ai_urls import HarnessUrlError, parse_harness_url
from .encryption import decrypt_secret, encrypt_secret
from .lifecycle_services import _DENIED, _person_for
from .models import AiProviderConnection, AiUsageEvent
from .policy_services import household_ai_allowed, may_use_ai
from .category_services import current_household

TOKEN_PREFIX = "ha-"
CONNECT_DENIED = "AI backends cannot be connected until the privacy and data policy is accepted."


class AiError(Exception):
    def __init__(self, message, failure_code=PROVIDER_ERROR):
        self.failure_code = failure_code
        super().__init__(message)


def connection_for(principal):
    person = _person_for(principal)
    return AiProviderConnection.objects.owned_by(person).first()


def member_has_ai(principal):
    try:
        person = _person_for(principal)
    except PermissionDenied:
        return False
    return may_use_ai(person) and connection_for(person) is not None


def connect_harness(principal, *, base_url, token):
    person = _person_for(principal)
    if not may_use_ai(person):
        raise AiError(CONNECT_DENIED, AUTHORIZATION_REQUIRED)
    url = parse_harness_url(base_url)
    secret = (token or "").strip()
    if not secret.startswith(TOKEN_PREFIX):
        raise AiError("Paste an Agent Harness App token that starts with ha-.")
    try:
        _root, backends, projects = discover(url, secret)
    except HarnessHttpError as exc:
        raise AiError("That Agent Harness token could not be used.", failure_from_http(exc)) from None
    infos = [describe_backend(item) for item in _backend_items(backends)]
    chat, background = default_backends(infos)
    project = _choose_project(projects)
    encrypted = encrypt_secret(secret)
    connection, _created = AiProviderConnection.objects.update_or_create(
        owner=person,
        kind=AiProviderConnection.Kind.AGENT_HARNESS,
        defaults={
            "base_url": url,
            "encrypted_token": encrypted,
            "harness_project": project,
            "chat_backend": chat,
            "background_backend": background,
            "chat_model": "",
            "background_model": "",
            "connected_at": timezone.now(),
            "last_status": "",
        },
    )
    return connection


def disconnect_harness(principal):
    person = _person_for(principal)
    AiProviderConnection.objects.owned_by(person).delete()


def set_defaults(principal, *, chat_backend, background_backend, chat_model="", background_model=""):
    person = _person_for(principal)
    connection = connection_for(person)
    if connection is None:
        raise AiError("Connect an AI backend first.")
    backends = {item.id: item for item in discovered_backends(person)}
    connection.chat_backend = _require_selectable(backends, chat_backend, live=True)
    connection.background_backend = _require_selectable(backends, background_backend, live=False)
    connection.chat_model = (chat_model or "").strip()
    connection.background_model = (background_model or "").strip()
    connection.save(
        update_fields=(
            "chat_backend",
            "background_backend",
            "chat_model",
            "background_model",
        )
    )
    return connection


def discovered_backends(principal):
    person = _person_for(principal)
    connection = _own_connection(person)
    token = _token(connection)
    try:
        infos = list_backends(connection.base_url, token)
        connection.last_status = ""
        connection.save(update_fields=("last_status",))
        return infos
    except HarnessHttpError as exc:
        code = failure_from_http(exc)
        connection.last_status = code
        connection.save(update_fields=("last_status",))
        if code == AUTHORIZATION_REQUIRED:
            raise AiError("Reconnect Agent Harness. The saved token is no longer valid.", code) from None
        raise AiError("The AI provider list could not be loaded.", code) from None


def local_status(principal):
    person = _person_for(principal)
    connection = _own_connection(person)
    try:
        return model_status(connection.base_url, _token(connection))
    except HarnessHttpError as exc:
        raise AiError("The local model status could not be read.", failure_from_http(exc)) from None


def warm_for_chat(principal):
    person = _person_for(principal)
    connection = _own_connection(person)
    if connection.chat_backend != LOCAL_BACKEND:
        return local_status(person)
    try:
        return warm_local_model(connection.base_url, _token(connection))
    except HarnessHttpError as exc:
        raise AiError("The local model could not be warmed.", failure_from_http(exc)) from None


def run_structured(
    principal,
    prompt,
    *,
    feature,
    backend=None,
    tools=None,
    session_id="",
    on_session=None,
    sleep=None,
    monotonic=None,
):
    return _run(
        principal,
        prompt,
        feature=feature,
        backend=backend,
        tools=tools,
        use_chat=False,
        session_id=session_id,
        on_session=on_session,
        sleep=sleep,
        monotonic=monotonic,
    )


def run_conversation(
    principal,
    prompt,
    *,
    feature,
    backend=None,
    tools=None,
    session_id="",
    on_session=None,
    sleep=None,
    monotonic=None,
):
    return _run(
        principal,
        prompt,
        feature=feature,
        backend=backend,
        tools=tools,
        use_chat=True,
        session_id=session_id,
        on_session=on_session,
        sleep=sleep,
        monotonic=monotonic,
    )


def record_usage(person, *, provider, backend, feature, result: ProviderResult):
    AiUsageEvent.objects.create(
        member=person,
        provider=provider,
        backend=backend,
        feature=feature,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        outcome="ok" if result.ok else (result.failure_code or PROVIDER_ERROR),
    )


def _run(
    principal,
    prompt,
    *,
    feature,
    backend,
    tools,
    use_chat,
    session_id="",
    on_session=None,
    sleep=None,
    monotonic=None,
):
    person = _person_for(principal)
    if not may_use_ai(person):
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    connection = connection_for(person)
    if connection is None:
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    chosen = (backend or (connection.chat_backend if use_chat else connection.background_backend) or "").strip()
    if not chosen:
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    try:
        token = _token(connection)
    except AiError as exc:
        return ProviderResult(ok=False, failure_code=exc.failure_code)
    project = connection.harness_project or getattr(settings, "AGENT_HARNESS_PROJECT", "financial-planner")
    model = connection.chat_model if use_chat else connection.background_model
    runner = None
    if tools:

        def runner(name, args):
            return _invoke_tool(person, tools, name, args)
    known_session = {"id": session_id or ""}

    def track_session(new_id):
        known_session["id"] = new_id
        if on_session is not None:
            on_session(new_id)

    try:
        if session_id:
            result = wait_for_session(
                connection.base_url,
                token,
                session_id,
                tool_runner=runner,
                sleep=sleep,
                monotonic=monotonic,
            )
        else:
            result = run_session(
                connection.base_url,
                token,
                prompt=prompt,
                backend=chosen,
                project=project,
                model=model,
                tools=tools,
                tool_runner=runner,
                on_session=track_session,
                sleep=sleep,
                monotonic=monotonic,
            )
    except HarnessHttpError as exc:
        # A transport or server error while polling says nothing about the session
        # itself, so keep it open for the retry to resume instead of starting a duplicate.
        transient = exc.status == 0 or exc.status == 429 or exc.status >= 500
        result = ProviderResult(
            ok=False,
            failure_code=failure_from_http(exc),
            usage=Usage(),
            session_id=known_session["id"] or None,
            session_open=bool(known_session["id"]) and transient,
        )
    record_usage(
        person,
        provider=connection.kind,
        backend=chosen,
        feature=feature,
        result=result,
    )
    return result


def finance_tools_allowed_for(person):
    household = current_household(person)
    include_household = household is None or household_ai_allowed(household)
    return include_household


def _invoke_tool(person, tools, name, args):
    from .ai_tools import run_tool

    return run_tool(person, tools, name, args)


def _own_connection(person):
    connection = connection_for(person)
    if connection is None:
        raise PermissionDenied(_DENIED)
    return connection


def _token(connection):
    try:
        return decrypt_secret(connection.encrypted_token)
    except Exception as exc:
        raise AiError(
            "This AI connection can no longer be read. Disconnect it and connect again.",
            AUTHORIZATION_REQUIRED,
        ) from exc


def _choose_project(projects):
    wanted = getattr(settings, "AGENT_HARNESS_PROJECT", "financial-planner")
    names = []
    for item in projects or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    if wanted in names:
        return wanted
    return names[0] if names else wanted


def _backend_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("backends"), list):
        return payload["backends"]
    return []


def _require_selectable(backends, name, *, live):
    info = backends.get(name)
    if info is None or not info.available:
        raise AiError("Choose an available backend.")
    if live and not info.suits_live:
        raise AiError("That backend is not available for live chat.")
    if not live and not info.suits_background:
        raise AiError("That backend is not available for background jobs.")
    return name
