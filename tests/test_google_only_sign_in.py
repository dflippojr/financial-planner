"""Production mode (GOOGLE_ONLY_SIGN_IN): Google is the only way in (#792).

No password is accepted anywhere, from loopback and the tailnet too: not at
sign-in, confirmation, join, first-run setup or recovery. Join and first-run
setup still complete through Google.
"""

import pytest
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.auth_services import create_invitation, create_recovery_codes
from finance.models import LoginThrottle, RecoveryCode
from finance.reauth import RECENT_AUTH_SESSION_KEY
from financial_planner.settings import google_only_sign_in_from_env
from tests.test_auth_flows import NEW_PASSWORD, PASSWORD, SYNTHETIC_SETUP_CODE, _setup_form, make_member
from tests.test_google_auth import GOOGLE_SUB, _finish_google, _google_settings


def _google_only(**extra):
    return _google_settings(GOOGLE_ONLY_SIGN_IN=True, **extra)


def _link_google(user):
    return SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})


def _confirmed_client(user):
    client = Client()
    client.force_login(user)
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()
    return client


def test_production_mode_is_off_by_default():
    assert settings.GOOGLE_ONLY_SIGN_IN is False


@pytest.mark.parametrize(
    ("value", "client_id", "secret", "expected"),
    [
        ("", "", "", False),
        ("false", "id", "secret", False),
        ("true", "id", "secret", True),
        (" TRUE ", "id", "secret", True),
    ],
)
def test_production_mode_flag_is_read_from_the_environment(value, client_id, secret, expected):
    assert google_only_sign_in_from_env(value, client_id, secret) is expected


@pytest.mark.parametrize(("client_id", "secret"), [("", ""), ("id", ""), ("", "secret")])
def test_production_mode_without_google_credentials_stops_start_up(client_id, secret):
    # Otherwise the app would start with no way to sign in at all.
    with pytest.raises(ImproperlyConfigured, match="GOOGLE_ONLY_SIGN_IN"):
        google_only_sign_in_from_env("true", client_id, secret)


@pytest.mark.django_db
@_google_only()
@pytest.mark.parametrize("remote", ["127.0.0.1", "100.64.0.5", "203.0.113.20"], ids=["loopback", "tailnet", "public"])
def test_password_sign_in_is_refused_and_creates_no_session(remote):
    user, _person, _household = make_member()
    client = Client(REMOTE_ADDR=remote)

    response = client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert response.status_code == 403
    assert "_auth_user_id" not in client.session
    # The password was never checked, so nothing was counted against the name.
    assert not LoginThrottle.objects.exists()
    assert client.get(reverse("home")).status_code == 302


@pytest.mark.django_db
@_google_only()
def test_sign_in_page_offers_only_google():
    make_member()

    page = Client().get(reverse("login"))

    assert page.status_code == 200
    assert b"Sign in with Google" in page.content
    assert b'name="password"' not in page.content
    assert b'name="username"' not in page.content
    assert reverse("recover").encode() not in page.content


@pytest.mark.django_db
@_google_only()
def test_a_failed_google_sign_in_does_not_suggest_a_password():
    make_member()
    client = Client()

    failed = _finish_google(client, client.post(reverse("google-sign-in")))
    page = client.get(failed["Location"])

    assert b"Google sign-in failed" in page.content
    assert b"password" not in page.content.lower()


@pytest.mark.django_db
@_google_only()
def test_a_google_member_signs_in():
    user, _person, _household = make_member()
    _link_google(user)
    client = Client()

    signed_in = _finish_google(client, client.post(reverse("google-sign-in")))

    assert signed_in.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
@_google_only()
def test_join_completes_with_google_and_refuses_a_password():
    inviter, _person, _household = make_member("inviter")
    code = create_invitation(inviter.person)
    client = Client()

    page = client.get(reverse("join"))
    assert b"Join with Google" in page.content
    assert b'name="password1"' not in page.content

    refused = client.post(
        reverse("join"),
        {
            "invitation_code": code,
            "username": "joined-password",
            "display_name": "Joined Password",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    assert refused.status_code == 403
    assert get_user_model().objects.count() == 1

    start = client.post(
        reverse("join"),
        {"intent": "google", "invitation_code": code, "username": "joined-google", "display_name": "Joined Google"},
    )
    joined = _finish_google(client, start)
    assert joined.status_code == 200
    assert b"Save these one-time recovery codes" in joined.content
    user = get_user_model().objects.get(username="joined-google")
    assert not user.has_usable_password()
    assert SocialAccount.objects.filter(user=user, uid=GOOGLE_SUB).exists()

    # The new member then signs in with Google, the only way in.
    signed_in = _finish_google(client, client.post(reverse("google-sign-in")))
    assert signed_in.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
@_google_only(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_first_member_setup_completes_with_google_and_refuses_a_password():
    client = Client()

    page = client.get(reverse("setup"))
    assert b"Set up with Google" in page.content
    assert b'name="password1"' not in page.content

    refused = client.post(reverse("setup"), _setup_form())
    assert refused.status_code == 403
    assert get_user_model().objects.count() == 0
    assert "_auth_user_id" not in client.session

    start = client.post(
        reverse("setup"),
        {
            "intent": "google",
            "setup_code": SYNTHETIC_SETUP_CODE,
            "username": "first-google",
            "display_name": "First Google",
            "household_name": "Synthetic Household",
        },
    )
    created = _finish_google(client, start)
    assert created.status_code == 200
    user = get_user_model().objects.get()
    assert not user.has_usable_password()
    assert client.session["_auth_user_id"] == str(user.pk)
    assert client.get(reverse("home")).status_code == 200


@pytest.mark.django_db
@_google_only()
def test_a_password_does_not_confirm_a_sensitive_action():
    user, _person, _household = make_member()
    _link_google(user)
    client = Client()
    client.force_login(user)

    page = client.get(reverse("reauth"))
    assert b'name="password"' not in page.content

    refused = client.post(reverse("reauth"), {"password": PASSWORD, "next": reverse("invite")})

    assert refused.status_code == 403
    assert RECENT_AUTH_SESSION_KEY not in client.session
    assert not LoginThrottle.objects.exists()


@pytest.mark.django_db
@_google_only()
def test_recovery_is_gone_because_it_only_sets_a_password():
    user, _person, _household = make_member()
    codes = create_recovery_codes(user)
    client = Client()

    assert client.get(reverse("recover")).status_code == 404
    refused = client.post(
        reverse("recover"),
        {"username": user.username, "recovery_code": codes[0], "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )

    assert refused.status_code == 404
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
    assert RecoveryCode.objects.filter(user=user, used_at__isnull=True).count() == 8


@pytest.mark.django_db
@_google_only()
@pytest.mark.parametrize("url_name", ["passkey-sign-in", "passkey-sign-in-options", "passkey-sign-in-assert"])
def test_the_step_after_a_password_is_gone(url_name):
    make_member()

    assert Client().post(reverse(url_name)).status_code == 404


@pytest.mark.django_db
@_google_only()
def test_a_member_cannot_add_a_password():
    user, _person, _household = make_member()
    user.set_unusable_password()
    user.save(update_fields=("password",))
    _link_google(user)
    client = _confirmed_client(user)

    page = client.get(reverse("account-settings"))
    assert b'value="add-password"' not in page.content

    client.post(
        reverse("account-settings"), {"action": "add-password", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    )

    user.refresh_from_db()
    assert not user.has_usable_password()


@pytest.mark.django_db
@_google_only()
def test_google_cannot_be_disconnected_even_with_a_password_set():
    # A leftover password signs nobody in, so Google is the last method.
    user, _person, _household = make_member()
    _link_google(user)
    client = _confirmed_client(user)

    page = client.get(reverse("account-settings"))
    assert b'value="disconnect-google"' not in page.content

    client.post(reverse("account-settings"), {"action": "disconnect-google"})

    assert SocialAccount.objects.filter(user=user, provider="google").exists()
