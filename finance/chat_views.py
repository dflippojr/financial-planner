"""Chat page, drawer, and local-model warm endpoints."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .access import request_person as _person
from .ai_services import AiError, local_status, member_has_ai, resolve_ai, warm_for_chat
from .ai_types import LOCAL_BACKEND
from .chat_proposals import ProposalError, apply_proposal, card_for, dismiss_proposal, proposal_for
from .chat_runner import recover_stale_turns
from .chat_services import (
    chat_local_enabled,
    conversations_for,
    delete_all_conversations,
    delete_conversation,
    page_context_from_request,
    sanitize_page_context,
    send_message,
    start_conversation,
)
from .input_limits import MAX_CHAT_MESSAGES
from .models import AiConversationMessage
from .reauth import safe_next_url


def _backend_label(backend):
    labels = {LOCAL_BACKEND: "Local model", "claude": "Claude", "codex": "Codex", "cursor": "Cursor"}
    return labels.get(backend, backend or "AI")


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


@require_POST
def chat_send(request):
    person = _person(request)
    prompt = request.POST.get("prompt") or ""
    conversation_id = request.POST.get("conversation_id") or None
    page_context = sanitize_page_context(
        {
            "route": request.POST.get("page_route") or "",
            "query": request.POST.get("page_query") or "",
        }
    )
    next_url = safe_next_url(request, request.POST.get("next"), default=reverse("chat"))
    if conversation_id:
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            conversation_id = None
    try:
        conversation = send_message(person, prompt, conversation_id=conversation_id, page_context=page_context)
    except PermissionDenied:
        messages.error(request, "That conversation is not available.")
        return redirect("chat")
    except AiError as exc:
        messages.error(request, str(exc))
        if "chat" in next_url:
            return redirect("chat")
        return redirect(next_url)
    if next_url.rstrip("/").endswith("chat") or next_url.startswith(reverse("chat")):
        return redirect(f"{reverse('chat')}?c={conversation.pk}")
    return redirect(next_url)


@require_POST
def chat_delete(request, conversation_id):
    person = _person(request)
    try:
        delete_conversation(person, conversation_id)
        messages.success(request, "Conversation deleted.")
    except PermissionDenied:
        messages.error(request, "That conversation is not available.")
    return redirect("chat")


@require_POST
def chat_delete_all(request):
    person = _person(request)
    delete_all_conversations(person)
    messages.success(request, "All conversations were deleted.")
    return redirect("chat")


@require_POST
def chat_new(request):
    person = _person(request)
    conversation = start_conversation(person)
    return redirect(f"{reverse('chat')}?c={conversation.pk}")


def _back_to_chat(proposal):
    return redirect(f"{reverse('chat')}?c={proposal.conversation_id}#proposal-{proposal.pk}")


@require_POST
def chat_proposal_apply(request, proposal_id):
    person = _person(request)
    proposal = proposal_for(person, proposal_id)
    if proposal is None:
        messages.error(request, "That suggestion is not available.")
        return redirect("chat")
    try:
        apply_proposal(person, proposal_id)
        messages.success(request, "Suggestion applied.")
    except ProposalError as exc:
        messages.error(request, str(exc))
    except PermissionDenied:
        messages.error(request, "That suggestion is not available.")
        return redirect("chat")
    return _back_to_chat(proposal)


@require_POST
def chat_proposal_dismiss(request, proposal_id):
    person = _person(request)
    proposal = proposal_for(person, proposal_id)
    if proposal is None:
        messages.error(request, "That suggestion is not available.")
        return redirect("chat")
    try:
        dismiss_proposal(person, proposal_id)
    except ProposalError as exc:
        messages.error(request, str(exc))
    except PermissionDenied:
        messages.error(request, "That suggestion is not available.")
        return redirect("chat")
    return _back_to_chat(proposal)


@require_POST
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
                "figures": [
                    {
                        "url": _local_url(item.get("url")),
                        "label": str(item.get("label") or ""),
                        "amount_display": str(item.get("amount_display") or ""),
                    }
                    for item in turn.figures or ()
                    if isinstance(item, dict)
                ],
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
    return {
        "chat_ready": bool(member_has_ai(person) and connection is not None and backend),
        "chat_backend": backend,
        "chat_backend_label": _backend_label(backend),
        "chat_show_local": show_local,
        "conversation": conversation,
        "chat_messages": messages_qs,
        "conversations": conversations_for(person),
        "page_context": page_context_from_request(request),
        "assistant_roles": (AiConversationMessage.Role.ASSISTANT, AiConversationMessage.Role.ERROR),
    }
