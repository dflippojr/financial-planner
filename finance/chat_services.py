"""Chat conversations over read-only finance tools."""

from __future__ import annotations

from datetime import timedelta
import json

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
from .input_limits import MAX_CHAT_PROMPT_CHARS, MAX_PAGE_CONTEXT_CHARS, MAX_CHAT_MESSAGES
from .models import Account, AiConversation, AiConversationMessage, Budget, Person, PlannedItem
from .category_services import current_household
from .policy_services import household_ai_allowed, may_use_ai

FEATURE = "chat"
OUT_OF_SCOPE = (
    "I can only answer questions about this member's finances using the app's "
    "read-only tools and suggest changes for you to confirm. I cannot change data myself or give financial advice."
)
TURN_LIMIT = "This conversation has reached its turn limit. Start a new one from Chat."
TOOL_LIMIT = "This conversation has reached its tool-call limit."
PENDING_LIMIT = "Wait for your earlier questions to be answered before asking another."
CONVERSATION_LIMIT = "You have reached the limit of open conversations. Delete one from Chat to start another."
TILE_UNAVAILABLE = "This tile or its filters are no longer available to AI. Reopen the tile and ask again."
LOCAL_HIDDEN = "Chat on the local model is not offered yet. Choose Claude in Settings → AI."
NO_CHAT_BACKEND = "No chat backend is available yet. Sign in to Claude on the harness, then choose it in Settings → AI."


def expire_days():
    return max(1, int(getattr(settings, "AI_CHAT_EXPIRE_DAYS", 30)))


def max_turns():
    return max(1, int(getattr(settings, "AI_CHAT_MAX_TURNS", 20)))


def max_tool_calls():
    return max(1, int(getattr(settings, "AI_CHAT_MAX_TOOL_CALLS", 40)))


def max_pending_turns():
    """Unanswered turns one member may have queued across all of their conversations."""
    return max(1, int(getattr(settings, "AI_CHAT_MAX_PENDING_PER_MEMBER", 3)))


def max_conversations():
    """Unexpired conversations one member may keep."""
    return max(1, int(getattr(settings, "AI_CHAT_MAX_CONVERSATIONS", 50)))


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


# The household rows a tool can quote, by the conversation field that records them.
_USED_ROWS = {
    "used_account_ids": Account,
    "used_planned_item_ids": PlannedItem,
    "used_budget_ids": Budget,
}


def delete_conversations_without_access(account_ids=(), *, planned_item_ids=(), budget_ids=()):
    """Delete conversations that used one of these rows when their member can no longer see it.

    Chat history is derived from what the member can see, so unsharing an account,
    making a household planned item or budget private, or leaving a household
    purges it the same way deleting the row does.
    """
    affected = {
        field: set(ids)
        for field, ids in (
            ("used_account_ids", account_ids),
            ("used_planned_item_ids", planned_item_ids),
            ("used_budget_ids", budget_ids),
        )
    }
    affected = {field: ids for field, ids in affected.items() if ids}
    if not affected:
        return
    visible_by_member = {}
    doomed = []
    for row in AiConversation.objects.select_related("member").iterator():
        used = {field: ids.intersection(getattr(row, field) or []) for field, ids in affected.items()}
        if not any(used.values()):
            continue
        if row.member_id not in visible_by_member:
            visible_by_member[row.member_id] = {
                field: set(
                    _USED_ROWS[field].objects.visible_to(row.member).filter(pk__in=ids).values_list("pk", flat=True)
                )
                for field, ids in affected.items()
            }
        visible = visible_by_member[row.member_id]
        if any(ids - visible[field] for field, ids in used.items()):
            doomed.append(row.pk)
    if doomed:
        AiConversation.objects.filter(pk__in=doomed).delete()


def keep_conversation_if_visible(conversation_id):
    """Before a reply is stored, purge its conversation if the member lost sight of a row it used.

    Locks the used rows first, accounts before the others, as unsharing, leaving
    and a scope change do, so a reply racing one of them either commits before
    its purge scan or sees its committed result.
    """
    conversation = AiConversation.objects.select_related("member").filter(pk=conversation_id).first()
    if conversation is None:
        return False
    for field, model in _USED_ROWS.items():
        used = sorted(set(getattr(conversation, field) or []))
        if used:
            list(model.objects.select_for_update().filter(pk__in=used).order_by("pk").values_list("pk", flat=True))
    if _uses_only_visible_rows(conversation, conversation.member):
        return True
    conversation.delete()
    return False


def _uses_only_visible_rows(conversation, person):
    for field, model in _USED_ROWS.items():
        used = set(getattr(conversation, field) or [])
        if used and model.objects.visible_to(person).filter(pk__in=used).count() != len(used):
            return False
    return True


def sanitize_page_context(raw, person=None):
    if not raw:
        return {}
    if not isinstance(raw, dict):
        return {}
    from .ai_tiles import sanitize_tile

    try:
        if len(json.dumps(raw)) > MAX_PAGE_CONTEXT_CHARS:
            return {}
    except (TypeError, ValueError, RecursionError):
        return {}
    route = str(raw.get("route") or raw.get("path") or "").strip()
    query = str(raw.get("query") or raw.get("query_string") or "").strip()
    if len(route) + len(query) > MAX_PAGE_CONTEXT_CHARS:
        return {}
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
    tile = sanitize_tile(raw.get("tile"), person)
    if tile:
        cleaned["tile"] = tile
    return cleaned


def _validated_page_context(raw, person):
    """Keep supplied tile constraints intact at send time and queued execution."""
    cleaned = sanitize_page_context(raw, person)
    supplied_tile = raw.get("tile") if isinstance(raw, dict) else None
    if not isinstance(supplied_tile, dict):
        return cleaned
    from .ai_tiles import TILES, tile_data_shareable

    kind = supplied_tile.get("kind")
    spec = TILES.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return cleaned
    tile = cleaned.get("tile")
    if not tile or not tile_data_shareable(person, tile["filters"], kind=kind):
        raise AiError(TILE_UNAVAILABLE)
    supplied = supplied_tile.get("filters")
    supplied = supplied if isinstance(supplied, dict) else {}
    for key in spec.filters:
        value = supplied.get(key)
        if key == "all_dates" and value is False:
            continue
        if value not in (None, "") and key not in tile["filters"]:
            raise AiError(TILE_UNAVAILABLE)
    return cleaned


def page_context_from_request(request):
    return sanitize_page_context(
        {
            "route": request.path,
            "query": request.GET.urlencode(),
        }
    )


def send_message(principal, text, *, conversation_id=None, page_context=None, start_new=False):
    """Store the question and a pending reply; the chat runner answers it in the background."""
    if len(text or "") > MAX_CHAT_PROMPT_CHARS:
        raise AiError(f"Questions must be at most {MAX_CHAT_PROMPT_CHARS:,} characters.")
    person = _person_for(principal)
    purge_expired(person)
    _connection, backend = chat_backend_for(person)
    prompt = (text or "").strip()
    if not prompt:
        raise AiError("Enter a question.")
    context_payload = _validated_page_context(page_context, person)
    conversation = None
    if conversation_id:
        conversation = conversation_for(person, conversation_id)
        if conversation is None:
            raise PermissionDenied(_DENIED)
    elif not start_new:
        # Plain Chat forms keep their latest-thread fallback; a fresh popover has shown no history.
        conversation = conversations_for(person).first()
    if conversation is None:
        conversation = _new_conversation(person, backend)
    with transaction.atomic():
        _lock_member(person)
        conversation = _lock_conversation(conversation.pk)
        pending = AiConversationMessage.objects.filter(
            conversation__member=person, status=AiConversationMessage.Status.PENDING
        ).count()
        if pending >= max_pending_turns():
            raise AiError(PENDING_LIMIT, LIMIT_REACHED)
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
    saved_context = question.page_context if question is not None else None
    try:
        context_payload = _validated_page_context(saved_context, person)
        saved_tile = saved_context.get("tile") if isinstance(saved_context, dict) else None
        if saved_tile and saved_tile != context_payload.get("tile"):
            raise AiError(TILE_UNAVAILABLE)
    except AiError as exc:
        return failed_reply(str(exc), backend=backend)

    collected = []
    cap = max_tool_calls()

    def tool_budget():
        used = AiConversation.objects.filter(pk=conversation.pk).values_list("tool_call_count", flat=True).first()
        return 0 if used is None else cap - used

    def allow_tool(_name, _args):
        with transaction.atomic():
            locked = _lock_conversation(conversation.pk)
            conversation.tool_call_count = locked.tool_call_count
            for field in _USED_ROWS:
                setattr(conversation, field, list(getattr(locked, field) or []))
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
            for field, quoted in (
                ("used_account_ids", result.account_ids),
                ("used_planned_item_ids", result.planned_item_ids),
                ("used_budget_ids", result.budget_ids),
            ):
                ids = list(getattr(locked, field) or [])
                for row_id in quoted:
                    if row_id not in ids:
                        ids.append(row_id)
                setattr(locked, field, ids)
                setattr(conversation, field, ids)
            locked.save(update_fields=(*_USED_ROWS, "updated_at"))
            conversation.tool_call_count = locked.tool_call_count

    tools = default_tools() + proposal_tools(conversation, turn)
    harness_context = _harness_context(context_payload)
    marker = _connection_marker(connection)
    saved_session = (conversation.harness_session_id or "").strip()
    household = current_household(person)
    consent_held = household is None or household_ai_allowed(household)
    # Earlier turns built from a row the member can no longer see are never resent.
    history_held = consent_held and _uses_only_visible_rows(conversation, person)
    follow_up = (
        history_held
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
        history=_api_history(conversation, question) if history_held and connection.kind in API_KINDS else (),
        tool_budget=tool_budget,
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


def _lock_member(person):
    """Serialise one member's quota checks so two requests cannot both pass the cap."""
    Person.objects.select_for_update().filter(pk=person.pk).first()


@transaction.atomic
def _new_conversation(person, backend):
    _lock_member(person)
    now = timezone.now()
    if AiConversation.objects.filter(member=person, expires_at__gt=now).count() >= max_conversations():
        raise AiError(CONVERSATION_LIMIT, LIMIT_REACHED)
    return AiConversation.objects.create(
        member=person,
        backend=backend,
        expires_at=now + timedelta(days=expire_days()),
        used_account_ids=[],
        used_planned_item_ids=[],
        used_budget_ids=[],
    )


def _api_history(conversation, question):
    """Earlier turns, resent on every request: an API-key backend keeps no session."""
    query = conversation.messages.filter(
        role__in=(AiConversationMessage.Role.USER, AiConversationMessage.Role.ASSISTANT),
        status=AiConversationMessage.Status.DONE,
    )
    if question is not None:
        query = query.filter(pk__lt=question.pk)
    rows = list(query.order_by("-pk")[:min(MAX_CHAT_MESSAGES, 2 * max_turns())])[::-1]
    while rows and rows[0].role != AiConversationMessage.Role.USER:
        rows.pop(0)
    return [{"role": row.role, "content": row.content} for row in rows]


def _harness_context(page_context):
    blocks = [{"title": "How to answer", "content": INSTRUCTION_CONTEXT}]
    if page_context.get("route") or page_context.get("query"):
        parts = []
        if page_context.get("route"):
            parts.append(f"route={page_context['route']}")
        if page_context.get("query"):
            parts.append(f"query={page_context['query']}")
        blocks.append({"title": "Current page", "content": "\n".join(parts)})
    from .ai_tiles import TILES

    context_tile = page_context.get("tile", {})
    tile = TILES.get(context_tile.get("kind"))
    if tile:
        parts = [f"kind={context_tile['kind']}", f"label={tile.label}"]
        parts.extend(f"{key}={value}" for key, value in context_tile["filters"].items())
        parts.append("tools=" + ", ".join(tile.tools))
        parts.append("Use these filters with the named tools. A month means its full date range; compare prior months where needed.")
        blocks.append({"title": "Current tile", "content": "\n".join(parts)})
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
