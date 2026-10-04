from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .auth_services import (
    InvalidOneTimeCode,
    accept_invitation,
    clear_login_failures,
    complete_member_session,
    first_member_exists,
    login_is_blocked,
    record_login_failure,
    seed_first_household,
)
from .policy_services import current_policy, record_onboarding_acceptance
from .google_auth import (
    GOOGLE_FAILED,
    GOOGLE_FLOW_NONCE_STATE_KEY,
    GoogleOnboardingConflict,
    bind_google_flow_nonce,
    complete_google_onboarding,
    consume_google_pending,
    google_email_is_verified,
    google_reauth_identity_matches,
    google_reauth_is_recent,
    google_signin_enabled,
    google_throttle_key,
    google_uid,
    has_usable_google_sign_in,
    peek_google_pending,
)
from .reauth import reauth_redirect, recent_auth_is_fresh, safe_next_url, stamp_recent_auth


class MemberAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    def send_mail(self, template_prefix, email, context):
        return None


class MemberSocialAccountAdapter(DefaultSocialAccountAdapter):
    def send_notification_mail(self, *args, **kwargs):
        return None

    def can_authenticate_by_email(self, login, email):
        return False

    def authenticate_by_email(self, sociallogin):
        return None

    def is_email_verified(self, provider, email):
        return False

    def get_connect_redirect_url(self, request, socialaccount):
        return reverse("account-settings")

    def generate_state_param(self, state):
        from allauth.core import context

        request = self.request or context.request
        if request is not None:
            nonce = bind_google_flow_nonce(request, state)
            if nonce:
                return nonce
        return super().generate_state_param(state)

    def is_open_for_signup(self, request, sociallogin):
        nonce = (sociallogin.state or {}).get(GOOGLE_FLOW_NONCE_STATE_KEY)
        pending = peek_google_pending(request, nonce)
        return bool(pending and pending.get("intent") in ("join", "setup"))

    def on_authentication_error(
        self, request, provider, error=None, exception=None, extra_context=None
    ):
        # A denied or failed Google round trip is a failed sign-in attempt and
        # counts toward the same remote-address throttle as a wrong password.
        record_login_failure(google_throttle_key(request.META.get("REMOTE_ADDR")))
        raise ImmediateHttpResponse(self._failed_response(request))

    def pre_social_login(self, request, sociallogin):
        if not google_signin_enabled():
            raise ImmediateHttpResponse(self._failed_response(request))
        key = google_throttle_key(request.META.get("REMOTE_ADDR"))
        extra = sociallogin.account.extra_data or {}
        if not google_uid(extra) or not google_email_is_verified(extra):
            record_login_failure(key)
            raise ImmediateHttpResponse(self._failed_response(request))
        self._dispatch_social_login(request, sociallogin, key)

    def _dispatch_social_login(self, request, sociallogin, key):
        process = sociallogin.state.get("process")
        pending = consume_google_pending(request, sociallogin)
        intent = (pending or {}).get("intent")
        if login_is_blocked(key):
            raise ImmediateHttpResponse(self._failed_response(request))
        if process == "connect" and not recent_auth_is_fresh(request):
            # Linking a new sign-in method is sensitive: refuse a connect that
            # reached the callback without a fresh confirmation (for example a
            # direct POST to the login URL from a stale session).
            raise ImmediateHttpResponse(
                reauth_redirect(request, "connect-google", reverse("account-settings"))
            )
        if intent == "reauth":
            self._complete_reauth(request, sociallogin, pending, key)
            return
        if sociallogin.is_existing:
            self._complete_existing_social_login(request, sociallogin, process, key)
            return
        if process == "connect":
            return
        self._complete_onboarding_social_login(request, sociallogin, pending, intent, key)

    def _complete_existing_social_login(self, request, sociallogin, process, key):
        if not hasattr(sociallogin.user, "person"):
            record_login_failure(key)
            raise ImmediateHttpResponse(self._failed_response(request))
        if process == "connect":
            return
        current = getattr(request, "user", None)
        if current is not None and current.is_authenticated:
            if current.pk != sociallogin.user.pk:
                # A Google identity belonging to another member must never
                # confirm (or take over) the signed-in member's session.
                record_login_failure(key)
                raise ImmediateHttpResponse(self._failed_response(request))
            # Re-signing into an existing session is not a confirmation: that
            # goes through /reauth/google/, which checks Google's auth_time.
            clear_login_failures(key)
            return
        clear_login_failures(key)
        stamp_recent_auth(request)

    def _complete_onboarding_social_login(self, request, sociallogin, pending, intent, key):
        if not pending:
            record_login_failure(key)
            raise ImmediateHttpResponse(self._failed_response(request, "login"))
        if intent == "join":
            self._complete_join(request, sociallogin, pending, key)
            return
        if intent == "setup":
            self._complete_setup(request, sociallogin, pending, key)
            return
        record_login_failure(key)
        raise ImmediateHttpResponse(self._failed_response(request, "login"))

    def validate_disconnect(self, account, accounts):
        if has_usable_google_sign_in(account.user) and not account.user.has_usable_password():
            raise self.validation_error("disconnect_last")

    def _complete_join(self, request, sociallogin, pending, key):
        uid = google_uid(sociallogin.account.extra_data) or sociallogin.account.uid
        try:
            _user, recovery_codes = complete_google_onboarding(
                uid,
                lambda: accept_invitation(
                    pending.get("invitation_code", ""),
                    pending.get("username", ""),
                    pending.get("display_name", ""),
                    password=None,
                ),
                lambda user: sociallogin.connect(request, user),
            )
        except (InvalidOneTimeCode, GoogleOnboardingConflict):
            record_login_failure(key)
            raise ImmediateHttpResponse(self._failed_response(request, "join"))
        record_onboarding_acceptance(
            _user.person,
            pending.get("accept_privacy_policy", False),
            pending.get("privacy_policy_version"),
            request=request,
        )
        clear_login_failures(key)
        raise ImmediateHttpResponse(
            render(
                request,
                "finance/join.html",
                {
                    "form": None,
                    "google_form": None,
                    "recovery_codes": recovery_codes,
                    "privacy_policy": current_policy(),
                    "wide_card": True,
                },
            )
        )

    def _complete_setup(self, request, sociallogin, pending, key):
        if first_member_exists():
            raise ImmediateHttpResponse(self._failed_response(request, "setup"))
        uid = google_uid(sociallogin.account.extra_data) or sociallogin.account.uid
        try:
            user, recovery_codes = complete_google_onboarding(
                uid,
                lambda: seed_first_household(
                    pending.get("username", ""),
                    pending.get("display_name", ""),
                    pending.get("household_name", ""),
                    password=None,
                ),
                lambda created: sociallogin.connect(request, created),
            )
        except (ValueError, GoogleOnboardingConflict):
            raise ImmediateHttpResponse(self._failed_response(request, "setup"))
        record_onboarding_acceptance(
            user.person,
            pending.get("accept_privacy_policy", False),
            pending.get("privacy_policy_version"),
            request=request,
        )
        clear_login_failures(key)
        complete_member_session(request, user)
        raise ImmediateHttpResponse(
            render(
                request,
                "finance/setup.html",
                {
                    "form": None,
                    "google_form": None,
                    "recovery_codes": recovery_codes,
                    "setup_configured": True,
                    "privacy_policy": current_policy(),
                    "wide_card": True,
                },
            )
        )

    def _complete_reauth(self, request, sociallogin, pending, key):
        extra = sociallogin.account.extra_data or {}
        if (
            not request.user.is_authenticated
            or not google_reauth_identity_matches(request.user, extra)
            or not google_reauth_is_recent(extra)
        ):
            record_login_failure(key)
            raise ImmediateHttpResponse(self._failed_response(request, "reauth"))
        clear_login_failures(key)
        stamp_recent_auth(request)
        raise ImmediateHttpResponse(
            HttpResponseRedirect(safe_next_url(request, (pending or {}).get("next", "")))
        )

    def _failed_response(self, request, page="login"):
        from django.contrib import messages

        messages.error(request, GOOGLE_FAILED)
        if page == "join":
            return HttpResponseRedirect(reverse("join"))
        if page == "setup":
            return HttpResponseRedirect(reverse("setup"))
        if page == "settings":
            return HttpResponseRedirect(reverse("account-settings"))
        if page == "reauth":
            next_url = safe_next_url(request, request.session.get("reauth_next", ""))
            return HttpResponseRedirect(f"{reverse('reauth')}?{urlencode({'next': next_url})}")
        return HttpResponseRedirect(reverse("login"))
