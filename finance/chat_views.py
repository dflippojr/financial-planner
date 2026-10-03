"""Chat page, drawer, and local-model warm endpoints."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .ai_services import AiError, connection_for, local_status, member_has_ai, warm_for_chat
from .ai_types import LOCAL_BACKEND
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
from .models import AiConversationMessage, Person
from .reauth import safe_next_url


def _person(request):
    return get_object_or_404(Person, user=request.user)


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
    prompt = (request.POST.get("prompt") or "").strip()
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


@require_POST
def chat_warm(request):
    person = _person(request)
    if not member_has_ai(person):
        return JsonResponse({"ok": False, "error": "Connect an AI backend first."}, status=403)
    if not chat_local_enabled():
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
    connection = connection_for(person)
    backend = connection.chat_backend if connection else ""
    show_local = bool(connection and backend == LOCAL_BACKEND and chat_local_enabled())
    messages_qs = []
    if conversation is not None:
        messages_qs = list(conversation.messages.order_by("created_at", "pk"))
    return {
        "chat_ready": member_has_ai(person),
        "chat_backend": backend,
        "chat_backend_label": _backend_label(backend),
        "chat_show_local": show_local,
        "conversation": conversation,
        "chat_messages": messages_qs,
        "conversations": conversations_for(person),
        "page_context": page_context_from_request(request),
        "assistant_roles": (AiConversationMessage.Role.ASSISTANT, AiConversationMessage.Role.ERROR),
    }
