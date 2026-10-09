"""Chat conversations over read-only finance tools."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .ai_jobs import _connection_marker
from .ai_plan import LINK_PROMPT, plan_end_user
from .ai_services import (
    AiError,
    HOSTED_SHARED_DENIED,
    SHARED_LOCAL_CHOOSE,
    SHARED_LOCAL_UNAVAILABLE,
    offered_local_connection,
    resolve_ai,
    run_conversation,
)
from .ai_tools import INSTRUCTION_CONTEXT, default_tools
from .ai_types import (
    API_KINDS,
    APP_TOOLS_ONLY_UNSUPPORTED,
    AUTHORIZATION_REQUIRED,
    LIMIT_REACHED,
    LOCAL_BACKEND,
    LOGIN_REQUIRED,
    ToolResult,
    UNAVAILABLE,
)
from .chat_proposals import proposal_tools
from .lifecycle_services import _DENIED, _person_for
from .models import AiConversation, AiConversationMessage
from .category_services import current_household
from .policy_services import household_ai_allowed, may_use_ai

FEATURE = "chat"
OUT_OF_SCOPE = (
    "I can only answer questions about this member's finances using the app's "
    "read-only tools and suggest changes for you to confirm. I cannot change data myself or give financial advice."
)
TURN_LIMIT = "This conversation has reached its turn limit. Start a new one from Chat."
TOOL_LIMIT = "This conversation has reached its tool-call limit."
LOCAL_HIDDEN = "Chat on the local model is not offered yet. Choose Claude in Settings → AI."
NO_CHAT_BACKEND = "No chat backend is available yet. Sign in to Claude on the harness, then choose it in Settings → AI."


def expire_days():
    return max(1, int(getattr(settings, "AI_CHAT_EXPIRE_DAYS", 30)))


def max_turns():
    return max(1, int(getattr(settings, "AI_CHAT_MAX_TURNS", 20)))


def max_tool_calls():
    return max(1, int(getattr(settings, "AI_CHAT_MAX_TOOL_CALLS", 40)))


def chat_local_enabled(connection=None):
    """The operator's hard switch, and the host's "offer the local model for chat" setting."""
    if not getattr(settings, "AI_CHAT_LOCAL_ENABLED", True):
        return False
    return bool(connection is not None and connection.offer_local_chat)


def purge_expired(person=None):
    query = AiConversation.objects.filter(expires_at__lte=timezone.now())
    if person is not None:
        query = query.filter(member=person)
    query.delete()


def start_conversation(principal):
    person = _person_for(principal)
    connection, backend = resolve_ai(person, use_chat=True)
    return _new_conversation(person, backend)


def conversations_for(principal):
    person = _person_for(principal)
    purge_expired(person)
    return AiConversation.objects.visible_to(person).order_by("-updated_at", "-pk")


def conversation_for(principal, conversation_id):
    person = _person_for(principal)
    purge_expired(person)
    return AiConversation.objects.visible_to(person).filter(pk=conversation_id).first()


def delete_conversation(principal, conversation_id):
    person = _person_for(principal)
    row = AiConversation.objects.owned_by(person).filter(pk=conversation_id).first()
    if row is None:
        raise PermissionDenied(_DENIED)
    row.delete()


def delete_all_conversations(principal):
    person = _person_for(principal)
    AiConversation.objects.owned_by(person).delete()


def delete_conversations_for_account(account_id):
    doomed = []
    for row in AiConversation.objects.iterator():
        ids = row.used_account_ids or []
        if account_id in ids:
            doomed.append(row.pk)
    if doomed:
        AiConversation.objects.filter(pk__in=doomed).delete()


def sanitize_page_context(raw):
    if not raw:
        return {}
    if not isinstance(raw, dict):
        return {}
    route = str(raw.get("route") or raw.get("path") or "").strip()
    query = str(raw.get("query") or raw.get("query_string") or "").strip()
    if query.startswith("?"):
        query = query[1:]
    if not route.startswith("/") or "\n" in route or "://" in route:
        route = ""
    if "\n" in query:
        query = ""
    cleaned = {}
    if route:
        cleaned["route"] = route
    if query:
        cleaned["query"] = query
    return cleaned


def page_context_from_request(request):
    return sanitize_page_context(
        {
            "route": request.path,
            "query": request.GET.urlencode(),
        }
    )


def send_message(principal, text, *, conversation_id=None, page_context=None):
    """Store the question and a pending reply; the chat runner answers it in the background."""
    person = _person_for(principal)
    purge_expired(person)
    _connection, backend = chat_backend_for(person)
    prompt = (text or "").strip()
    if not prompt:
        raise AiError("Enter a question.")
    context_payload = sanitize_page_context(page_context)
    conversation = None
    if conversation_id:
        conversation = conversation_for(person, conversation_id)
        if conversation is None:
            raise PermissionDenied(_DENIED)
    else:
        conversation = conversations_for(person).first()
    if conversation is None:
        conversation = _new_conversation(person, backend)
    with transaction.atomic():
        conversation = _lock_conversation(conversation.pk)
        if conversation.turn_count >= max_turns():
            raise AiError(TURN_LIMIT, LIMIT_REACHED)
        if conversation.tool_call_count >= max_tool_calls():
            raise AiError(TOOL_LIMIT, LIMIT_REACHED)

        question = AiConversationMessage.objects.create(
            conversation=conversation,
            role=AiConversationMessage.Role.USER,
            content=prompt,
            page_context=context_payload,
        )
        conversation.turn_count += 1
        if not conversation.title:
            conversation.title = prompt[:80]
        conversation.save(update_fields=("turn_count", "title", "updated_at"))

        if _out_of_scope(prompt):
            AiConversationMessage.objects.create(
                conversation=conversation,
                role=AiConversationMessage.Role.ASSISTANT,
                content=OUT_OF_SCOPE,
                backend=backend,
                reply_to=question,
            )
        else:
            AiConversationMessage.objects.create(
                conversation=conversation,
                role=AiConversationMessage.Role.ASSISTANT,
                content="",
                backend=backend,
                reply_to=question,
                status=AiConversationMessage.Status.PENDING,
            )
    return conversation


def chat_backend_for(person):
    """The member's chat connection and backend, or an AiError with the member-facing reason."""
    if not may_use_ai(person):
        raise AiError("AI is off until the current privacy and data policy is accepted.", AUTHORIZATION_REQUIRED)
    connection, backend = resolve_ai(person, use_chat=True)
    if connection is None:
        if offered_local_connection(person) is not None:
            raise AiError(SHARED_LOCAL_CHOOSE, AUTHORIZATION_REQUIRED)
        raise AiError("Connect an AI backend first.", AUTHORIZATION_REQUIRED)
    if connection.owner_id != person.id and not plan_end_user(person, connection, backend):
        if backend != LOCAL_BACKEND:
            raise AiError(HOSTED_SHARED_DENIED, AUTHORIZATION_REQUIRED)
        if not connection.offer_local_to_household:
            raise AiError(SHARED_LOCAL_UNAVAILABLE, UNAVAILABLE)
    elif backend == LOCAL_BACKEND and not chat_local_enabled(connection):
        raise AiError(LOCAL_HIDDEN, UNAVAILABLE)
    if not backend:
        raise AiError(NO_CHAT_BACKEND, UNAVAILABLE)
    return connection, backend


def answer_turn(turn, *, sleep=None, monotonic=None):
    """Ask the harness for one pending turn's reply. Runs in the chat runner, never in a request.

    Tool calls run as the member who owns the conversation, with that member's
    visibility. Returns the reply fields for the runner to store.
    """
    conversation = turn.conversation
    person = conversation.member
    try:
        connection, backend = chat_backend_for(person)
    except AiError as exc:
        return failed_reply(str(exc), backend=turn.backend)
    question = turn.reply_to
    prompt = question.content if question is not None else ""
    context_payload = sanitize_page_context(question.page_context if question is not None else None)

    collected = []
    cap = max_tool_calls()

    def allow_tool(_name, _args):
        with transaction.atomic():
            locked = _lock_conversation(conversation.pk)
            conversation.tool_call_count = locked.tool_call_count
            conversation.used_account_ids = list(locked.used_account_ids or [])
            if locked.tool_call_count >= cap:
                return ToolResult(text=TOOL_LIMIT, ok=False)
            locked.tool_call_count += 1
            locked.save(update_fields=("tool_call_count", "updated_at"))
            conversation.tool_call_count = locked.tool_call_count
        return None

    def on_tool(result: ToolResult):
        collected.append(result)
        with transaction.atomic():
            locked = _lock_conversation(conversation.pk)
            ids = list(locked.used_account_ids or [])
            for account_id in result.account_ids:
                if account_id not in ids:
                    ids.append(account_id)
            locked.used_account_ids = ids
            locked.save(update_fields=("used_account_ids", "updated_at"))
            conversation.used_account_ids = ids
            conversation.tool_call_count = locked.tool_call_count

    tools = default_tools() + proposal_tools(conversation, turn)
    harness_context = _harness_context(context_payload)
    marker = _connection_marker(connection)
    saved_session = (conversation.harness_session_id or "").strip()
    household = current_household(person)
    consent_held = household is None or household_ai_allowed(household)
    follow_up = (
        consent_held
        and bool(saved_session)
        and (conversation.harness_connection or "") == marker
        and (conversation.backend or "") == backend
    )
    session_id = saved_session if follow_up else ""

    def on_session(new_id):
        with transaction.atomic():
            locked = _lock_conversation(conversation.pk)
            locked.harness_session_id = new_id
            locked.harness_connection = marker
            locked.save(update_fields=("harness_session_id", "harness_connection", "updated_at"))
            conversation.harness_session_id = new_id
            conversation.harness_connection = marker

    result = run_conversation(
        person,
        prompt,
        feature=FEATURE,
        backend=backend,
        tools=tools,
        session_id=session_id,
        on_session=on_session,
        sleep=sleep,
        monotonic=monotonic,
        tools_only=True,
        context=harness_context,
        follow_up=follow_up,
        on_tool=on_tool,
        allow_tool=allow_tool,
        history=_api_history(conversation, question) if consent_held and connection.kind in API_KINDS else (),
    )
    with transaction.atomic():
        locked = _lock_conversation(conversation.pk)
        locked.backend = backend
        locked.harness_session_id = conversation.harness_session_id
        locked.harness_connection = conversation.harness_connection
        locked.save(
            update_fields=("backend", "harness_session_id", "harness_connection", "updated_at")
        )
    if not result.ok:
        return failed_reply(failure_text(result.failure_code), backend=backend, notices=result.notices)
    figures = []
    for item in collected:
        figures.extend(item.figures)
    return {
        "status": AiConversationMessage.Status.DONE,
        "role": AiConversationMessage.Role.ASSISTANT,
        "content": result.answer or "",
        "backend": backend,
        "figures": list(figures),
        "notices": list(result.notices),
    }


def failed_reply(text, *, backend="", notices=()):
    return {
        "status": AiConversationMessage.Status.FAILED,
        "role": AiConversationMessage.Role.ERROR,
        "content": text,
        "backend": backend,
        "figures": [],
        "notices": list(notices),
    }


def _lock_conversation(pk):
    return AiConversation.objects.select_for_update().get(pk=pk)


def _new_conversation(person, backend):
    now = timezone.now()
    return AiConversation.objects.create(
        member=person,
        backend=backend,
        expires_at=now + timedelta(days=expire_days()),
        used_account_ids=[],
    )


def _api_history(conversation, question):
    """Earlier turns, resent on every request: an API-key backend keeps no session."""
    query = conversation.messages.filter(
        role__in=(AiConversationMessage.Role.USER, AiConversationMessage.Role.ASSISTANT),
        status=AiConversationMessage.Status.DONE,
    )
    if question is not None:
        query = query.filter(pk__lt=question.pk)
    rows = list(query.order_by("-pk")[: 2 * max_turns()])[::-1]
    while rows and rows[0].role != AiConversationMessage.Role.USER:
        rows.pop(0)
    return [{"role": row.role, "content": row.content} for row in rows]


def _harness_context(page_context):
    blocks = [{"title": "How to answer", "content": INSTRUCTION_CONTEXT}]
    if page_context:
        parts = []
        if page_context.get("route"):
            parts.append(f"route={page_context['route']}")
        if page_context.get("query"):
            parts.append(f"query={page_context['query']}")
        blocks.append({"title": "Current page", "content": "\n".join(parts)})
    return blocks


def _out_of_scope(prompt):
    text = prompt.lower()
    needles = (
        "drop table",
        "delete from",
        "run sql",
        "write a virus",
        "hack",
        "transfer money",
        "wire funds",
    )
    return any(item in text for item in needles)


def failure_text(code):
    if code == LOGIN_REQUIRED:
        return LINK_PROMPT
    if code == AUTHORIZATION_REQUIRED:
        return "Authorization is required to use this AI backend."
    if code == LIMIT_REACHED:
        return "The AI backend reached its rate or usage limit."
    if code == APP_TOOLS_ONLY_UNSUPPORTED:
        return "This backend cannot run a tools-only chat session."
    if code == UNAVAILABLE:
        return "The AI backend is unavailable."
    return "The AI backend could not complete that question."
