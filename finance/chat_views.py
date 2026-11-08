"""Chat page, popover, and local-model warm endpoints."""

from __future__ import annotations

import json
from collections import defaultdict

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET

from . import chat_markdown
from .access import request_person as _person, service_or_404
from .ai_api import label as api_label
from .ai_services import AiError, local_status, member_has_ai, resolve_ai, warm_for_chat
from .ai_types import API_KINDS, LOCAL_BACKEND, backend_display_name
from .chat_attachment_parsing import TOO_LARGE, AttachmentRejected, parse_upload
from .chat_attachments import (
    attachments_for,
    max_per_conversation,
    provider_key,
    provider_needs_warning,
    remove_attachment,
)
from .chat_proposals import (
    ITEM_FAILED,
    ITEM_STALE,
    ProposalError,
    apply_proposal,
    card_for,
    dismiss_proposal,
    proposal_for,
)
from .chat_runner import recover_stale_turns
from .chat_services import (
    chat_backend_for,
    chat_local_enabled,
    conversations_for,
    delete_all_conversations,
    delete_conversation,
    page_context_from_request,
    send_message,
    start_conversation,
)
from .input_limits import MAX_CHAT_MESSAGES, MAX_PAGE_CONTEXT_CHARS
from .models import AiConversationMessage
from .policy_services import may_use_ai
from .post_only import post_only
from .reauth import safe_next_url


# A chat form body without a parsed file but larger than this held a file the memory-only
# upload handler dropped (FILE_UPLOAD_MAX_MEMORY_SIZE): refuse it rather than send the question alone.
DROPPED_UPLOAD_BYTES = 1024 * 1024
ATTACHMENT_ACCEPT = ".csv,.xlsx,.pdf,text/csv,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _backend_label(backend):
    return backend_display_name(backend, "AI")


def provider_label(person, connection, backend):
    """Where an attached file's contents go: the member's current chat provider, in words (#644)."""
    if backend in API_KINDS:
        return f"{api_label(backend)} (your key)"
    if backend == LOCAL_BACKEND:
        if connection is not None and connection.owner_id != person.id:
            return "the local model on your household's home server"
        return "the local model on your home server"
    if not backend:
        return "your AI provider"
    return f"{_backend_label(backend)} through the agent harness"


@require_GET
def chat_page(request):
    person = _person(request)
    conversation_id = request.GET.get("c")
    conversation = None
    if conversation_id:
        conversation = conversations_for(person).filter(pk=conversation_id).first()
    if conversation is None:
        conversation = conversations_for(person).first()
    return render(request, "finance/chat.html", _chat_context(person, conversation, request))


@post_only("chat")
def chat_send(request):
    person = _person(request)
    wants_json = request.headers.get("Accept") == "application/json"
    prompt = request.POST.get("prompt") or ""
    conversation_id = request.POST.get("conversation_id") or None
    page_context = _posted_page_context(request)
    next_url = safe_next_url(request, request.POST.get("next"), default=reverse("chat"))
    try:
        conversation_id = _send_conversation_id(conversation_id, wants_json)
        attachment = _posted_attachment(request, person)
        conversation = send_message(
            person, prompt, conversation_id=conversation_id, page_context=page_context,
            start_new=wants_json and conversation_id is None, attachment=attachment,
            warned_provider=request.POST.get("attachment_provider") or "",
        )
    except (PermissionDenied, AiError, AttachmentRejected) as exc:
        return _chat_send_error(request, exc, next_url, wants_json)
    if wants_json:
        return _popover_response(person, conversation, request)
    if next_url.rstrip("/").endswith("chat") or next_url.startswith(reverse("chat")):
        return redirect(f"{reverse('chat')}?c={conversation.pk}{_latest_question_anchor(conversation)}")
    return redirect(next_url)


def _latest_question_anchor(conversation):
    """Land on the question just sent, with its reply below it, even without JavaScript (#686)."""
    question = (
        conversation.messages.filter(role=AiConversationMessage.Role.USER)
        .order_by("-created_at", "-pk").values_list("pk", flat=True).first()
    )
    return f"#turn-{question}" if question else ""


def _posted_attachment(request, person):
    """The parsed file sent with a question, or None. A refused file raises AttachmentRejected."""
    upload = request.FILES.get("attachment")
    if upload is None:
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > DROPPED_UPLOAD_BYTES:
            raise AttachmentRejected(TOO_LARGE)
        return None
    # Parse only for a member who may chat now; send_message checks the same again.
    chat_backend_for(person)
    return parse_upload(upload)


def _posted_page_context(request):
    raw = request.POST.get("tile_context") or ""
    if len(raw) > MAX_PAGE_CONTEXT_CHARS:
        return {}
    return {
        "tile": _posted_tile(raw),
        "route": request.POST.get("page_route") or "",
        "query": request.POST.get("page_query") or "",
    }


def _posted_tile(raw):
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _parse_conversation_id(raw_id):
    conversation_id = int(raw_id)
    if not 0 < conversation_id <= 2**63 - 1:
        raise ValueError
    return conversation_id


def _send_conversation_id(raw_id, wants_json):
    if not raw_id:
        return None
    try:
        return _parse_conversation_id(raw_id) if wants_json else int(raw_id)
    except (TypeError, ValueError):
        if wants_json:
            raise PermissionDenied("That conversation is not available.") from None
        return None


def _chat_send_error(request, exc, next_url, wants_json):
    denied = isinstance(exc, PermissionDenied)
    error = "That conversation is not available." if denied else str(exc)
    if wants_json:
        return JsonResponse({"ok": False, "error": error}, status=404 if denied else 409)
    messages.error(request, error)
    if denied or "chat" in next_url:
        return redirect("chat")
    return redirect(next_url)


def _popover_response(person, conversation, request):
    context = _chat_context(person, conversation, request)
    context["popover"] = True
    return JsonResponse({
        "ok": True,
        "conversationId": conversation.pk if conversation else None,
        "html": render_to_string("finance/_chat_messages.html", context, request=request),
        "messageCount": len(context["chat_messages"]),
        "providerLabel": context["chat_provider_label"],
        "providerKey": context["chat_provider_key"],
    }, headers={"Cache-Control": "no-store"})


@require_GET
def chat_popover(request):
    """Restore only the current member's unexpired conversation; never pick a fallback."""
    person = _person(request)
    if not may_use_ai(person) or not member_has_ai(person):
        return JsonResponse({"ok": False}, status=403)
    raw_id = request.GET.get("c")
    conversation = None
    if raw_id:
        try:
            conversation_id = _parse_conversation_id(raw_id)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False}, status=404)
        conversation = conversations_for(person).filter(pk=conversation_id).first()
        if conversation is None:
            return JsonResponse({"ok": False}, status=404)
    return _popover_response(person, conversation, request)


@post_only("chat")
def chat_delete(request, conversation_id):
    person = _person(request)
    service_or_404(lambda: delete_conversation(person, conversation_id))
    messages.success(request, "Conversation deleted.")
    return redirect("chat")


@post_only("chat")
def chat_attachment_remove(request, attachment_id):
    person = _person(request)
    conversation_id = service_or_404(lambda: remove_attachment(person, attachment_id))
    messages.success(request, "Attachment removed.")
    return redirect(f"{reverse('chat')}?c={conversation_id}")


@post_only("chat")
def chat_delete_all(request):
    person = _person(request)
    delete_all_conversations(person)
    messages.success(request, "All conversations were deleted.")
    return redirect("chat")


@post_only("chat")
def chat_new(request):
    person = _person(request)
    try:
        conversation = start_conversation(person)
    except AiError as exc:
        messages.error(request, str(exc))
        return redirect("chat")
    return redirect(f"{reverse('chat')}?c={conversation.pk}")


def _back_to_chat(proposal):
    return redirect(f"{reverse('chat')}?c={proposal.conversation_id}#proposal-{proposal.pk}")


def _ticked_items(request):
    """The item indexes ticked on an item card (#659), or None when the form has no checkboxes."""
    if request.POST.get("item_selection") != "1":
        return None
    ticked = set()
    for value in request.POST.getlist("item"):
        try:
            ticked.add(int(value))
        except ValueError:
            continue
    return ticked


def _applied_message(proposal):
    result = proposal.result or {}
    if "items" not in result:
        return "Suggestion applied."
    count = result.get("count") or 0
    failed = sum(1 for item in result["items"] if item.get("status") in (ITEM_FAILED, ITEM_STALE))
    text = f"Applied {count} item{'s' if count != 1 else ''}."
    if failed:
        text += " Some ticked items could not be applied; the card shows why."
    return text


@post_only("chat")
def chat_proposal_apply(request, proposal_id):
    person = _person(request)
    proposal = proposal_for(person, proposal_id)
    if proposal is None:
        raise Http404
    try:
        applied = apply_proposal(person, proposal_id, selected=_ticked_items(request))
        messages.success(request, _applied_message(applied))
    except ProposalError as exc:
        messages.error(request, str(exc))
    except PermissionDenied:
        messages.error(request, "That suggestion is not available.")
        return redirect("chat")
    return _back_to_chat(proposal)


@post_only("chat")
def chat_proposal_dismiss(request, proposal_id):
    person = _person(request)
    proposal = proposal_for(person, proposal_id)
    if proposal is None:
        raise Http404
    try:
        dismiss_proposal(person, proposal_id)
    except ProposalError as exc:
        messages.error(request, str(exc))
    except PermissionDenied:
        messages.error(request, "That suggestion is not available.")
        return redirect("chat")
    return _back_to_chat(proposal)


@post_only("chat")
def chat_warm(request):
    person = _person(request)
    if not member_has_ai(person):
        return JsonResponse({"ok": False, "error": "Connect an AI backend first."}, status=403)
    connection, backend = resolve_ai(person, use_chat=True)
    shared = bool(connection and connection.owner_id != person.id and backend == LOCAL_BACKEND)
    if not shared and not chat_local_enabled(connection):
        return JsonResponse({"ok": False, "error": "Chat on the local model is not offered yet."}, status=409)
    try:
        rows = warm_for_chat(person)
    except AiError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    payload = _status_payload(rows)
    payload["ok"] = True
    return JsonResponse(payload)


@require_GET
def chat_status(request):
    person = _person(request)
    if not member_has_ai(person):
        return JsonResponse({"ok": False}, status=403)
    try:
        rows = local_status(person)
    except AiError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    payload = _status_payload(rows)
    payload["ok"] = True
    return JsonResponse(payload)


@require_GET
def chat_turn(request, turn_id):
    """Where a pending chat turn stands, for the page to swap in the reply without a reload."""
    person = _person(request)
    turn = (
        AiConversationMessage.objects.visible_to(person)
        .filter(
            pk=turn_id,
            conversation__in=conversations_for(person),
            role__in=(AiConversationMessage.Role.ASSISTANT, AiConversationMessage.Role.ERROR),
        )
        .first()
    )
    if turn is None:
        return JsonResponse({"ok": False}, status=404)
    if turn.status == AiConversationMessage.Status.PENDING and recover_stale_turns(pk=turn.pk):
        # The runner died or never picked it up: end it here so the page stops waiting.
        turn.refresh_from_db()
    payload = {"ok": True, "status": turn.status}
    proposals = turn.proposals.count() if turn.status != AiConversationMessage.Status.PENDING else 0
    if turn.status != AiConversationMessage.Status.PENDING:
        payload.update(
            {
                "role": turn.role,
                "content": turn.content,
                # The same markup the saved turn renders; error turns stay plain text (#634).
                "content_html": (
                    chat_markdown.render(turn.content) if turn.role == AiConversationMessage.Role.ASSISTANT else None
                ),
                "figures": [
                    {
                        "url": _local_url(item.get("url")),
                        "label": str(item.get("label") or ""),
                        "amount_display": str(item.get("amount_display") or ""),
                    }
                    for item in turn.figures or ()
                    if isinstance(item, dict)
                ],
                # The same include the saved turn renders, so a live chart matches it (#646).
                "charts_html": (
                    render_to_string("finance/_chat_charts.html", {"charts": turn.charts}, request=request)
                    if turn.charts else ""
                ),
                "notices": [str(item) for item in turn.notices or ()],
                "proposals": proposals,
            }
        )
    return JsonResponse(payload)


def _local_url(value):
    url = str(value or "")
    return url if url.startswith("/") and not url.startswith("//") else ""


def _status_payload(rows):
    row = rows[0] if rows else None
    state = row.state if row else "unreachable"
    sleeping = state in {"sleeping", "unloaded", "paused", "unreachable"}
    return {
        "state": state,
        "waking_seconds": row.waking_seconds if row else 0,
        "asleep": sleeping,
        "ready": state == "ready",
        "loading": state == "waking",
    }


def _chat_context(person, conversation, request):
    connection, backend = resolve_ai(person, use_chat=True)
    show_local = bool(connection and backend == LOCAL_BACKEND and (
        connection.owner_id != person.id or chat_local_enabled(connection)
    ))
    messages_qs = []
    if conversation is not None:
        messages_qs = list(reversed(list(conversation.messages.order_by("-created_at", "-pk")
                                         .prefetch_related("proposals")[:MAX_CHAT_MESSAGES])))
        for item in messages_qs:
            item.cards = [card_for(person, proposal) for proposal in item.proposals.all()]
    by_message = defaultdict(list)
    for attachment in attachments_for(conversation):
        by_message[attachment["message_id"]].append(attachment)
    for item in messages_qs:
        item.attachment_list = by_message.get(item.pk, [])
    return {
        "chat_ready": bool(member_has_ai(person) and connection is not None and backend),
        "chat_backend": backend,
        "chat_backend_label": _backend_label(backend),
        "chat_provider_label": provider_label(person, connection, backend),
        "chat_provider_key": provider_key(connection, backend),
        # Owner decision 5: after a switch or reconnect, name the new provider before it reads
        # the conversation's files. Sending from this page records that it was shown.
        "chat_attachment_switch": provider_needs_warning(conversation, provider_key(connection, backend)),
        "chat_attachment_accept": ATTACHMENT_ACCEPT,
        "chat_attachment_limit": max_per_conversation(),
        "chat_show_local": show_local,
        "conversation": conversation,
        "chat_messages": messages_qs,
        "conversations": conversations_for(person),
        "page_context": page_context_from_request(request),
        "assistant_roles": (AiConversationMessage.Role.ASSISTANT, AiConversationMessage.Role.ERROR),
    }
