"""Settings AI connection views."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .ai_api import label as api_label, default_model
from .ai_plan import (
    PlanLinkError,
    plan_cards,
    poll_login,
    set_offer_plan_links,
    start_login,
    submit_code,
    unlink,
)
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
from .ai_types import API_KINDS, PLAN_BACKENDS, SHARED_LOCAL_CHOICE
from .ai_urls import HarnessUrlError
from .forms import (
    AiDefaultsForm,
    AiOfferLocalChatForm,
    AiOfferLocalForm,
    AiOfferPlanLinksForm,
    AiSharedLocalForm,
    ApiKeyConnectForm,
    ApiKeyDefaultsForm,
    HarnessConnectForm,
)
from .chat_services import chat_local_enabled
from .models import AiUsageEvent, Person
from .policy_services import may_use_ai
from .reauth import recent_auth_is_fresh, reauth_redirect, requires_recent_auth
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
            "ai_plan_cards": [],
            "ai_offer_plan_form": None,
        }
    connection = connection_for(person)
    error = ""
    backends = []
    defaults_form = None
    offer_form = None
    offer_chat_form = None
    shared_form = None
    shared_offered = offered_local_connection(person)
    offer_plan_form = None
    if connection is not None:
        offer_plan_form = AiOfferPlanLinksForm(initial={"offer_plan_links": connection.offer_plan_links})
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
        "ai_plan_cards": plan_cards(person),
        "ai_offer_plan_form": offer_plan_form,
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


def _plan_backend(backend):
    if backend not in PLAN_BACKENDS:
        raise PlanLinkError("Choose Claude or Codex.")
    return backend


def _json_error(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


@require_POST
def ai_plan_start(request, backend):
    person = get_object_or_404(Person, user=request.user)
    if not recent_auth_is_fresh(request):
        # The popup's script cannot follow a redirect to the sign-in form, so hand it the address.
        target = reauth_redirect(request, "link-ai-plan", reverse("settings-ai"))["Location"]
        return JsonResponse({"ok": False, "reauth": target}, status=403)
    try:
        data = start_login(person, _plan_backend(backend))
    except PlanLinkError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True, **data})


@require_POST
def ai_plan_code(request, backend):
    person = get_object_or_404(Person, user=request.user)
    if not recent_auth_is_fresh(request):
        return _json_error("Sign in again to link your plan.", 403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        attempt_id = str(payload.get("attempt_id") or "")
        code = str(payload.get("code") or "")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return _json_error("Paste the code from the sign-in page.")
    try:
        submit_code(person, _plan_backend(backend), attempt_id=attempt_id, code=code)
    except PlanLinkError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True})


@require_GET
def ai_plan_status(request, backend):
    person = get_object_or_404(Person, user=request.user)
    try:
        state = poll_login(person, _plan_backend(backend), request=request)
    except PlanLinkError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True, **state})


@require_POST
@requires_recent_auth("unlink-ai-plan", form_url_name="settings-ai")
def ai_plan_unlink(request, backend):
    person = get_object_or_404(Person, user=request.user)
    try:
        unlink(person, _plan_backend(backend))
        record_security_event(person, EVENT_TYPES.AI_CONNECTION_CHANGED, request=request)
        messages.success(request, "Your plan was unlinked.")
    except PlanLinkError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("ai-offer-plan", form_url_name="settings-ai")
def ai_save_offer_plan(request):
    person = get_object_or_404(Person, user=request.user)
    form = AiOfferPlanLinksForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose whether to offer plan linking.")
        return redirect("settings-ai")
    try:
        set_offer_plan_links(person, form.cleaned_data["offer_plan_links"])
        messages.success(request, "Plan linking setting was saved.")
    except PermissionDenied:
        messages.error(request, "Only the connection owner can offer plan linking.")
    return redirect("settings-ai")
