import json

from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .access import request_person as _person
from .auth_services import (
    InvalidOneTimeCode,
    clear_login_failures,
    complete_member_session,
    login_is_blocked,
    record_login_failure,
    throttle_key,
)
from .forms import RecoveryCodeOnlyForm
from .passkey_services import (
    PasskeyError,
    assert_passkey,
    authentication_options_json,
    clear_pending_passkey_login,
    complete_login_with_recovery_code,
    delete_passkey,
    passkeys_for,
    pending_passkey_next,
    pending_passkey_user,
    register_passkey,
    registration_options_json,
    set_require_passkey_after_password,
)
from .reauth import (
    recent_auth_is_fresh,
    requires_recent_auth,
    safe_next_url,
    stamp_recent_auth,
)
from .security_services import record_sign_in_failure_for_username

PASSKEY_FAILED = "Sign-in failed. Check your credentials and try again later."
REAUTH_FAILED = "Confirmation failed. Try again later."


def _json_body(request):
    try:
        payload = json.loads(request.body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _json_error(message, *, status=400, extra=None):
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=status)


@require_POST
@never_cache
def passkey_register_options(request):
    if not recent_auth_is_fresh(request):
        return _json_error("Re-authentication required.", status=403, extra={"reauth": True})
    return JsonResponse({"ok": True, "options": json.loads(registration_options_json(request, _person(request, related=True)))})


@require_POST
@never_cache
def passkey_register(request):
    if not recent_auth_is_fresh(request):
        return _json_error("Re-authentication required.", status=403, extra={"reauth": True})
    payload = _json_body(request)
    if payload is None or "credential" not in payload:
        return _json_error("Passkey registration failed.")
    try:
        passkey = register_passkey(
            request,
            _person(request, related=True),
            payload["credential"],
            name=payload.get("name", ""),
        )
    except PasskeyError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True, "id": passkey.pk, "name": passkey.name})


@require_POST
@never_cache
@requires_recent_auth("remove-passkey", form_url_name="account-settings")
def passkey_delete(request, passkey_id):
    delete_passkey(request.user, passkey_id)
    return redirect("account-settings")


@require_POST
@never_cache
@requires_recent_auth("require-passkey", form_url_name="account-settings")
def passkey_require(request):
    person = _person(request, related=True)
    try:
        set_require_passkey_after_password(person, request.POST.get("require_passkey") == "on")
    except PasskeyError:
        pass
    return redirect("account-settings")


@login_not_required
@never_cache
@require_http_methods(["GET", "POST"])
def passkey_sign_in(request):
    user = pending_passkey_user(request)
    if user is None or not hasattr(user, "person"):
        return redirect("login")
    form = RecoveryCodeOnlyForm(request.POST or None)
    error = None
    if request.method == "POST":
        key = throttle_key(user.username, request.META.get("REMOTE_ADDR"))
        if login_is_blocked(key):
            error = PASSKEY_FAILED
        elif form.is_valid():
            try:
                complete_login_with_recovery_code(user, form.cleaned_data["recovery_code"], request=request)
            except InvalidOneTimeCode:
                record_login_failure(key)
                record_sign_in_failure_for_username(user.username, request)
                error = PASSKEY_FAILED
            else:
                clear_login_failures(key)
                next_url = pending_passkey_next(request)
                clear_pending_passkey_login(request)
                complete_member_session(request, user)
                return redirect(safe_next_url(request, next_url))
        else:
            error = PASSKEY_FAILED
    return render(
        request,
        "finance/passkey_sign_in.html",
        {
            "form": form,
            "error": error,
            "auth_card_layout": True,
        },
    )


@login_not_required
@require_POST
@never_cache
def passkey_sign_in_options(request):
    user = pending_passkey_user(request)
    if user is None or not hasattr(user, "person"):
        return _json_error(PASSKEY_FAILED, status=403)
    key = throttle_key(user.username, request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        return _json_error(PASSKEY_FAILED, status=429)
    return JsonResponse({"ok": True, "options": json.loads(authentication_options_json(request, user.person))})


@login_not_required
@require_POST
@never_cache
def passkey_sign_in_assert(request):
    user = pending_passkey_user(request)
    if user is None or not hasattr(user, "person"):
        return _json_error(PASSKEY_FAILED, status=403)
    key = throttle_key(user.username, request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        return _json_error(PASSKEY_FAILED, status=429)
    payload = _json_body(request)
    if payload is None or "credential" not in payload:
        record_login_failure(key)
        record_sign_in_failure_for_username(user.username, request)
        return _json_error(PASSKEY_FAILED)
    try:
        assert_passkey(request, user.person, payload["credential"])
    except PasskeyError:
        record_login_failure(key)
        record_sign_in_failure_for_username(user.username, request)
        return _json_error(PASSKEY_FAILED)
    clear_login_failures(key)
    next_url = pending_passkey_next(request)
    clear_pending_passkey_login(request)
    complete_member_session(request, user)
    return JsonResponse({"ok": True, "redirect": safe_next_url(request, next_url)})


@require_POST
@never_cache
def passkey_reauth_options(request):
    person = _person(request, related=True)
    if not passkeys_for(person).exists():
        raise Http404()
    key = throttle_key(request.user.username, request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        return _json_error(REAUTH_FAILED, status=429)
    return JsonResponse({"ok": True, "options": json.loads(authentication_options_json(request, person))})


@require_POST
@never_cache
def passkey_reauth_assert(request):
    person = _person(request, related=True)
    if not passkeys_for(person).exists():
        raise Http404()
    key = throttle_key(request.user.username, request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        return _json_error(REAUTH_FAILED, status=429)
    payload = _json_body(request)
    if payload is None or "credential" not in payload:
        record_login_failure(key)
        return _json_error(REAUTH_FAILED)
    try:
        assert_passkey(request, person, payload["credential"])
    except PasskeyError:
        record_login_failure(key)
        return _json_error(REAUTH_FAILED)
    clear_login_failures(key)
    stamp_recent_auth(request)
    next_url = safe_next_url(request, payload.get("next") or request.GET.get("next", ""))
    return JsonResponse({"ok": True, "redirect": next_url})
