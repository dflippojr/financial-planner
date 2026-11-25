"""Sign-in, Google sign-in and linking, invitations, joining, first-run setup, re-auth and account recovery."""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import authenticate, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required  # pyright: ignore[reportAttributeAccessIssue] - pyright's Django stubs lack login_not_required (passkey_views is baselined for the same)
from django.db import transaction as database_transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .post_only import post_only
from .auth_services import (
    InvalidOneTimeCode,
    SETUP_THROTTLE_USERNAME,
    accept_invitation,
    clear_login_failures,
    complete_member_session,
    create_invitation,
    first_member_exists,
    invitation_is_usable,
    login_is_blocked,
    normalize_username,
    recover_account,
    record_login_failure,
    seed_first_household,
    setup_code_configured,
    setup_code_matches,
    throttle_key,
    validated_username,
)
from .forms import (
    JoinForm,
    JoinGoogleForm,
    LoginForm,
    PasswordPairForm,
    ReauthPasswordForm,
    RecoveryForm,
    SetupForm,
    SetupGoogleForm,
)
from .break_glass import break_glass_throttle_key, break_glass_token_is_usable, redeem_break_glass_token
from .google_auth import (
    GOOGLE_ONLY_NOTICE,
    disconnect_google_account,
    google_signin_enabled,
    google_throttle_key,
    has_usable_google_sign_in,
    has_usable_password_sign_in,
    password_sign_in_enabled,
    remove_member_password,
    store_google_pending,
    username_is_taken,
)
from .reauth import (
    action_label,
    reauth_redirect,
    recent_auth_is_fresh,
    requires_recent_auth,
    safe_next_url,
    stamp_recent_auth,
)
from .security_services import (
    EVENT_TYPES,
    client_ip,
    record_security_event,
    record_sign_in_failure_for_username,
    retouch_after_session_cycle,
)
from .audit_services import append_event
from .models import (
    AuditEvent,
    Membership,
    Person,
)
from .policy_services import (
    current_policy,
    record_onboarding_acceptance,
)
from .households import current_household


def _authenticate_member(request, username, password, key):
    """Resolve a signed-in member for (username, password), or None.

    Only a genuine failed authentication attempt counts toward the login
    throttle: recording one for a request that was already blocked would let
    a caller reset record_login_failure's window (and clear blocked_until)
    simply by retrying, before the block is meant to expire.

    Every password check in the app goes through here, so production mode
    (#792) answers None before the password is looked at.
    """
    if not password_sign_in_enabled() or login_is_blocked(key):
        return None
    user = authenticate(request, username=username, password=password)
    if user is not None and not hasattr(user, "person"):
        user = None
    if user is None:
        record_login_failure(key)
    return user



def _redirect_target(request):
    return safe_next_url(request, request.POST.get("next", ""))



def _complete_member_session(request, user):
    complete_member_session(request, user)



SETUP_FAILED = "Setup could not be completed. Check the setup code and try again later."



LAST_SIGN_IN_METHOD = "Keep at least one sign-in method."



def _privacy_policy_page_context(*, wide=True):
    return {"privacy_policy": current_policy(), "wide_card": wide}



@login_not_required
@never_cache
def sign_in(request):
    if not first_member_exists():
        return redirect("setup")
    if request.method == "POST" and not password_sign_in_enabled():
        return render(
            request,
            "finance/login.html",
            {
                "form": LoginForm(),
                "next": request.POST.get("next", ""),
                "auth_error": GOOGLE_ONLY_NOTICE,
                **_privacy_policy_page_context(wide=False),
            },
            status=403,
        )
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = normalize_username(form.cleaned_data["username"])
        key = throttle_key(username, client_ip(request))
        already_blocked = login_is_blocked(key)
        user = _authenticate_member(request, username, form.cleaned_data["password"], key)
        if user is None:
            form.add_error(None, "Sign-in failed. Check your credentials and try again later.")
            if not already_blocked:
                record_sign_in_failure_for_username(username, request)
        else:
            from .passkey_services import passkey_required_after_password, store_pending_passkey_login

            if passkey_required_after_password(user):
                store_pending_passkey_login(request, user, _redirect_target(request))
                return redirect("passkey-sign-in")
            clear_login_failures(key)
            _complete_member_session(request, user)
            return redirect(_redirect_target(request))
    return render(
        request,
        "finance/login.html",
        {"form": form, "next": request.GET.get("next", ""), **_privacy_policy_page_context(wide=False)},
    )



def _google_oauth_login(request):
    from allauth.socialaccount.providers.google.views import oauth2_login

    return oauth2_login(request)



def _start_google_join(request, google_form):
    if not google_signin_enabled():
        raise Http404()
    if not google_form.is_valid():
        return None
    invitation_code = google_form.cleaned_data["invitation_code"]
    username = validated_username(google_form.cleaned_data["username"])
    if not invitation_is_usable(invitation_code) or username_is_taken(username):
        google_form.add_error(None, "The invitation could not be used.")
        return None
    store_google_pending(
        request,
        {
            "intent": "join",
            "invitation_code": invitation_code,
            "username": google_form.cleaned_data["username"],
            "display_name": google_form.cleaned_data["display_name"],
            "accept_privacy_policy": google_form.cleaned_data.get("accept_privacy_policy", False),
            "privacy_policy_version": google_form.cleaned_data.get("privacy_policy_version"),
        },
    )
    return _google_oauth_login(request)



def _start_google_setup(request, google_form):
    if not google_signin_enabled():
        raise Http404()
    key = throttle_key(SETUP_THROTTLE_USERNAME, client_ip(request))
    if login_is_blocked(key):
        google_form.add_error(None, SETUP_FAILED)
        return None
    if not setup_code_matches(request.POST.get("setup_code", "")):
        record_login_failure(key)
        google_form.add_error(None, SETUP_FAILED)
        return None
    if not google_form.is_valid():
        return None
    username = validated_username(google_form.cleaned_data["username"])
    if username_is_taken(username):
        google_form.add_error(None, SETUP_FAILED)
        return None
    store_google_pending(
        request,
        {
            "intent": "setup",
            "username": google_form.cleaned_data["username"],
            "display_name": google_form.cleaned_data["display_name"],
            "household_name": google_form.cleaned_data["household_name"],
            "accept_privacy_policy": google_form.cleaned_data.get("accept_privacy_policy", False),
            "privacy_policy_version": google_form.cleaned_data.get("privacy_policy_version"),
        },
    )
    return _google_oauth_login(request)



def _complete_password_setup(request, form):
    key = throttle_key(SETUP_THROTTLE_USERNAME, client_ip(request))
    if login_is_blocked(key):
        form.add_error(None, SETUP_FAILED)
        return None
    if not setup_code_matches(request.POST.get("setup_code", "")):
        record_login_failure(key)
        form.add_error(None, SETUP_FAILED)
        return None
    if not form.is_valid():
        return None
    try:
        user, recovery_codes = seed_first_household(
            form.cleaned_data["username"],
            form.cleaned_data["display_name"],
            form.cleaned_data["household_name"],
            form.cleaned_data["password1"],
        )
    except ValueError:
        raise Http404() from None
    clear_login_failures(key)
    _complete_member_session(request, user)
    record_onboarding_acceptance(
        user.person,  # pyright: ignore[reportAttributeAccessIssue] - Person.user reverse one-to-one accessor, not visible to pyright
        form.cleaned_data.get("accept_privacy_policy", False),
        form.cleaned_data.get("privacy_policy_version"),
        request=request,
    )
    return recovery_codes



def account_connect_google(request):
    if not google_signin_enabled():
        raise Http404()
    post = request.POST.copy()
    post["process"] = "connect"
    request.POST = post
    return _google_oauth_login(request)



def account_disconnect_google(user):
    if not disconnect_google_account(user):
        return LAST_SIGN_IN_METHOD
    return None



def account_add_password(request, password_form):
    if not password_sign_in_enabled():
        return password_form, GOOGLE_ONLY_NOTICE
    if not password_form.is_valid():
        return password_form, None
    had_password = request.user.has_usable_password()
    with database_transaction.atomic():
        request.user.set_password(password_form.cleaned_data["password1"])
        request.user.save(update_fields=("password",))
        person = request.user.person
        append_event(
            action=AuditEvent.Action.PASSWORD_CHANGED if had_password else AuditEvent.Action.PASSWORD_ADDED,
            actor=person, target_id=person.pk,
        )
    previous_key = request.session.session_key
    update_session_auth_hash(request, request.user)
    retouch_after_session_cycle(request, previous_key)
    record_security_event(request.user, EVENT_TYPES.PASSWORD_CHANGED, request=request)
    password_form = PasswordPairForm()
    password_form.existing_user = request.user
    return password_form, None



def account_remove_password(request):
    if not remove_member_password(request.user):
        return LAST_SIGN_IN_METHOD
    request.user.refresh_from_db()
    previous_key = request.session.session_key
    update_session_auth_hash(request, request.user)
    retouch_after_session_cycle(request, previous_key)
    record_security_event(request.user, EVENT_TYPES.PASSWORD_CHANGED, request=request)
    return None



@login_not_required
@never_cache
@require_POST
def start_google_sign_in(request):
    if not google_signin_enabled():
        raise Http404()
    if not first_member_exists():
        return redirect("setup")
    key = google_throttle_key(client_ip(request))
    if login_is_blocked(key):
        return render(
            request,
            "finance/login.html",
            {
                "form": LoginForm(),
                "next": request.POST.get("next", ""),
                "auth_error": "Sign-in failed. Check your credentials and try again later.",
                **_privacy_policy_page_context(wide=False),
            },
        )
    store_google_pending(request, {"intent": "login"})
    return _google_oauth_login(request)



@login_not_required
@never_cache
@require_http_methods(["GET", "POST"])
def google_oauth_login(request):
    if not google_signin_enabled():
        raise Http404()
    if (
        request.method == "POST"
        and request.POST.get("process") == "connect"
        and request.user.is_authenticated
        and not recent_auth_is_fresh(request)
    ):
        return reauth_redirect(request, "connect-google", reverse("account-settings"))
    return _google_oauth_login(request)



@login_not_required
@never_cache
@require_GET
def google_oauth_callback(request):
    if not google_signin_enabled():
        raise Http404()
    from allauth.socialaccount.providers.google.views import oauth2_callback

    return oauth2_callback(request)



@post_only("home")
def sign_out(request):
    logout(request)
    return redirect("login")



@never_cache
@requires_recent_auth("invite")
def invite(request):
    person = get_object_or_404(Person, user=request.user)
    household = current_household(person)
    code = None
    if request.method == "POST":
        code = create_invitation(person)
    members = []
    if household is not None:
        members = list(
            Membership.objects.filter(household=household, ended_at__isnull=True)
            .select_related("person")
            .order_by("person__display_name", "pk")
        )
    return render(
        request,
        "finance/invite.html",
        {
            "invitation_code": code,
            "invitation_ttl_hours": settings.INVITATION_TTL_HOURS,
            "household": household,
            "members": members,
        },
    )



@login_not_required
@never_cache
def join(request):
    form = JoinForm()
    google_form = JoinGoogleForm()
    recovery_codes = None
    status = 200
    if request.method == "POST" and request.POST.get("intent") == "google":
        google_form = JoinGoogleForm(request.POST)
        started = _start_google_join(request, google_form)
        if started is not None:
            return started
    elif request.method == "POST" and not password_sign_in_enabled():
        status = 403
    elif request.method == "POST":
        form = JoinForm(request.POST)
        if form.is_valid():
            try:
                _user, recovery_codes = accept_invitation(
                    form.cleaned_data["invitation_code"],
                    form.cleaned_data["username"],
                    form.cleaned_data["display_name"],
                    form.cleaned_data["password1"],
                )
            except InvalidOneTimeCode:
                form.add_error(None, "The invitation could not be used.")
            else:
                record_onboarding_acceptance(
                    _user.person,  # pyright: ignore[reportAttributeAccessIssue] - Person.user reverse one-to-one accessor, not visible to pyright
                    form.cleaned_data.get("accept_privacy_policy", False),
                    form.cleaned_data.get("privacy_policy_version"),
                    request=request,
                )
    return render(
        request,
        "finance/join.html",
        {
            "form": form,
            "google_form": google_form,
            "recovery_codes": recovery_codes,
            **_privacy_policy_page_context(),
        },
        status=status,
    )



@login_not_required
@never_cache
@require_http_methods(["GET", "POST"])
def setup(request):
    if first_member_exists():
        raise Http404()
    configured = setup_code_configured()
    form = SetupForm()
    google_form = SetupGoogleForm()
    recovery_codes = None
    status = 200
    if not configured:
        return render(
            request,
            "finance/setup.html",
            {
                "form": None,
                "google_form": None,
                "recovery_codes": None,
                "setup_configured": False,
                **_privacy_policy_page_context(),
            },
        )
    if request.method == "POST" and request.POST.get("intent") == "google":
        google_form = SetupGoogleForm(request.POST)
        started = _start_google_setup(request, google_form)
        if started is not None:
            return started
    elif request.method == "POST" and not password_sign_in_enabled():
        status = 403
    elif request.method == "POST":
        form = SetupForm(request.POST)
        created_codes = _complete_password_setup(request, form)
        if created_codes is not None:
            recovery_codes = created_codes
    return render(
        request,
        "finance/setup.html",
        {
            "form": form,
            "google_form": google_form,
            "recovery_codes": recovery_codes,
            "setup_configured": True,
            **_privacy_policy_page_context(),
        },
        status=status,
    )



REAUTH_FAILED = "Confirmation failed. Try again later."



# Google accepts only none, consent, and select_account for prompt. It sends
# auth_time only to published, verified apps that ask for it through claims;
# without it the callback checks that the ID token was just issued (iat).
GOOGLE_REAUTH_AUTH_PARAMS = urlencode(
    {
        "prompt": "select_account",
        "max_age": "0",
        "claims": '{"id_token":{"auth_time":{"essential":true}}}',
    }
)



def _reauth_context(request, form, auth_error=None):
    next_url = safe_next_url(request, request.POST.get("next") or request.GET.get("next", ""))
    action = request.POST.get("action") or request.GET.get("action", "")
    return {
        "form": form,
        "next": next_url,
        "action": action,
        "action_label": action_label(action),
        "show_password": has_usable_password_sign_in(request.user),
        "show_google": has_usable_google_sign_in(request.user),
        "show_passkey": bool(
            getattr(request.user, "person", None) and request.user.person.passkeys.exists()
        ),
        "auth_error": auth_error,
        "auth_card_layout": True,
    }



def _render_reauth(request, form=None, auth_error=None, status=200):
    if form is None:
        form = ReauthPasswordForm()
    return render(request, "finance/reauth.html", _reauth_context(request, form, auth_error), status=status)



@never_cache
@require_http_methods(["GET", "POST"])
def reauth(request):
    form = ReauthPasswordForm(request.POST or None)
    if request.method != "POST":
        return _render_reauth(request, form)
    if not password_sign_in_enabled():
        return _render_reauth(request, ReauthPasswordForm(), REAUTH_FAILED, status=403)
    if not request.user.has_usable_password():
        return _render_reauth(request, ReauthPasswordForm(), REAUTH_FAILED)
    key = throttle_key(request.user.username, client_ip(request))
    user = _authenticate_member(request, request.user.username, request.POST.get("password", ""), key)
    if user is None or user.pk != request.user.pk:
        form.add_error(None, REAUTH_FAILED)
        return _render_reauth(request, form)
    clear_login_failures(key)
    stamp_recent_auth(request)
    return redirect(safe_next_url(request, request.POST.get("next", "")))



@post_only("home")
@never_cache
def start_google_reauth(request):
    if not has_usable_google_sign_in(request.user):
        raise Http404()
    key = google_throttle_key(client_ip(request))
    if login_is_blocked(key):
        return _render_reauth(request, auth_error=REAUTH_FAILED)
    next_url = safe_next_url(request, request.POST.get("next", ""))
    store_google_pending(request, {"intent": "reauth", "next": next_url})
    request.session["reauth_next"] = next_url
    query = request.GET.copy()
    query["auth_params"] = GOOGLE_REAUTH_AUTH_PARAMS
    request.GET = query
    return _google_oauth_login(request)



RECOVERY_FAILED = "Recovery failed. Check the supplied details."



@login_not_required
@never_cache
def recover(request):
    if not password_sign_in_enabled():
        # Recovery only sets a password, which signs nobody in here (#792).
        raise Http404()
    form = RecoveryForm(request.POST or None)
    recovered = False
    if request.method == "POST" and form.is_valid():
        username = normalize_username(form.cleaned_data["username"])
        key = throttle_key(username, client_ip(request))
        if login_is_blocked(key):
            # Not counted as a failure: that would let retries reset the window.
            form.add_error(None, RECOVERY_FAILED)
        else:
            try:
                recovered_user = recover_account(
                    form.cleaned_data["username"],
                    form.cleaned_data["recovery_code"],
                    form.cleaned_data["password1"],
                )
            except InvalidOneTimeCode:
                record_login_failure(key)
                record_sign_in_failure_for_username(username, request)
                form.add_error(None, RECOVERY_FAILED)
            else:
                clear_login_failures(key)
                recovered = True
                record_security_event(recovered_user, EVENT_TYPES.RECOVERY_CODE_USED, request=request)
                record_security_event(recovered_user, EVENT_TYPES.PASSWORD_CHANGED, request=request)
    return render(request, "finance/recover.html", {"form": form, "recovered": recovered})



@login_not_required
@never_cache
@require_http_methods(["GET", "POST"])
def break_glass(request, token):
    """Sign a member in with the one-time link `manage.py break_glass_sign_in` printed (#792).

    Cloudflare sets CF-Connecting-IP on every request it forwards and a visitor
    cannot remove it, so a request carrying it came through the public tunnel
    and is refused: the link works on the tailnet only. Every refusal is the
    same 404.
    """
    if "HTTP_CF_CONNECTING_IP" in request.META:
        raise Http404()
    key = break_glass_throttle_key(client_ip(request))
    if login_is_blocked(key):
        raise Http404()
    if request.method == "GET":
        # Opening the link changes nothing, so a link preview cannot use it up.
        if not break_glass_token_is_usable(token):
            record_login_failure(key)
            raise Http404()
        return render(request, "finance/break_glass.html", {"auth_card_layout": True})
    user = redeem_break_glass_token(token)
    if user is None:
        record_login_failure(key)
        raise Http404()
    clear_login_failures(key)
    # Redeeming ended every session of the member, this browser's included,
    # so start from an empty session with a new key.
    request.session.flush()
    _complete_member_session(request, user)
    record_security_event(user, EVENT_TYPES.BREAK_GLASS_SIGN_IN, request=request)
    return redirect("account-settings")
