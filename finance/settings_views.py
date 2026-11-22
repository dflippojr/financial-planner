"""Account settings, sessions, alerts, data, AI settings, export, leaving a household, deleting my data and the privacy policy."""

from decimal import Decimal

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_not_required  # pyright: ignore[reportAttributeAccessIssue] - pyright's Django stubs lack login_not_required (passkey_views is baselined for the same)
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction as database_transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .backup_health import settings_backup_context
from .post_only import post_only
from .reconciliation import settings_reconciliation_context
from .forms import (
    AlertSettingsForm,
    AlertEmailSettingsForm,
    AlertNtfySettingsForm,
    DeleteMyDataForm,
    PasswordPairForm,
)
from .google_auth import (
    has_google_account,
)
from .lifecycle_services import (
    delete_member_data,
    last_household_member,
    leave_household,
    lent_household_accounts,
    member_deletion_counts,
)
from .reauth import (
    ACCOUNT_SETTINGS_ACTIONS,
    reauth_redirect,
    recent_auth_is_fresh,
    requires_recent_auth,
    safe_next_url,
)
from .audit_services import record_download
from .security_services import (
    EVENT_TYPES,
    active_sessions_for,
    events_for,
    record_security_event,
    revoke_other_sessions_for,
    revoke_session_for,
)
from .export import export_filename, write_export_zip
from .audit_services import append_event
from .models import (
    AuditEvent,
    Person,
    PrivacyPolicyVersion,
)
from .ai.views import ai_settings_context
from .policy_services import (
    accept_shown_version,
    current_policy,
    in_acceptance,
    latest_acceptance,
    decline_shown_version,
    dismiss_shown_version,
)
from .alert_services import save_alert_settings, settings_for
from .auth_views import account_add_password, account_connect_google, account_disconnect_google, account_remove_password


def _alert_settings_form(person, data=None):
    prefs = settings_for(person)
    amount = None
    if prefs.large_transaction_minor:
        amount = Decimal(prefs.large_transaction_minor) / Decimal(100)
    return AlertSettingsForm(
        data,
        initial={
            "sync_enabled": prefs.sync_enabled,
            "recurring_price_enabled": prefs.recurring_price_enabled,
            "recurring_price_percent": prefs.recurring_price_percent,
            "recurring_missed_enabled": prefs.recurring_missed_enabled,
            "budget_enabled": prefs.budget_enabled,
            "large_transaction_enabled": prefs.large_transaction_enabled,
            "monthly_review_enabled": prefs.monthly_review_enabled,
            "monthly_review_ai_enabled": prefs.monthly_review_ai_enabled,
            "expected_balance_enabled": prefs.expected_balance_enabled,
            "import_due_enabled": prefs.import_due_enabled,
            "fund_enabled": prefs.fund_enabled,
            "unusual_spending_enabled": prefs.unusual_spending_enabled,
            "unusual_spending_ai_enabled": prefs.unusual_spending_ai_enabled,
            "unusual_category_percent": prefs.unusual_category_percent,
            "unusual_category_amount": Decimal(prefs.unusual_category_floor_minor) / Decimal(100),
            "large_transaction_amount": amount,
        },
    )



@never_cache
@requires_recent_auth("account-settings", action_from_post=ACCOUNT_SETTINGS_ACTIONS, form_url_name="account-settings")
def account_settings(request):
    password_form = PasswordPairForm()
    password_form.existing_user = request.user
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "connect-google":
            return account_connect_google(request)
        if action == "disconnect-google":
            error = account_disconnect_google(request.user)
        elif action == "add-password":
            password_form = PasswordPairForm(request.POST)
            password_form.existing_user = request.user
            password_form, error = account_add_password(request, password_form)
        elif action == "remove-password":
            error = account_remove_password(request)
    person = getattr(request.user, "person", None)
    current_key = request.session.session_key
    session_rows = []
    for row in active_sessions_for(request.user):
        session_rows.append(
            {
                "id": row.pk,
                "ip_address": row.ip_address,
                "user_agent": row.user_agent,
                "last_activity_at": row.last_activity_at,
                "is_current": bool(current_key) and row.session_key == current_key,
            }
        )
    return render(
        request,
        "finance/account_settings.html",
        {
            "password_form": password_form,
            "has_google": has_google_account(request.user),
            "has_password": request.user.has_usable_password(),
            "error": error,
            "security_events": list(events_for(person)[:50]),
            "member_sessions": session_rows,
            "passkeys": list(person.passkeys.visible_to(person).order_by("-created_at", "-pk"))
            if person is not None
            else [],
            "require_passkey_after_password": bool(person and person.require_passkey_after_password),
        },
    )



@post_only("account-settings")
@never_cache
@requires_recent_auth("revoke-session", form_url_name="account-settings")
def revoke_session(request, session_id):
    was_current = revoke_session_for(
        request.user,
        session_id,
        current_session_key=request.session.session_key,
    )
    if was_current:
        logout(request)
        return redirect("login")
    return redirect("account-settings")



@post_only("account-settings")
@never_cache
@requires_recent_auth("revoke-other-sessions", form_url_name="account-settings")
def revoke_other_sessions(request):
    revoke_other_sessions_for(
        request.user,
        request.session.session_key,
        session=request.session,
    )
    return redirect("account-settings")



def _save_alert_email_form(request, prefs, form):
    if not form.is_valid():
        return None
    address = form.cleaned_data["notification_email"]
    if address != prefs.notification_email and not recent_auth_is_fresh(request):
        return reauth_redirect(request, "alert-email-address", reverse("settings-alerts"))
    enabled = form.cleaned_data["email_enabled"]
    changed = [name for name, old, new in (
        ("email_enabled", prefs.email_enabled, enabled),
        ("notification_email", prefs.notification_email, address),
    ) if old != new]
    with database_transaction.atomic():
        prefs.email_enabled = enabled
        prefs.notification_email = address
        prefs.save(update_fields=("email_enabled", "notification_email"))
        if changed:
            append_event(action=AuditEvent.Action.NOTIFICATION_ADDRESS_CHANGED, actor=prefs.person,
                         target_id=prefs.pk, changed_fields=changed)
    return redirect("settings-alerts")



def _save_alert_ntfy_form(request, prefs, form):
    if not form.is_valid():
        return None
    topic = form.cleaned_data["ntfy_topic"]
    if topic != prefs.ntfy_topic and not recent_auth_is_fresh(request):
        return reauth_redirect(request, "alert-ntfy-topic", reverse("settings-alerts"))
    enabled = form.cleaned_data["ntfy_enabled"]
    changed = [name for name, old, new in (
        ("ntfy_enabled", prefs.ntfy_enabled, enabled),
        ("ntfy_topic", prefs.ntfy_topic, topic),
    ) if old != new]
    with database_transaction.atomic():
        prefs.ntfy_enabled = enabled
        prefs.ntfy_topic = topic
        prefs.save(update_fields=("ntfy_enabled", "ntfy_topic"))
        if changed:
            append_event(action=AuditEvent.Action.NOTIFICATION_ADDRESS_CHANGED, actor=prefs.person,
                         target_id=prefs.pk, changed_fields=changed)
    return redirect("settings-alerts")



def _ntfy_settings_post(request, prefs, action):
    """Returns (response, form, notice) for the ntfy save and test actions."""
    from .alert_ntfy import ntfy_notices_available, send_test_ntfy

    if not ntfy_notices_available():
        return redirect("settings-alerts"), None, None
    if action == "test-ntfy":
        notice = ("Test push sent." if send_test_ntfy(prefs)
                  else "Test push could not be sent. Check your saved topic or ask the operator.")
        return None, None, notice
    form = AlertNtfySettingsForm(request.POST)
    return _save_alert_ntfy_form(request, prefs, form), form, None



@require_http_methods(["GET", "HEAD", "POST"])
@never_cache
def settings_alerts(request):
    from .alert_email import email_notices_available, send_test_notice
    from .alert_ntfy import ntfy_notices_available

    person = get_object_or_404(Person, user=request.user)
    prefs = settings_for(person)
    form = _alert_settings_form(person)
    available = email_notices_available()
    email_form = AlertEmailSettingsForm(initial={
        "email_enabled": prefs.email_enabled,
        "notification_email": prefs.notification_email,
    })
    ntfy_form = AlertNtfySettingsForm(initial={"ntfy_enabled": prefs.ntfy_enabled, "ntfy_topic": prefs.ntfy_topic})
    notice = ntfy_notice = None
    action = request.POST.get("action", "")
    if request.method == "POST" and action in ("save-ntfy", "test-ntfy"):
        response, posted_form, ntfy_notice = _ntfy_settings_post(request, prefs, action)
        if response is not None:
            return response
        ntfy_form = posted_form or ntfy_form
    elif request.method == "POST" and action in ("save-email", "test-email"):
        if not available:
            return redirect("settings-alerts")
        if action == "test-email":
            notice = ("Test notice sent." if send_test_notice(prefs)
                      else "Test notice could not be sent. Check your saved settings or ask the operator.")
        else:
            email_form = AlertEmailSettingsForm(request.POST)
            response = _save_alert_email_form(request, prefs, email_form)
            if response is not None:
                return response
    elif request.method == "POST":
        form = _alert_settings_form(person, request.POST)
        if form.is_valid():
            save_alert_settings(person, **form.save_payload())
            return redirect("settings-alerts")
    return render(request, "finance/settings_alerts.html", {
        "alert_settings_form": form, "email_notices_available": available,
        "email_settings_form": email_form, "email_notice": notice,
        "ntfy_notices_available": ntfy_notices_available(), "ntfy_settings_form": ntfy_form,
        "ntfy_notice": ntfy_notice,
    })



@require_safe
@never_cache
def settings_data(request):
    person = get_object_or_404(Person, user=request.user)
    context = {**settings_backup_context(person), **settings_reconciliation_context(person)}
    return render(request, "finance/settings_data.html", context)



@never_cache
def settings_ai(request):
    person = getattr(request.user, "person", None)
    if request.method == "POST" and request.POST.get("action") == "accept-privacy-policy":
        if person is not None:
            accept_shown_version(person, request.POST.get("version"), request=request)
    policy = current_policy()
    acceptance = latest_acceptance(person) if person is not None else None
    return render(
        request,
        "finance/settings_ai.html",
        {
            "privacy_policy": policy,
            "privacy_in_acceptance": in_acceptance(person) if person is not None else False,
            "privacy_acceptance": acceptance,
            **ai_settings_context(person),
        },
    )



def _account_export_zip(request):
    person = get_object_or_404(Person, user=request.user)
    payload = write_export_zip(person)
    filename = export_filename(timezone.localdate())
    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response



@post_only("settings-data")
@never_cache
@requires_recent_auth("export-data", form_url_name="settings-data")
def account_export(request):
    response = _account_export_zip(request)
    record_download(request.user, export_kind="data_zip")
    record_security_event(request.user, EVENT_TYPES.MEMBER_DATA_EXPORT, request=request)
    return response



@post_only("invite")
@never_cache
@requires_recent_auth("leave-household", form_url_name="invite")
def leave_household_view(request):
    try:
        leave_household(request.user)
    except PermissionDenied:
        raise Http404() from None
    return redirect("home")



def _delete_my_data_context(request, person, form):
    last_member = last_household_member(person)
    lent_accounts = lent_household_accounts(person)
    if form is None:
        form = DeleteMyDataForm(
            username=request.user.username,
            lent_accounts=lent_accounts,
            last_member=last_member,
        )
    return {
        "form": form,
        "lent_accounts": lent_accounts,
        "last_member": last_member,
        **member_deletion_counts(person),
    }



@require_http_methods(["GET", "POST"])
@never_cache
@requires_recent_auth("delete-my-data", form_url_name="delete-my-data")
def delete_my_data(request):
    person = get_object_or_404(Person, user=request.user)
    last_member = last_household_member(person)
    lent_accounts = lent_household_accounts(person)
    form = DeleteMyDataForm(
        request.POST or None,
        username=request.user.username,
        lent_accounts=lent_accounts,
        last_member=last_member,
    )
    if request.method == "POST" and form.is_valid():
        try:
            delete_member_data(request.user, form.lent_choices())
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            logout(request)
            return redirect("member-data-deleted")
    return render(request, "finance/delete_my_data.html", _delete_my_data_context(request, person, form))



@login_not_required
@require_safe
@never_cache
def member_data_deleted(request):
    return render(
        request,
        "finance/member_data_deleted.html",
        {"auth_card_layout": True},
    )



@login_not_required
@require_safe
@never_cache
def privacy_policy(request, version=None):
    if version is None:
        policy = current_policy()
    else:
        policy = get_object_or_404(PrivacyPolicyVersion, version=version)
    current = current_policy()
    return render(
        request,
        "finance/privacy_policy.html",
        {
            "privacy_policy": policy,
            "wide_card": True,
            "is_current_policy": policy.pk == current.pk,  # pyright: ignore[reportOptionalMemberAccess] - current_policy() is None only before a policy is seeded
        },
    )



@post_only("privacy-policy")
@never_cache
def privacy_policy_respond(request):
    person = get_object_or_404(Person, user=request.user)
    action = request.POST.get("action")
    wants_json = "application/json" in request.headers.get("Accept", "")
    if action == "accept":
        responded = accept_shown_version(person, request.POST.get("version"), request=request)
    elif action == "decline":
        responded = decline_shown_version(person, request.POST.get("version"))
    elif action == "dismiss":
        responded = dismiss_shown_version(person, request.POST.get("version"))
    else:
        if wants_json:
            return JsonResponse({"error": "Choose Accept, Deny or Not now."}, status=400)
        return redirect(safe_next_url(request, request.POST.get("next", "")))
    if not responded:
        if wants_json:
            return JsonResponse({"error": "The policy has changed. Read the current policy before responding.",
                                 "policy_url": reverse("privacy-policy")}, status=409)
        return redirect("privacy-policy")
    if wants_json:
        # Middleware may already have cached the reverse one-to-one relation.
        request.user.person = person
        from .context_processors import ai_consent_context

        context = ai_consent_context(request, return_url=safe_next_url(request, request.POST.get("next", "")))
        return JsonResponse({
            "ai_slot": context["ai_slot"],
            "slot_html": render_to_string("finance/_ai_slot.html", context, request=request),
            "dialog_html": render_to_string("finance/_ai_consent_dialog.html", context, request=request),
        })
    return redirect(safe_next_url(request, request.POST.get("next", "")))
