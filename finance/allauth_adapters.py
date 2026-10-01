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
from .google_auth import (
    GOOGLE_FAILED,
    GOOGLE_FLOW_NONCE_STATE_KEY,
    bind_google_flow_nonce,
    consume_google_pending,
    google_email_is_verified,
    google_signin_enabled,
    google_throttle_key,
    google_uid,
    has_google_account,
    peek_google_pending,
    sign_in_method_count,
)


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
        raise ImmediateHttpResponse(self._failed_response(request))

    def pre_social_login(self, request, sociallogin):
        if not google_signin_enabled():
            raise ImmediateHttpResponse(self._failed_response(request))
        extra = sociallogin.account.extra_data or {}
        if not google_uid(extra) or not google_email_is_verified(extra):
            raise ImmediateHttpResponse(self._failed_response(request))
        process = sociallogin.state.get("process")
        pending = consume_google_pending(request, sociallogin)
        intent = (pending or {}).get("intent")
        key = google_throttle_key(request.META.get("REMOTE_ADDR"))
        if login_is_blocked(key):
            raise ImmediateHttpResponse(self._failed_response(request))
        if sociallogin.is_existing:
            if not hasattr(sociallogin.user, "person"):
                record_login_failure(key)
                raise ImmediateHttpResponse(self._failed_response(request))
            if process == "connect":
                return
            clear_login_failures(key)
            return
        if process == "connect":
            return
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
        if sign_in_method_count(account.user) <= 1:
            raise self.validation_error("disconnect_last")
        if not account.user.has_usable_password() and not has_google_account(account.user):
            raise self.validation_error("disconnect_last")

    def _complete_join(self, request, sociallogin, pending, key):
        try:
            user, recovery_codes = accept_invitation(
                pending.get("invitation_code", ""),
                pending.get("username", ""),
                pending.get("display_name", ""),
                password=None,
            )
        except InvalidOneTimeCode:
            record_login_failure(key)
            raise ImmediateHttpResponse(self._failed_response(request, "join"))
        sociallogin.connect(request, user)
        clear_login_failures(key)
        raise ImmediateHttpResponse(
            render(
                request,
                "finance/join.html",
                {"form": None, "google_form": None, "recovery_codes": recovery_codes},
            )
        )

    def _complete_setup(self, request, sociallogin, pending, key):
        if first_member_exists():
            raise ImmediateHttpResponse(self._failed_response(request, "setup"))
        try:
            user, recovery_codes = seed_first_household(
                pending.get("username", ""),
                pending.get("display_name", ""),
                pending.get("household_name", ""),
                password=None,
            )
        except ValueError:
            raise ImmediateHttpResponse(self._failed_response(request, "setup"))
        sociallogin.connect(request, user)
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
                },
            )
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
        return HttpResponseRedirect(reverse("login"))
