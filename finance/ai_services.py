"""Per-member AI connections and a backend-neutral request surface."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .ai_harness import (
    LOCAL_WARM_REFUSED,
    add_session_context,
    default_backends,
    hosted_sessions_enabled,
    describe_backend,
    discover,
    failure_from_http,
    list_backends,
    model_status,
    run_session,
    send_session_message,
    wait_for_session,
    warm_local_model,
)
from .ai_api import label as api_label, looks_like_key, model_for, run_api
from .ai_http import HarnessHttpError
from .ai_types import (
    API_KINDS,
    AUTHORIZATION_REQUIRED,
    HOSTED_BACKENDS,
    LOCAL_BACKEND,
    LIMIT_REACHED,
    LOGIN_REQUIRED,
    PLAN_BACKENDS,
    PLAN_REPLACES_API,
    PROVIDER_ERROR,
    SHARED_CONNECTION_ID_REF,
    SHARED_LOCAL_CHOICE,
    SHARED_LOCAL_REF,
    UNAVAILABLE,
    ProviderResult,
    Usage,
)
from .ai_plan import mark_login_required, plan_end_user, plan_link, plan_links
from .ai_urls import HarnessUrlError, parse_harness_url
from .encryption import decrypt_secret, encrypt_secret
from .lifecycle_services import _DENIED, _person_for
from .models import AiJob, AiProviderConnection, AiUsageEvent, Membership
from .policy_services import household_ai_allowed, may_use_ai
from .category_services import current_household

TOKEN_PREFIX = "ha-"
CONNECT_DENIED = "AI backends cannot be connected until the privacy and data policy is accepted."
SHARED_LOCAL_UNAVAILABLE = "The household local model is not available."
SHARED_LOCAL_NOT_OFFERED = "The household local model is not offered."
SHARED_LOCAL_CHOOSE = "Choose Local model (shared) in Settings → AI."
HOSTED_SHARED_DENIED = "Hosted AI backends on another member's connection cannot be used."
TOKEN_FORMAT = "Paste an Agent Harness App token that starts with ha-."
TOKEN_REJECTED = (
    "That token isn't valid for this Agent Harness. Create an App token in that "
    "harness's Settings → Apps, then connect again."
)
HARNESS_NOT_FOUND = (
    "That URL doesn't point at an Agent Harness API. Use the harness's own address, "
    "without /api/v1 or a page path."
)
HARNESS_UNREACHABLE = (
    "The app can't reach that Agent Harness. Inside Docker, localhost is the app's "
    "own container, so use the harness's tailnet URL."
)
HARNESS_FAILED = "Agent Harness couldn't finish connecting. Try again in a moment."


class AiError(Exception):
    def __init__(self, message, failure_code=PROVIDER_ERROR):
        self.failure_code = failure_code
        super().__init__(message)


def connection_for(principal):
    """The member's Agent Harness connection. API-key connections are separate."""
    person = _person_for(principal)
    return AiProviderConnection.objects.owned_by(person).filter(
        kind=AiProviderConnection.Kind.AGENT_HARNESS
    ).first()


def api_connection(principal, kind):
    person = _person_for(principal)
    if kind not in API_KINDS:
        return None
    return AiProviderConnection.objects.owned_by(person).filter(kind=kind).first()


def api_connections(principal):
    person = _person_for(principal)
    return list(AiProviderConnection.objects.owned_by(person).filter(kind__in=API_KINDS).order_by("kind"))


def preferred_api_connection(person, *, use_chat):
    field = "use_for_chat" if use_chat else "use_for_background"
    return AiProviderConnection.objects.owned_by(person).filter(kind__in=API_KINDS, **{field: True}).first()


def member_has_ai(principal):
    try:
        person = _person_for(principal)
    except PermissionDenied:
        return False
    if not may_use_ai(person):
        return False
    if connection_for(person) is not None or api_connections(person) or plan_links(person):
        return True
    opted = person.use_shared_local_chat or person.use_shared_local_background
    return opted and offered_local_connection(person) is not None


def offered_local_connection(principal):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        return None
    owner_ids = Membership.objects.filter(
        household=household,
        ended_at__isnull=True,
    ).exclude(person=person).values("person_id")
    return (
        AiProviderConnection.objects.filter(
            owner_id__in=owner_ids,
            offer_local_to_household=True,
        )
        .order_by("pk")
        .first()
    )


def _linked_plan(person, backend):
    """The connection and backend of the member's own linked plan, when they have one for it."""
    link = plan_link(person, backend)
    if link is None:
        return None
    return link.connection, backend


def resolve_ai(principal, *, use_chat, requested_backend=None):
    person = _person_for(principal)
    requested = (requested_backend or "").strip()
    if requested in API_KINDS:
        return api_connection(person, requested), requested
    if requested in PLAN_BACKENDS:
        # A linked plan wins over the member's API key for that backend.
        linked = _linked_plan(person, requested)
        if linked is not None:
            return linked
    if not requested:
        preferred = preferred_api_connection(person, use_chat=use_chat)
        if preferred is not None:
            linked = _linked_plan(person, next(b for b, k in PLAN_REPLACES_API.items() if k == preferred.kind))
            return linked if linked is not None else (preferred, preferred.kind)
    own = connection_for(person)
    if requested in HOSTED_BACKENDS:
        return own, requested
    wants_shared = person.use_shared_local_chat if use_chat else person.use_shared_local_background
    if wants_shared and requested in ("", LOCAL_BACKEND, SHARED_LOCAL_CHOICE):
        shared = offered_local_connection(person)
        if shared is not None:
            return shared, LOCAL_BACKEND
    if own is None:
        if not requested:
            first = plan_links(person)
            if first:
                return first[0].connection, first[0].backend
        return None, requested
    chosen = requested or ((own.chat_backend if use_chat else own.background_backend) or "").strip()
    if chosen == SHARED_LOCAL_CHOICE:
        return None, chosen
    if chosen in PLAN_BACKENDS:
        linked = _linked_plan(person, chosen)
        if linked is not None:
            return linked
    return own, chosen


def connect_harness(principal, *, base_url, token):
    person = _person_for(principal)
    if not may_use_ai(person):
        raise AiError(CONNECT_DENIED, AUTHORIZATION_REQUIRED)
    url = parse_harness_url(base_url)
    secret = (token or "").strip()
    if not _looks_like_app_token(secret):
        raise AiError(TOKEN_FORMAT)
    try:
        _root, backends, projects = discover(url, secret)
    except HarnessHttpError as exc:
        raise AiError(_connect_failure_message(exc), failure_from_http(exc)) from None
    infos = [describe_backend(item) for item in _backend_items(backends)]
    chat, background = default_backends(infos)
    project = _choose_project(projects)
    encrypted = encrypt_secret(secret)
    existing = connection_for(person)
    if existing is not None:
        stop_shared_local_use(existing)
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
            "offer_local_to_household": False,
            "connected_at": timezone.now(),
            "last_status": "",
        },
    )
    _forget_saved_sessions(person)
    return connection


def connect_api_key(principal, *, kind, key, chat_model="", background_model=""):
    """Save a member's own provider key. It is stored encrypted and never shown again."""
    person = _person_for(principal)
    if not may_use_ai(person):
        raise AiError(CONNECT_DENIED, AUTHORIZATION_REQUIRED)
    if kind not in API_KINDS:
        raise AiError("Choose Anthropic or OpenAI.")
    secret = (key or "").strip()
    if not looks_like_key(kind, secret):
        raise AiError(f"Paste a valid {api_label(kind)} key.")
    existing = api_connection(person, kind)
    connection, _created = AiProviderConnection.objects.update_or_create(
        owner=person,
        kind=kind,
        defaults={
            "base_url": "",
            "encrypted_token": encrypt_secret(secret),
            "chat_model": model_for(kind, chat_model, use_chat=True),
            "background_model": model_for(kind, background_model, use_chat=False),
            "use_for_chat": existing.use_for_chat if existing else False,
            "use_for_background": existing.use_for_background if existing else False,
            "connected_at": timezone.now(),
            "last_status": "",
        },
    )
    return connection


def set_api_defaults(principal, *, kind, chat_model, background_model, use_chat, use_background):
    person = _person_for(principal)
    connection = api_connection(person, kind)
    if connection is None:
        raise AiError("Connect that provider first.")
    connection.chat_model = model_for(kind, chat_model, use_chat=True)
    connection.background_model = model_for(kind, background_model, use_chat=False)
    connection.use_for_chat = bool(use_chat)
    connection.use_for_background = bool(use_background)
    connection.save(update_fields=("chat_model", "background_model", "use_for_chat", "use_for_background"))
    # One backend per feature: choosing this connection replaces any other choice.
    _clear_other_choices(person, keep=connection, chat=connection.use_for_chat, background=connection.use_for_background)
    return connection


def disconnect_api_key(principal, kind):
    person = _person_for(principal)
    if kind in API_KINDS:
        AiProviderConnection.objects.owned_by(person).filter(kind=kind).delete()


def _clear_other_choices(person, *, keep=None, chat=False, background=False, shared_local=True):
    """Make the chosen API connection the only choice for those features."""
    others = AiProviderConnection.objects.owned_by(person).filter(kind__in=API_KINDS)
    if keep is not None:
        others = others.exclude(pk=keep.pk)
    if chat:
        others.update(use_for_chat=False)
    if background:
        others.update(use_for_background=False)
    fields = []
    if not shared_local:
        return
    if chat and person.use_shared_local_chat:
        person.use_shared_local_chat = False
        fields.append("use_shared_local_chat")
    if background and person.use_shared_local_background:
        person.use_shared_local_background = False
        fields.append("use_shared_local_background")
    if fields:
        person.save(update_fields=(*fields, "updated_at"))


def api_usage_summary(principal):
    """Approximate tokens this calendar month on the member's own API connections."""
    person = _person_for(principal)
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    totals = {kind: 0 for kind in API_KINDS}
    rows = AiUsageEvent.objects.visible_to(person).filter(provider__in=API_KINDS, created_at__gte=start)
    for row in rows.values("provider", "prompt_tokens", "completion_tokens"):
        totals[row["provider"]] += (row["prompt_tokens"] or 0) + (row["completion_tokens"] or 0)
    return totals


def _looks_like_app_token(secret):
    # The token goes into an HTTP header: a curly quote or line break from a bad
    # paste would make http.client fail before the harness sees the request.
    return (
        secret.startswith(TOKEN_PREFIX)
        and secret.isascii()
        and secret.isprintable()
        and not any(ch.isspace() for ch in secret)
    )


def _connect_failure_message(exc):
    if exc.status in {401, 403}:
        return TOKEN_REJECTED
    if exc.status == 404:
        return HARNESS_NOT_FOUND
    if exc.status == 0:
        return HARNESS_UNREACHABLE
    return HARNESS_FAILED


def disconnect_harness(principal):
    person = _person_for(principal)
    connection = connection_for(person)
    if connection is not None:
        stop_shared_local_use(connection)
    AiProviderConnection.objects.owned_by(person).filter(
        kind=AiProviderConnection.Kind.AGENT_HARNESS
    ).delete()
    _forget_saved_sessions(person)


def _forget_saved_sessions(person):
    """Saved session ids belong to the old harness; never resume them on a new connection.

    A running job that is still alive saves its id again when it finishes; one left
    behind by a crashed runner is requeued by stale-job recovery as a fresh session.
    """
    AiJob.objects.filter(
        member=person,
        status__in=(AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL, AiJob.Status.RUNNING),
    ).exclude(harness_session_id="").update(harness_session_id="", updated_at=timezone.now())


def set_defaults(principal, *, chat_backend, background_backend, chat_model="", background_model=""):
    person = _person_for(principal)
    connection = connection_for(person)
    if connection is None:
        raise AiError("Connect an AI backend first.")
    backends = {item.id: item for item in discovered_backends(person)}
    chat = (chat_backend or "").strip()
    background = (background_backend or "").strip()
    person.use_shared_local_chat = chat == SHARED_LOCAL_CHOICE
    person.use_shared_local_background = background == SHARED_LOCAL_CHOICE
    if person.use_shared_local_chat:
        if offered_local_connection(person) is None:
            raise AiError(SHARED_LOCAL_NOT_OFFERED, UNAVAILABLE)
    else:
        connection.chat_backend = _require_selectable(backends, chat, live=True)
    if person.use_shared_local_background:
        if offered_local_connection(person) is None:
            raise AiError(SHARED_LOCAL_NOT_OFFERED, UNAVAILABLE)
    else:
        connection.background_backend = _require_selectable(backends, background, live=False)
    connection.chat_model = (chat_model or "").strip()
    connection.background_model = (background_model or "").strip()
    person.save(update_fields=("use_shared_local_chat", "use_shared_local_background", "updated_at"))
    # Choosing a harness backend replaces an API-key choice for that feature.
    AiProviderConnection.objects.owned_by(person).filter(kind__in=API_KINDS).update(
        use_for_chat=False, use_for_background=False
    )
    connection.save(
        update_fields=(
            "chat_backend",
            "background_backend",
            "chat_model",
            "background_model",
        )
    )
    return connection


def set_offer_local_to_household(principal, offered):
    person = _person_for(principal)
    connection = _own_connection(person)
    enabled = bool(offered)
    was = connection.offer_local_to_household
    connection.offer_local_to_household = enabled
    connection.save(update_fields=("offer_local_to_household",))
    if was and not enabled:
        stop_shared_local_use(connection)
    return connection


def set_offer_local_chat(principal, offered):
    person = _person_for(principal)
    connection = _own_connection(person)
    connection.offer_local_chat = bool(offered)
    connection.save(update_fields=("offer_local_chat",))
    return connection


def set_shared_local_use(principal, *, chat, background):
    person = _person_for(principal)
    if not may_use_ai(person):
        raise AiError(CONNECT_DENIED, AUTHORIZATION_REQUIRED)
    want_chat = bool(chat)
    want_background = bool(background)
    if (want_chat or want_background) and offered_local_connection(person) is None:
        raise AiError(SHARED_LOCAL_NOT_OFFERED, UNAVAILABLE)
    person.use_shared_local_chat = want_chat
    person.use_shared_local_background = want_background
    person.save(update_fields=("use_shared_local_chat", "use_shared_local_background", "updated_at"))
    _clear_other_choices(person, chat=want_chat, background=want_background, shared_local=False)
    return person


def stop_shared_local_use(connection):
    now = timezone.now()
    pending = (AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL, AiJob.Status.RUNNING)
    jobs = AiJob.objects.filter(status__in=pending).exclude(member_id=connection.owner_id)
    for job in jobs:
        refs = job.input_refs or {}
        if not refs.get(SHARED_LOCAL_REF):
            continue
        stored_id = refs.get(SHARED_CONNECTION_ID_REF)
        marker = refs.get("harness_connection") or ""
        if stored_id not in (connection.pk, str(connection.pk)) and not str(marker).startswith(
            f"{connection.pk}:"
        ):
            continue
        job.status = AiJob.Status.FAILED
        job.failure_code = UNAVAILABLE
        job.finished_at = now
        job.save(update_fields=("status", "failure_code", "finished_at", "updated_at"))


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
    connection, _backend = resolve_ai(person, use_chat=True)
    if connection is None:
        connection = _own_connection(person)
    try:
        return model_status(connection.base_url, _token(connection))
    except HarnessHttpError as exc:
        raise AiError("The local model status could not be read.", failure_from_http(exc)) from None


def warm_for_chat(principal):
    person = _person_for(principal)
    connection, backend = resolve_ai(person, use_chat=True)
    if connection is None:
        raise PermissionDenied(_DENIED)
    if backend != LOCAL_BACKEND:
        return local_status(person)
    try:
        return warm_local_model(connection.base_url, _token(connection))
    except HarnessHttpError as exc:
        if exc.status == 409:
            raise AiError(LOCAL_WARM_REFUSED, UNAVAILABLE) from None
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
    connection=None,
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
        connection=connection,
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
    tools_only=None,
    context=None,
    follow_up=False,
    on_tool=None,
    allow_tool=None,
    history=(),
):
    use_tools_only = bool(tools) if tools_only is None else bool(tools_only)
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
        tools_only=use_tools_only,
        context=context,
        follow_up=follow_up,
        on_tool=on_tool,
        allow_tool=allow_tool,
        history=history,
    )


def record_usage(person, *, provider, backend, feature, result: ProviderResult, resumed=False):
    AiUsageEvent.objects.create(
        member=person,
        resumed=resumed,
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
    tools_only=False,
    context=None,
    follow_up=False,
    on_tool=None,
    allow_tool=None,
    connection=None,
    history=(),
):
    person = _person_for(principal)
    if not may_use_ai(person):
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    if connection is not None:
        # A background job passes the connection its saved session belongs to, so
        # a changed preference can never send that session to a different harness.
        chosen = (backend or "").strip()
    else:
        connection, chosen = resolve_ai(person, use_chat=use_chat, requested_backend=backend)
    if connection is None or not chosen:
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    # Polling a started session is not a new request; a follow-up prompt is.
    polling_resume = bool(session_id) and not follow_up
    runner = _tool_runner(person, tools, allow_tool, on_tool)
    if connection.kind in API_KINDS:
        return _run_api_connection(
            person,
            connection,
            prompt,
            feature=feature,
            use_chat=use_chat,
            tools=tools,
            runner=runner,
            context=context,
            history=history,
        )
    end_user = plan_end_user(person, connection, chosen)
    shared = connection.owner_id != person.id
    if shared and not end_user:
        denied = _shared_local_denied(person, connection, chosen, use_chat=use_chat, resuming=polling_resume)
        if denied is not None:
            return denied
    # Project-based hosted sessions still need the operator flag. Tools-only
    # chat uses the harness guarantee instead and may call claude without it.
    if chosen != LOCAL_BACKEND and not tools_only and not hosted_sessions_enabled():
        return ProviderResult(ok=False, failure_code=UNAVAILABLE)
    try:
        token = _token(connection)
    except AiError as exc:
        return ProviderResult(ok=False, failure_code=exc.failure_code)
    project = connection.harness_project or getattr(settings, "AGENT_HARNESS_PROJECT", "financial-planner")
    model = "" if shared else (connection.chat_model if use_chat else connection.background_model)
    if end_user:
        # Another login's model choice never applies to a member's own plan.
        model = ""
    known_session = {"id": session_id or ""}

    def track_session(new_id):
        known_session["id"] = new_id
        if on_session is not None:
            on_session(new_id)

    try:
        if follow_up and session_id:
            if context:
                add_session_context(connection.base_url, token, session_id, context)
            result = send_session_message(
                connection.base_url,
                token,
                session_id,
                prompt,
                tool_runner=runner,
                sleep=sleep,
                monotonic=monotonic,
            )
        elif session_id:
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
                project=None if tools_only else project,
                model=model,
                tools=tools,
                tool_runner=runner,
                on_session=track_session,
                sleep=sleep,
                monotonic=monotonic,
                tools_only=tools_only,
                context=context,
                end_user=end_user,
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
    if end_user and result.failure_code == LOGIN_REQUIRED:
        mark_login_required(person, chosen)
    record_usage(
        person,
        provider=connection.kind,
        backend=chosen,
        feature=feature,
        result=result,
        resumed=polling_resume,
    )
    return result


def _tool_runner(person, tools, allow_tool, on_tool):
    if not tools:
        return None

    def runner(name, args):
        if allow_tool is not None:
            denied = allow_tool(name, args)
            if denied is not None:
                return denied.text, denied.ok
        result = _invoke_tool(person, tools, name, args)
        if on_tool is not None:
            on_tool(result)
        return result.text, result.ok

    return runner


def _run_api_connection(person, connection, prompt, *, feature, use_chat, tools, runner, context, history):
    """A member's own API key: stateless, so nothing to resume and the history is resent."""
    if connection.owner_id != person.id:
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    try:
        key = _token(connection)
    except AiError as exc:
        return ProviderResult(ok=False, failure_code=exc.failure_code)
    system = "\n\n".join(
        f"{block['title']}:\n{block['content']}" for block in (context or ()) if block.get("content")
    )
    result = run_api(
        connection.kind,
        key,
        model=model_for(
            connection.kind,
            connection.chat_model if use_chat else connection.background_model,
            use_chat=use_chat,
        ),
        prompt=prompt,
        system=system,
        history=history,
        tools=tools or (),
        tool_runner=runner,
    )
    record_usage(person, provider=connection.kind, backend=connection.kind, feature=feature, result=result)
    return result


def finance_tools_allowed_for(person):
    household = current_household(person)
    include_household = household is None or household_ai_allowed(household)
    return include_household


def _invoke_tool(person, tools, name, args):
    from .ai_tools import run_tool

    return run_tool(person, tools, name, args)


def _shared_local_denied(person, connection, chosen, *, use_chat, resuming=False):
    if not _same_household(person, connection.owner):
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    if not connection.offer_local_to_household:
        return ProviderResult(ok=False, failure_code=UNAVAILABLE)
    if chosen != LOCAL_BACKEND:
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    opted = person.use_shared_local_chat if use_chat else person.use_shared_local_background
    if not opted:
        return ProviderResult(ok=False, failure_code=AUTHORIZATION_REQUIRED)
    # The cap limits new requests; resuming a session already started adds no GPU work.
    if not resuming and _shared_local_cap_reached(person):
        return ProviderResult(ok=False, failure_code=LIMIT_REACHED)
    return None


def _same_household(person, other):
    left = current_household(person)
    right = current_household(other)
    return left is not None and right is not None and left.pk == right.pk


def _shared_local_cap_reached(person):
    cap = int(getattr(settings, "AI_SHARED_LOCAL_DAILY_CAP", 200))
    if cap <= 0:
        return False
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        AiUsageEvent.objects.filter(
            member=person,
            backend=LOCAL_BACKEND,
            created_at__gte=start,
            resumed=False,
        ).count()
        >= cap
    )


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
    # Never fall back to another project: until app-tools-only sessions exist, a
    # session gets that project's workspace and tools.
    raise AiError(
        f"Create an empty project named {wanted} in Agent Harness, then connect again."
    )


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
