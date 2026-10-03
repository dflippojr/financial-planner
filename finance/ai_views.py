"""Settings AI connection views."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .ai_services import (
    AiError,
    connect_harness,
    connection_for,
    disconnect_harness,
    discovered_backends,
    set_defaults,
)
from .ai_urls import HarnessUrlError
from .forms import AiDefaultsForm, HarnessConnectForm
from .models import AiUsageEvent, Person
from .policy_services import may_use_ai
from .reauth import requires_recent_auth


def ai_settings_context(person):
    if person is None or not may_use_ai(person):
        return {
            "show_ai_settings": False,
            "ai_connected": False,
            "ai_backends": [],
            "ai_connect_form": HarnessConnectForm(),
            "ai_defaults_form": None,
            "ai_usage": [],
            "ai_error": "",
        }
    connection = connection_for(person)
    error = ""
    backends = []
    defaults_form = None
    if connection is not None:
        try:
            backends = discovered_backends(person)
        except AiError as exc:
            error = str(exc)
            backends = []
        defaults_form = AiDefaultsForm(
            backends=backends,
            initial={
                "chat_backend": connection.chat_backend,
                "background_backend": connection.background_backend,
                "chat_model": connection.chat_model,
                "background_model": connection.background_model,
            },
        )
    return {
        "show_ai_settings": True,
        "ai_connected": connection is not None,
        "ai_connection": connection,
        "ai_backends": backends,
        "ai_connect_form": HarnessConnectForm(),
        "ai_defaults_form": defaults_form,
            "ai_usage": list(AiUsageEvent.objects.visible_to(person).order_by("-created_at", "-pk")[:20]),
            "ai_error": error,
        }


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
        messages.success(request, "Agent Harness is connected.")
    except (AiError, HarnessUrlError) as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")


@require_POST
@requires_recent_auth("disconnect-ai", form_url_name="settings-ai")
def ai_disconnect(request):
    person = get_object_or_404(Person, user=request.user)
    disconnect_harness(person)
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
    form = AiDefaultsForm(request.POST, backends=backends)
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
        messages.success(request, "AI backend defaults were saved.")
    except AiError as exc:
        messages.error(request, str(exc))
    return redirect("settings-ai")
