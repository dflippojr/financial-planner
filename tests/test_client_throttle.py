"""Sign-in throttles are per client when the app runs behind a trusted proxy.

Every client below reaches the app through the same proxy address, as it does
behind Tailscale Serve, and is told apart only by the proxy-appended
X-Forwarded-For hop.
"""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.auth_services import create_recovery_codes, throttle_key
from finance.models import LoginThrottle
from finance.reauth import RECENT_AUTH_SESSION_KEY
from financial_planner.trusted_proxies import parse_trusted_proxies
from tests.test_auth_flows import NEW_PASSWORD, PASSWORD, SYNTHETIC_SETUP_CODE, _setup_form, make_member
from tests.test_google_auth import _google_settings
from tests.test_passkeys import PASSKEY_SETTINGS, _require_passkey_after_password


PROXY_ADDRESS = "172.18.0.1"
TRUSTED_PROXY = parse_trusted_proxies(PROXY_ADDRESS)
THROTTLE_SETTINGS = override_settings(
    TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900
)


def _client_at(address):
    return Client(REMOTE_ADDR=PROXY_ADDRESS, HTTP_X_FORWARDED_FOR=address)


@pytest.mark.django_db
@THROTTLE_SETTINGS
def test_password_failures_from_one_client_do_not_block_another_client():
    user, _person, _household = make_member()
    first = _client_at("100.64.0.11")
    second = _client_at("100.64.0.12")
    for _ in range(2):
        first.post(reverse("login"), {"username": user.username, "password": "wrong"})

    blocked = first.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    allowed = second.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert b"Sign-in failed" in blocked.content
    assert "_auth_user_id" not in first.session
    assert allowed.status_code == 302
    assert second.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
@THROTTLE_SETTINGS
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_failures_from_one_client_do_not_block_another_client():
    first = _client_at("100.64.0.11")
    second = _client_at("100.64.0.12")
    for _ in range(2):
        first.post(reverse("setup"), _setup_form(setup_code="wrong"))
    first.post(reverse("setup"), _setup_form())
    assert get_user_model().objects.count() == 0

    second.post(reverse("setup"), _setup_form())

    assert get_user_model().objects.count() == 1


@pytest.mark.django_db
@THROTTLE_SETTINGS
def test_reauth_failures_from_one_client_do_not_block_another_client():
    user, _person, _household = make_member()
    first = _client_at("100.64.0.11")
    second = _client_at("100.64.0.12")
    first.force_login(user)
    second.force_login(user)
    for _ in range(2):
        first.post(reverse("reauth"), {"password": "wrong-password", "next": reverse("invite")})

    first.post(reverse("reauth"), {"password": PASSWORD, "next": reverse("invite")})
    second.post(reverse("reauth"), {"password": PASSWORD, "next": reverse("invite")})

    assert RECENT_AUTH_SESSION_KEY not in first.session
    assert RECENT_AUTH_SESSION_KEY in second.session


@PASSKEY_SETTINGS
@pytest.mark.django_db
@THROTTLE_SETTINGS
def test_passkey_step_failures_from_one_client_do_not_block_another_client():
    user, _person, _household = make_member()
    _require_passkey_after_password(user)
    first = _client_at("100.64.0.11")
    second = _client_at("100.64.0.12")
    for _ in range(2):
        first.post(reverse("login"), {"username": user.username, "password": PASSWORD})
        first.post(reverse("passkey-sign-in"), {"recovery_code": "not-a-recovery-code"})

    blocked = first.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    allowed = second.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert b"Sign-in failed" in blocked.content
    assert allowed.status_code == 302
    assert allowed["Location"].startswith(reverse("passkey-sign-in"))


def _deny_google(client):
    start = client.post(reverse("google-sign-in"))
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    client.get(reverse("google_callback"), {"error": "access_denied", "state": state})


@pytest.mark.django_db
@_google_settings(TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_google_failures_from_one_client_do_not_block_another_client():
    make_member()
    first = _client_at("100.64.0.11")
    second = _client_at("100.64.0.12")
    for _ in range(2):
        _deny_google(first)

    blocked = first.post(reverse("google-sign-in"))
    allowed = second.post(reverse("google-sign-in"))

    assert b"Sign-in failed" in blocked.content
    assert allowed.status_code == 302


@pytest.mark.django_db
@_google_settings(TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_google_error_callbacks_without_a_started_sign_in_are_not_counted():
    make_member()
    client = _client_at("100.64.0.11")
    for _ in range(3):
        client.get(reverse("google_callback"), {"error": "access_denied", "state": "not-a-started-flow"})
        client.get(reverse("google_callback"), {"error": "access_denied"})

    started = client.post(reverse("google-sign-in"))

    assert LoginThrottle.objects.count() == 0
    assert started.status_code == 302


@pytest.mark.django_db
@_google_settings(TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_a_started_google_sign_in_counts_at_most_one_failure():
    make_member()
    client = _client_at("100.64.0.11")
    start = client.post(reverse("google-sign-in"))
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    for _ in range(3):
        client.get(reverse("google_callback"), {"error": "access_denied", "state": state})

    assert LoginThrottle.objects.get().failure_count == 1
    assert client.post(reverse("google-sign-in")).status_code == 302


@pytest.mark.django_db
@THROTTLE_SETTINGS
def test_forwarded_for_from_an_untrusted_address_cannot_buy_a_fresh_bucket():
    # A container on the compose network connects from its own address, not the
    # proxy's, so rotating X-Forwarded-For must not reset its pair throttle.
    user, _person, _household = make_member()
    for index in range(2):
        Client(REMOTE_ADDR="172.18.0.7", HTTP_X_FORWARDED_FOR=f"100.64.0.{index + 20}").post(
            reverse("login"), {"username": user.username, "password": "wrong"}
        )

    spoofed = Client(REMOTE_ADDR="172.18.0.7", HTTP_X_FORWARDED_FOR="100.64.0.99")
    blocked = spoofed.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert b"Sign-in failed" in blocked.content
    assert "_auth_user_id" not in spoofed.session


def _fail_from_rotating_clients(username, count):
    for index in range(count):
        _client_at(f"100.64.1.{index + 1}").post(reverse("login"), {"username": username, "password": "wrong"})


@pytest.mark.django_db
@THROTTLE_SETTINGS
@override_settings(LOGIN_USERNAME_FAILURE_LIMIT=3, LOGIN_USERNAME_FAILURE_WINDOW_SECONDS=900)
def test_username_cap_trips_across_rotating_client_addresses_and_then_expires():
    user, _person, _household = make_member()
    other, _other_person, _other_household = make_member("othermember")
    _fail_from_rotating_clients(user.username, 3)

    fresh = _client_at("100.64.2.1")
    blocked = fresh.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert b"Sign-in failed" in blocked.content
    assert "_auth_user_id" not in fresh.session

    # The cap is per username: another member at the same address is unaffected.
    neighbour = _client_at("100.64.2.1")
    assert neighbour.post(reverse("login"), {"username": other.username, "password": PASSWORD}).status_code == 302

    # The block ends with its window, so a lockout lasts at most that long.
    username_row = LoginThrottle.objects.get(key_digest=throttle_key(user.username, "").username)
    assert username_row.blocked_until == username_row.window_started_at + timedelta(seconds=900)
    earlier = timedelta(seconds=901)
    LoginThrottle.objects.update(
        window_started_at=username_row.window_started_at - earlier, blocked_until=timezone.now() - timedelta(seconds=1)
    )
    later = _client_at("100.64.2.1")
    assert later.post(reverse("login"), {"username": user.username, "password": PASSWORD}).status_code == 302


@pytest.mark.django_db
@THROTTLE_SETTINGS
@override_settings(LOGIN_USERNAME_FAILURE_LIMIT=3)
def test_username_cap_also_blocks_recovery_codes():
    user, _person, _household = make_member()
    code = create_recovery_codes(user, count=1)[0]
    _fail_from_rotating_clients(user.username, 3)

    response = _client_at("100.64.2.1").post(
        reverse("recover"),
        {"username": user.username, "recovery_code": code, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )

    assert b"Recovery failed" in response.content
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
    username_row = LoginThrottle.objects.get(key_digest=throttle_key(user.username, "").username)
    # A blocked attempt is not counted, so it can't push the window along.
    assert username_row.failure_count == 3


@pytest.mark.django_db
@_google_settings(TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_USERNAME_FAILURE_LIMIT=1)
def test_google_failures_have_no_shared_cap_across_clients():
    # Google sign-in has no guessable secret tied to a name, so a failure from
    # one client must not start a household-wide Google lockout.
    make_member()
    _deny_google(_client_at("100.64.0.11"))

    assert _client_at("100.64.0.12").post(reverse("google-sign-in")).status_code == 302
    assert LoginThrottle.objects.count() == 1
