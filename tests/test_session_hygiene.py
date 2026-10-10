"""Daily purges of auth state, and session revocation through the indexes (#302)."""

from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.auth_services import create_recovery_codes, revoke_user_sessions, throttle_key
from finance.lifecycle_services import delete_member_data
from finance.models import LoginThrottle, MemberSession, PendingSignInSession
from tests.test_auth_flows import PASSWORD, make_member
from tests.test_passkeys import PASSKEY_SETTINGS, _require_passkey_after_password


THROTTLE_SETTINGS = override_settings(
    LOGIN_FAILURE_LIMIT=2, LOGIN_FAILURE_WINDOW_SECONDS=900, LOGIN_BLOCK_SECONDS=900
)


def _fail(client, username):
    client.post(reverse("login"), {"username": username, "password": "wrong-synthetic"})


def _signed_in(user):
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert client.session.get("_auth_user_id") == str(user.pk)
    return client


def _pending_passkey(user):
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert "_auth_user_id" not in client.session
    assert PendingSignInSession.objects.filter(session_key=client.session.session_key, user=user).exists()
    return client


@pytest.mark.django_db
@THROTTLE_SETTINGS
def test_daily_pass_purges_expired_throttles_and_keeps_active_blocks():
    make_member()
    client = Client()
    for index in range(12):
        _fail(client, f"unknown-synthetic-{index}")
    assert LoginThrottle.objects.count() == 12
    past = timezone.now() - timedelta(seconds=1801)
    LoginThrottle.objects.update(window_started_at=past)

    blocked_key = throttle_key("blocked-synthetic", "127.0.0.1")
    LoginThrottle.objects.create(
        key_digest=blocked_key,
        failure_count=2,
        window_started_at=past,
        blocked_until=timezone.now() + timedelta(minutes=5),
    )
    open_window_key = throttle_key("recent-synthetic", "127.0.0.1")
    LoginThrottle.objects.create(key_digest=open_window_key, failure_count=1, window_started_at=timezone.now())
    ended_block_key = throttle_key("ended-synthetic", "127.0.0.1")
    LoginThrottle.objects.create(
        key_digest=ended_block_key, failure_count=2, window_started_at=past, blocked_until=past + timedelta(seconds=900)
    )

    run_daily_alert_pass()

    assert set(LoginThrottle.objects.values_list("key_digest", flat=True)) == {blocked_key, open_window_key}


@pytest.mark.django_db
@THROTTLE_SETTINGS
def test_successful_sign_in_clears_only_its_own_throttle_key():
    user, _person, _household = make_member()
    client = Client()
    _fail(client, user.username)
    _fail(client, "unknown-synthetic")
    assert LoginThrottle.objects.count() == 2

    _signed_in(user)

    assert list(LoginThrottle.objects.values_list("key_digest", flat=True)) == [
        throttle_key("unknown-synthetic", "127.0.0.1")
    ]


@pytest.mark.django_db
def test_daily_pass_deletes_expired_sessions_and_keeps_live_ones():
    user, _person, _household = make_member()
    live = _signed_in(user)
    expired = _signed_in(user)
    expired_key = expired.session.session_key
    Session.objects.filter(session_key=expired_key).update(expire_date=timezone.now() - timedelta(seconds=1))

    run_daily_alert_pass()

    assert not Session.objects.filter(session_key=expired_key).exists()
    assert not MemberSession.objects.filter(session_key=expired_key).exists()
    assert Session.objects.filter(session_key=live.session.session_key).exists()
    assert live.get(reverse("home")).status_code == 200


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_daily_pass_drops_pending_sign_in_index_rows_for_expired_sessions():
    user, _person, _household = make_member()
    _require_passkey_after_password(user)
    kept = _pending_passkey(user)
    gone = _pending_passkey(user)
    Session.objects.filter(session_key=gone.session.session_key).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )

    run_daily_alert_pass()

    assert list(PendingSignInSession.objects.values_list("session_key", flat=True)) == [kept.session.session_key]


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_revoking_one_member_does_not_decode_other_members_sessions():
    target, _person, _household = make_member("target")
    other, _other_person, _other_household = make_member("other")
    _require_passkey_after_password(target)
    _require_passkey_after_password(other)
    target_signed_in = _signed_in_without_passkey(target)
    target_pending = _pending_passkey(target)
    other_signed_in = _signed_in_without_passkey(other)
    other_pending = _pending_passkey(other)

    with mock.patch.object(Session, "get_decoded", autospec=True) as decoded:
        revoke_user_sessions(target)

    decoded.assert_not_called()
    assert not Session.objects.filter(session_key=target_signed_in.session.session_key).exists()
    assert not Session.objects.filter(session_key=target_pending.session.session_key).exists()
    assert Session.objects.filter(session_key=other_signed_in.session.session_key).exists()
    assert Session.objects.filter(session_key=other_pending.session.session_key).exists()
    assert not PendingSignInSession.objects.filter(user=target).exists()


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_recovery_removes_signed_in_and_pending_sessions():
    user, _person, _household = make_member()
    code = create_recovery_codes(user, count=1)[0]
    _require_passkey_after_password(user)
    signed_in = _signed_in_without_passkey(user)
    pending = _pending_passkey(user)

    Client().post(
        reverse("recover"),
        {
            "username": user.username,
            "recovery_code": code,
            "password1": "Another-synthetic-passphrase-84!",
            "password2": "Another-synthetic-passphrase-84!",
        },
    )

    assert not Session.objects.filter(session_key=signed_in.session.session_key).exists()
    assert not Session.objects.filter(session_key=pending.session.session_key).exists()
    assert not PendingSignInSession.objects.filter(user=user).exists()


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_delete_my_data_removes_signed_in_and_pending_sessions():
    user, person, _household = make_member()
    _require_passkey_after_password(user)
    signed_in = _signed_in_without_passkey(user)
    pending = _pending_passkey(user)
    signed_in_key = signed_in.session.session_key
    pending_key = pending.session.session_key

    delete_member_data(user)

    assert not Session.objects.filter(session_key__in=(signed_in_key, pending_key)).exists()
    assert not MemberSession.objects.filter(member_id=person.pk).exists()
    assert not PendingSignInSession.objects.exists()


def _signed_in_without_passkey(user):
    # force_login skips the passkey step; the middleware indexes the session.
    client = Client()
    client.force_login(user)
    assert client.get(reverse("home")).status_code == 200
    assert MemberSession.objects.filter(session_key=client.session.session_key).exists()
    return client
