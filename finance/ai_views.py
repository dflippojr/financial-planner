"""Settings AI connection views."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .ai_api import label as api_label, default_model
from .ai_services import (
    AiError,
    api_connection,
    api_connections,
    api_usage_summary,
    connect_api_key,
    disconnect_api_key,
    set_api_defaults,
    connect_harness,
    connection_for,
    disconnect_harness,
    discovered_backends,
    offered_local_connection,
    set_defaults,
    set_offer_local_chat,
    set_offer_local_to_household,
    set_shared_local_use,
)
from .ai_types import API_KINDS, SHARED_LOCAL_CHOICE
from .ai_urls import HarnessUrlError
from .forms import (
    AiDefaultsForm,
    AiOfferLocalChatForm,
    AiOfferLocalForm,
    AiSharedLocalForm,
    ApiKeyConnectForm,
    ApiKeyDefaultsForm,
    HarnessConnectForm,
)
from .chat_services import chat_local_enabled
from .models import AiUsageEvent, Person
from .policy_services import may_use_ai
from .reauth import requires_recent_auth
from .security_services import EVENT_TYPES, record_security_event


def ai_settings_context(person):
    if person is None or not may_use_ai(person):
        return {
            "show_ai_settings": False,
            "ai_connected": False,
            "ai_backends": [],
            "ai_connect_form": HarnessConnectForm(),
            "ai_defaults_form": None,
            "ai_offer_form": None,
            "ai_offer_chat_form": None,
            "ai_shared_local_form": None,
            "ai_shared_local_offered": False,
            "ai_usage": [],
            "ai_error": "",
            "ai_key_connect_form": None,
            "ai_key_cards": [],
        }
    connection = connection_for(person)
    error = ""
    backends = []
    defaults_form = None
    offer_form = None
    offer_chat_form = None
    shared_form = None
    shared_offered = offered_local_connection(person)
    if connection is not None:
        try:
            backends = discovered_backends(person)
        except AiError as exc:
            error = str(exc)
            backends = []
        chat_initial = SHARED_LOCAL_CHOICE if person.use_shared_local_chat else connection.chat_backend
        background_initial = (
            SHARED_LOCAL_CHOICE if person.use_shared_local_background else connection.background_backend
        )
        defaults_form = AiDefaultsForm(
            backends=backends,
            allow_local_chat=chat_local_enabled(connection),
            offer_shared_local=shared_offered is not None,
            initial={
                "chat_backend": chat_initial,
                "background_backend": background_initial,
                "chat_model": connection.chat_model,
                "background_model": connection.background_model,
            },
        )
        offer_form = AiOfferLocalForm(
            initial={"offer_local_to_household": connection.offer_local_to_household}
        )
        offer_chat_form = AiOfferLocalChatForm(initial={"offer_local_chat": connection.offer_local_chat})
    elif shared_offered is not None:
        shared_form = AiSharedLocalForm(
            initial={
                "use_shared_local_chat": person.use_shared_local_chat,
                "use_shared_local_background": person.use_shared_local_background,
            }
        )
    return {
        "show_ai_settings": True,
        "ai_connected": connection is not None,
        "ai_connection": connection,
        "ai_backends": backends,
        "ai_connect_form": HarnessConnectForm(),
        "ai_defaults_form": defaults_form,
        "ai_offer_form": offer_form,
        "ai_offer_chat_form": offer_chat_form,
        "ai_shared_local_form": shared_form,
        "ai_shared_local_offered": shared_offered is not None,
        "ai_usage": list(AiUsageEvent.objects.visible_to(person).order_by("-created_at", "-pk")[:20]),
        "ai_error": error,
        "ai_key_connect_form": ApiKeyConnectForm(),
        "ai_key_cards": _key_cards(person),
    }


def _key_cards(person):
    tokens = api_usage_summary(person)
    cards = []
    for row in api_connections(person):
        cards.append(
            {
                "kind": row.kind,
                "label": api_label(row.kind),
                "connected_at": row.connected_at,
                "month_tokens": tokens.get(row.kind, 0),
                "form": ApiKeyDefaultsForm(
                    kind=row.kind,
                    initial={
                        "chat_model": row.chat_model or default_model(row.kind, use_chat=True),
                        "background_model": row.background_model or default_model(row.kind, use_chat=False),
                        "use_for_chat": row.use_for_chat,
                        "use_for_background": row.use_for_background,
                    },
                ),
            }
        )
    return cards


@require_POST
@requires_recent_auth("connect-ai", form_url_name="settings-ai")
def ai_connect(request):
    person = get_object_or_404(Person, user=request.user)
    form = HarnessConnectForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a harness URL and an App token.")
        return redirect("settings-ai")
    try:
        connect_harness(
            person,
            base_url=form.cleaned_data["base_url"],
            token=form.cleaned_data["token"],
        )
        record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
        messages.success(request, "Agent Harness is connected.")
    except (AiError, HarnessUrlError) as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("disconnect-ai", form_url_name="settings-ai")
def ai_disconnect(request):
    person = get_object_or_404(Person, user=request.user)
    disconnect_harness(person)
    record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
    messages.success(request, "The AI connection was removed.")
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("ai-defaults", form_url_name="settings-ai")
def ai_save_defaults(request):
    person = get_object_or_404(Person, user=request.user)
    try:
        backends = discovered_backends(person)
    except AiError as exc:
        messages.error(request, str(exc))
        return redirect("settings-ai")
    form = AiDefaultsForm(
        request.POST,
        backends=backends,
        allow_local_chat=chat_local_enabled(connection_for(person)),
        offer_shared_local=offered_local_connection(person) is not None,
    )
    if not form.is_valid():
        messages.error(request, "Choose available backends for chat and background jobs.")
        return redirect("settings-ai")
    try:
        set_defaults(
            person,
            chat_backend=form.cleaned_data["chat_backend"],
            background_backend=form.cleaned_data["background_backend"],
            chat_model=form.cleaned_data.get("chat_model") or "",
            background_model=form.cleaned_data.get("background_model") or "",
        )
        record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
        messages.success(request, "AI backend defaults were saved.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("ai-offer-local", form_url_name="settings-ai")
def ai_save_offer_local(request):
    person = get_object_or_404(Person, user=request.user)
    form = AiOfferLocalForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose whether to offer the local model.")
        return redirect("settings-ai")
    try:
        set_offer_local_to_household(person, form.cleaned_data["offer_local_to_household"])
        messages.success(request, "Household local-model sharing was saved.")
    except PermissionDenied:
        messages.error(request, "Only the connection owner can offer the local model.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("ai-offer-local-chat", form_url_name="settings-ai")
def ai_save_offer_local_chat(request):
    person = get_object_or_404(Person, user=request.user)
    form = AiOfferLocalChatForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose whether to offer the local model for chat.")
        return redirect("settings-ai")
    try:
        set_offer_local_chat(person, form.cleaned_data["offer_local_chat"])
        messages.success(request, "Local-model chat setting was saved.")
    except PermissionDenied:
        messages.error(request, "Only the connection owner can offer the local model for chat.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("ai-shared-local", form_url_name="settings-ai")
def ai_save_shared_local(request):
    person = get_object_or_404(Person, user=request.user)
    form = AiSharedLocalForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose how to use the household local model.")
        return redirect("settings-ai")
    try:
        set_shared_local_use(
            person,
            chat=form.cleaned_data["use_shared_local_chat"],
            background=form.cleaned_data["use_shared_local_background"],
        )
        messages.success(request, "Household local-model settings were saved.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("connect-ai-key", form_url_name="settings-ai")
def ai_key_connect(request):
    person = get_object_or_404(Person, user=request.user)
    form = ApiKeyConnectForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a provider and paste its API key.")
        return redirect("settings-ai")
    try:
        connect_api_key(person, kind=form.cleaned_data["provider"], key=form.cleaned_data["key"])
        record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
        messages.success(request, f"{api_label(form.cleaned_data['provider'])} key saved. It will not be shown again.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("ai-key-defaults", form_url_name="settings-ai")
def ai_key_defaults(request, kind):
    person = get_object_or_404(Person, user=request.user)
    if kind not in API_KINDS or api_connection(person, kind) is None:
        messages.error(request, "That provider is not connected.")
        return redirect("settings-ai")
    form = ApiKeyDefaultsForm(request.POST, kind=kind)
    if not form.is_valid():
        messages.error(request, "Choose models from the list.")
        return redirect("settings-ai")
    try:
        set_api_defaults(
            person,
            kind=kind,
            chat_model=form.cleaned_data["chat_model"],
            background_model=form.cleaned_data["background_model"],
            use_chat=form.cleaned_data["use_for_chat"],
            use_background=form.cleaned_data["use_for_background"],
        )
        record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
        messages.success(request, f"{api_label(kind)} settings were saved.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("disconnect-ai-key", form_url_name="settings-ai")
def ai_key_disconnect(request, kind):
    person = get_object_or_404(Person, user=request.user)
    if kind not in API_KINDS:
        messages.error(request, "That provider is not connected.")
        return redirect("settings-ai")
    disconnect_api_key(person, kind)
    record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
    messages.success(request, f"{api_label(kind)} key removed.")
    return redirect("settings-ai")
