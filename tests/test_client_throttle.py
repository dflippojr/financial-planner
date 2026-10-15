"""Sign-in throttles are per client when the app runs behind a trusted proxy.

Every client below reaches the app through the same proxy address, as it does
behind Tailscale Serve, and is told apart only by the proxy-appended
X-Forwarded-For hop.
"""

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.auth_services import create_recovery_codes, login_is_blocked, throttle_key
from finance.models import LoginThrottle, RecoveryCode
from finance.reauth import RECENT_AUTH_SESSION_KEY
from financial_planner.trusted_proxies import parse_trusted_proxies
from tests.test_auth_flows import NEW_PASSWORD, PASSWORD, SYNTHETIC_SETUP_CODE, _setup_form, make_member
from tests.test_google_auth import GOOGLE_SUB, _finish_google, _google_settings, _id_token
from tests.test_passkeys import PASSKEY_SETTINGS, _json, _require_passkey_after_password


PROXY_ADDRESS = "172.18.0.1"
TRUSTED_PROXY = parse_trusted_proxies(PROXY_ADDRESS)
THROTTLE_SETTINGS = override_settings(
    TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900
)


def _client_at(address):
    return Client(REMOTE_ADDR=PROXY_ADDRESS, HTTP_X_FORWARDED_FOR=address)


def _deny_google(client):
    start = client.post(reverse("google-sign-in"))
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    client.get(reverse("google_callback"), {"error": "access_denied", "state": state})


@dataclass(frozen=True)
class SignInFlow:
    """One way to present a credential that the login throttle guards.

    ``prepare`` builds the world, ``sign_in`` readies a client (the reauth form
    needs a signed-in member), ``fail`` is one wrong attempt and ``attempt`` is
    the correct credential. ``refused`` checks that a blocked attempt got the
    generic failure and changed nothing; ``accepted`` checks the same attempt
    from a client that was never blocked.
    """

    prepare: Callable
    fail: Callable
    attempt: Callable
    refused: Callable
    accepted: Callable
    sign_in: Callable = lambda client, world: None


def _member_with_google():
    user, person, _household = make_member()
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    return SimpleNamespace(user=user, person=person)


def _member_with_recovery_code(**extra):
    user, person, _household = make_member()
    codes = create_recovery_codes(user, count=2)
    return SimpleNamespace(user=user, person=person, code=codes[0], **extra)


def _signed_out(client):
    assert "_auth_user_id" not in client.session


def _post_login(client, world, password=PASSWORD):
    return client.post(reverse("login"), {"username": world.user.username, "password": password})


def _prepare_login():
    user, person, _household = make_member()
    return SimpleNamespace(user=user, person=person)


def _login_refused(response, client, world):
    assert b"Sign-in failed" in response.content
    _signed_out(client)


def _login_accepted(response, client, world):
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(world.user.pk)


def _recover(client, world, code):
    return client.post(
        reverse("recover"),
        {"username": world.user.username, "recovery_code": code, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )


def _recover_refused(response, client, world):
    assert b"Recovery failed" in response.content
    world.user.refresh_from_db()
    assert world.user.check_password(PASSWORD)
    assert not RecoveryCode.objects.filter(user=world.user, used_at__isnull=False).exists()


def _recover_accepted(response, client, world):
    world.user.refresh_from_db()
    assert world.user.check_password(NEW_PASSWORD)
    assert RecoveryCode.objects.filter(user=world.user, used_at__isnull=False).count() == 1


def _setup_refused(response, client, world):
    assert b"Setup could not be completed" in response.content
    assert get_user_model().objects.count() == 0


def _setup_accepted(response, client, world):
    assert get_user_model().objects.count() == 1


def _reauth_refused(response, client, world):
    assert RECENT_AUTH_SESSION_KEY not in client.session


def _reauth_accepted(response, client, world):
    assert RECENT_AUTH_SESSION_KEY in client.session


def _post_reauth(client, world, password):
    return client.post(reverse("reauth"), {"password": password, "next": reverse("invite")})


def _passkey_world():
    user, person, _household = make_member()
    _require_passkey_after_password(user)
    return SimpleNamespace(user=user, person=person, code=create_recovery_codes(user, count=2)[0])


def _passkey_second_step(client, world):
    return client.post(reverse("passkey-sign-in"), {"recovery_code": world.code})


def _passkey_refused(response, client, world):
    assert b"Sign-in failed" in response.content
    # Even the right recovery code gets nowhere from a blocked client.
    _passkey_second_step(client, world)
    _signed_out(client)
    assert not RecoveryCode.objects.filter(user=world.user, used_at__isnull=False).exists()


def _passkey_accepted(response, client, world):
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("passkey-sign-in"))
    _passkey_second_step(client, world)
    assert client.session["_auth_user_id"] == str(world.user.pk)


def _passkey_attempt_then_options(client, world):
    _post_login(client, world)
    return _json(client, reverse("passkey-sign-in-options"))


def _options_refused(response, client, world):
    assert response.status_code == 429
    _signed_out(client)


def _options_accepted(response, client, world):
    assert response.status_code == 200


def _start_google(client, world):
    start = client.post(reverse("google-sign-in"))
    return _finish_google(client, start) if start.status_code == 302 else start


def _google_fail(id_token):
    def fail(client, world):
        _finish_google(client, client.post(reverse("google-sign-in")), id_token=id_token)

    return fail


def _google_refused(response, client, world):
    assert b"Sign-in failed" in response.content
    _signed_out(client)
    assert SocialAccount.objects.count() == 1


def _google_accepted(response, client, world):
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(world.user.pk)


SIGN_IN_FLOWS = {
    "login": SignInFlow(
        prepare=_prepare_login,
        fail=lambda client, world: _post_login(client, world, "wrong"),
        attempt=_post_login,
        refused=_login_refused,
        accepted=_login_accepted,
    ),
    "recover": SignInFlow(
        prepare=_member_with_recovery_code,
        fail=lambda client, world: _recover(client, world, "000000-000000"),
        attempt=lambda client, world: _recover(client, world, world.code),
        refused=_recover_refused,
        accepted=_recover_accepted,
    ),
    "setup": SignInFlow(
        prepare=lambda: SimpleNamespace(),
        fail=lambda client, world: client.post(reverse("setup"), _setup_form(setup_code="wrong")),
        attempt=lambda client, world: client.post(reverse("setup"), _setup_form()),
        refused=_setup_refused,
        accepted=_setup_accepted,
    ),
    "reauth": SignInFlow(
        prepare=_prepare_login,
        sign_in=lambda client, world: client.force_login(world.user),
        fail=lambda client, world: _post_reauth(client, world, "wrong-password"),
        attempt=lambda client, world: _post_reauth(client, world, PASSWORD),
        refused=_reauth_refused,
        accepted=_reauth_accepted,
    ),
    "passkey-sign-in": SignInFlow(
        prepare=_passkey_world,
        fail=lambda client, world: (
            _post_login(client, world),
            client.post(reverse("passkey-sign-in"), {"recovery_code": "not-a-recovery-code"}),
        ),
        attempt=_post_login,
        refused=_passkey_refused,
        accepted=_passkey_accepted,
    ),
    "passkey-assertion": SignInFlow(
        prepare=_passkey_world,
        fail=lambda client, world: (
            _post_login(client, world),
            _json(client, reverse("passkey-sign-in-assert"), {"credential": {}}),
        ),
        attempt=_passkey_attempt_then_options,
        refused=_options_refused,
        accepted=_options_accepted,
    ),
    "google-denied": SignInFlow(
        prepare=_member_with_google,
        fail=lambda client, world: _deny_google(client),
        attempt=_start_google,
        refused=_google_refused,
        accepted=_google_accepted,
    ),
    "google-unverified": SignInFlow(
        prepare=_member_with_google,
        fail=_google_fail(_id_token(verified=False)),
        attempt=_start_google,
        refused=_google_refused,
        accepted=_google_accepted,
    ),
    "google-unknown-sub": SignInFlow(
        prepare=_member_with_google,
        fail=_google_fail(_id_token(sub="synthetic-google-sub-unknown")),
        attempt=_start_google,
        refused=_google_refused,
        accepted=_google_accepted,
    ),
}


def _throttle_rows():
    return sorted(LoginThrottle.objects.values_list("key_digest", "failure_count", "window_started_at", "blocked_until"))


@PASSKEY_SETTINGS
@pytest.mark.django_db
@_google_settings(TRUSTED_PROXY_IPS=TRUSTED_PROXY, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
@pytest.mark.parametrize("flow", SIGN_IN_FLOWS.values(), ids=SIGN_IN_FLOWS.keys())
def test_failures_block_only_the_failing_client(flow):
    world = flow.prepare()
    blocked = _client_at("100.64.0.11")
    other = _client_at("100.64.0.12")
    for client in (blocked, other):
        flow.sign_in(client, world)
    for _ in range(2):
        flow.fail(blocked, world)
    throttled = _throttle_rows()

    # The failing client's correct credential gets the generic failure and changes nothing...
    flow.refused(flow.attempt(blocked, world), blocked, world)
    assert _throttle_rows() == throttled
    # ...while another client's identical attempt goes through.
    flow.accepted(flow.attempt(other, world), other, world)


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
    username_row = LoginThrottle.objects.get(key_digest=throttle_key(user.username, "").username_digest)
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
    username_row = LoginThrottle.objects.get(key_digest=throttle_key(user.username, "").username_digest)
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


@pytest.mark.django_db
@THROTTLE_SETTINGS
@override_settings(LOGIN_USERNAME_FAILURE_LIMIT=5)
def test_a_successful_sign_in_keeps_failures_other_clients_sent_against_the_name():
    user, _person, _household = make_member()
    _fail_from_rotating_clients(user.username, 3)

    member = _client_at("100.64.2.1")
    member.post(reverse("login"), {"username": user.username, "password": "wrong"})
    assert member.post(reverse("login"), {"username": user.username, "password": PASSWORD}).status_code == 302

    key = throttle_key(user.username, "100.64.2.1")
    assert not LoginThrottle.objects.filter(key_digest=key.pair_digest).exists()
    assert LoginThrottle.objects.get(key_digest=key.username_digest).failure_count == 4
    _fail_from_rotating_clients(user.username, 1)
    assert login_is_blocked(throttle_key(user.username, "100.64.9.9"))


@pytest.mark.django_db
@THROTTLE_SETTINGS
@override_settings(LOGIN_USERNAME_FAILURE_LIMIT=3)
def test_a_lookalike_spelling_of_a_capped_name_does_not_sign_in():
    # PostgreSQL's UPPER() maps a dotless i (U+0131) to I, so a case-insensitive
    # lookup would accept "alıce" for "alice" under its own throttle buckets.
    # SQLite folds ASCII only, so this test proves the fix on PostgreSQL.
    user, _person, _household = make_member("alice")
    lookalike = "alıce"
    with connection.cursor() as cursor:
        cursor.execute("SELECT UPPER(%s) = UPPER(%s)", [lookalike, user.username])
        folds_together = bool(cursor.fetchone()[0])
    if connection.vendor == "postgresql":
        assert folds_together, "the regression needs a database whose UPPER() folds the dotless i"
    _fail_from_rotating_clients(user.username, 3)

    fresh = _client_at("100.64.2.1")
    response = fresh.post(reverse("login"), {"username": lookalike, "password": PASSWORD})

    assert b"Sign-in failed" in response.content
    assert "_auth_user_id" not in fresh.session
