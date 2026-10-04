import logging
from datetime import timedelta

import pytest
from django.contrib.sessions.models import Session
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.lifecycle_services import delete_member_data
from finance.models import MemberSecurityEvent, MemberSession
from finance.reauth import RECENT_AUTH_SESSION_KEY
from finance.security_services import (
    EVENT_TYPES,
    SECURITY_EVENT_RETENTION_DAYS,
    client_ip,
    purge_old_security_events,
    record_security_event,
)
from tests.helpers import stamp_recent_auth
from tests.test_auth_flows import PASSWORD, make_member


@pytest.mark.django_db
def test_each_security_event_type_can_be_recorded():
    _user, person, _household = make_member()
    for value, _label in EVENT_TYPES.choices:
        record_security_event(person, value, ip_address="203.0.113.10", user_agent="SyntheticAgent/1.0")
    stored = list(MemberSecurityEvent.objects.visible_to(person).order_by("pk"))
    assert [row.event_type for row in stored] == [choice[0] for choice in EVENT_TYPES.choices]
    outsider, outsider_person, _h = make_member("outsider")
    assert MemberSecurityEvent.objects.visible_to(outsider_person).count() == 0
    assert MemberSecurityEvent.objects.visible_to(outsider).count() == 0
    assert MemberSecurityEvent.objects.visible_to(None).count() == 0


@pytest.mark.django_db
def test_sign_in_success_and_known_failure_are_logged_unknown_username_is_not():
    user, person, _household = make_member()
    client = Client(REMOTE_ADDR="203.0.113.20", HTTP_USER_AGENT="SyntheticLogin/1.0")
    client.post(reverse("login"), {"username": "unknown-member", "password": "not-a-real-password"})
    assert MemberSecurityEvent.objects.count() == 0

    client.post(reverse("login"), {"username": user.username, "password": "not-a-real-password"})
    failure = MemberSecurityEvent.objects.get()
    assert failure.member_id == person.pk
    assert failure.event_type == EVENT_TYPES.SIGN_IN_FAILURE
    assert failure.ip_address == "203.0.113.20"
    assert "SyntheticLogin/1.0" in failure.user_agent

    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    success = MemberSecurityEvent.objects.visible_to(person).filter(event_type=EVENT_TYPES.SIGN_IN_SUCCESS).get()
    assert success.ip_address == "203.0.113.20"


@pytest.mark.django_db
def test_logout_records_sign_out_and_security_page_lists_own_events_newest_first():
    user, person, _household = make_member("owner")
    other, other_person, _h = make_member("other")
    record_security_event(other_person, EVENT_TYPES.SIGN_IN_SUCCESS, ip_address="198.51.100.9")
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    client.post(reverse("logout"))
    types = list(
        MemberSecurityEvent.objects.visible_to(person).order_by("-occurred_at", "-pk").values_list("event_type", flat=True)
    )
    assert types[0] == EVENT_TYPES.SIGN_OUT
    assert EVENT_TYPES.SIGN_IN_SUCCESS in types

    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    page = client.get(reverse("account-settings"))
    html = page.content.decode()
    assert page.status_code == 200
    assert "Signed out" in html
    assert "198.51.100.9" not in html
    assert "session_key" not in html
    assert other.username not in html


@pytest.mark.django_db
def test_revoking_a_session_signs_that_browser_out_and_hides_other_members_sessions():
    user, person, _household = make_member("owner")
    other, other_person, _h = make_member("other")
    here = Client()
    there = Client()
    other_client = Client(REMOTE_ADDR="198.51.100.77")
    here.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    there.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    other_client.post(reverse("login"), {"username": other.username, "password": PASSWORD})
    there_key = there.session.session_key
    other_row = MemberSession.objects.get(member=other_person)
    there_row = MemberSession.objects.get(session_key=there_key)

    stamp_recent_auth(here)
    stolen = here.post(reverse("revoke-session", args=(other_row.pk,)))
    assert stolen.status_code == 404
    assert Session.objects.filter(session_key=other_row.session_key).exists()

    page = here.get(reverse("account-settings"))
    html = page.content.decode()
    assert "This device" in html
    assert "198.51.100.77" not in html

    revoked = here.post(reverse("revoke-session", args=(there_row.pk,)))
    assert revoked.status_code == 302
    assert revoked.url == reverse("account-settings")
    assert not Session.objects.filter(session_key=there_key).exists()

    blocked = there.get(reverse("home"))
    assert blocked.status_code == 302
    assert blocked.url.startswith(reverse("login"))

    still = here.get(reverse("home"))
    assert still.status_code == 200
    assert MemberSession.objects.visible_to(person).count() == 1
    assert MemberSession.objects.visible_to(other_person).count() == 1


@pytest.mark.django_db
def test_sign_out_everywhere_else_revokes_other_sessions_and_requires_recent_auth():
    user, _person, _household = make_member()
    here = Client()
    there = Client()
    here.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    there.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    there_key = there.session.session_key
    session = here.session
    session[RECENT_AUTH_SESSION_KEY] = (timezone.now() - timedelta(minutes=11)).timestamp()
    session.save()

    refused = here.post(reverse("revoke-other-sessions"))
    assert refused.status_code == 302
    assert refused.url.startswith(reverse("reauth"))
    assert Session.objects.filter(session_key=there_key).exists()

    stamp_recent_auth(here)
    done = here.post(reverse("revoke-other-sessions"))
    assert done.status_code == 302
    assert not Session.objects.filter(session_key=there_key).exists()
    assert here.get(reverse("home")).status_code == 200


@pytest.mark.django_db
def test_sign_out_everywhere_else_revokes_sessions_missing_from_the_index():
    user, _person, _household = make_member()
    here = Client()
    there = Client()
    here.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    there.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    there_key = there.session.session_key
    MemberSession.objects.filter(session_key=there_key).delete()
    assert Session.objects.filter(session_key=there_key).exists()

    stamp_recent_auth(here)
    done = here.post(reverse("revoke-other-sessions"))
    assert done.status_code == 302
    assert here.get(reverse("home")).status_code == 200

    blocked = there.get(reverse("home"))
    assert blocked.status_code == 302
    assert blocked.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_ninety_day_purge_removes_old_events_and_runs_from_the_daily_pass():
    _user, person, _household = make_member()
    old = record_security_event(person, EVENT_TYPES.SIGN_IN_SUCCESS)
    kept = record_security_event(person, EVENT_TYPES.SIGN_OUT)
    MemberSecurityEvent.objects.filter(pk=old.pk).update(
        occurred_at=timezone.now() - timedelta(days=SECURITY_EVENT_RETENTION_DAYS + 1)
    )
    MemberSecurityEvent.objects.filter(pk=kept.pk).update(
        occurred_at=timezone.now() - timedelta(days=SECURITY_EVENT_RETENTION_DAYS - 1)
    )
    purge_old_security_events()
    assert not MemberSecurityEvent.objects.filter(pk=old.pk).exists()
    assert MemberSecurityEvent.objects.filter(pk=kept.pk).exists()

    MemberSecurityEvent.objects.filter(pk=kept.pk).update(
        occurred_at=timezone.now() - timedelta(days=SECURITY_EVENT_RETENTION_DAYS + 5)
    )
    run_daily_alert_pass()
    assert not MemberSecurityEvent.objects.filter(pk=kept.pk).exists()


@pytest.mark.django_db
def test_member_data_deletion_removes_events_and_session_index():
    user, person, _household = make_member("gone")
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    record_security_event(person, EVENT_TYPES.MEMBER_DATA_EXPORT)
    assert MemberSession.objects.filter(member=person).exists()
    delete_member_data(user)
    assert not MemberSecurityEvent.objects.filter(member_id=person.pk).exists()
    assert not MemberSession.objects.filter(member_id=person.pk).exists()


@pytest.mark.django_db
def test_recovery_records_code_and_password_events():
    from finance.auth_services import create_recovery_codes

    user, person, _household = make_member()
    code = create_recovery_codes(user, count=1)[0]
    client = Client()
    client.post(
        reverse("recover"),
        {
            "username": user.username,
            "recovery_code": code,
            "password1": "Another-synthetic-passphrase-84!",
            "password2": "Another-synthetic-passphrase-84!",
        },
    )
    types = set(MemberSecurityEvent.objects.visible_to(person).values_list("event_type", flat=True))
    assert EVENT_TYPES.RECOVERY_CODE_USED in types
    assert EVENT_TYPES.PASSWORD_CHANGED in types


@pytest.mark.django_db
@override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_blocked_sign_in_does_not_keep_recording_failures():
    user, person, _household = make_member()
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": "wrong"})
    client.post(reverse("login"), {"username": user.username, "password": "wrong"})
    assert MemberSecurityEvent.objects.filter(event_type=EVENT_TYPES.SIGN_IN_FAILURE).count() == 2
    client.post(reverse("login"), {"username": user.username, "password": "wrong"})
    assert MemberSecurityEvent.objects.filter(event_type=EVENT_TYPES.SIGN_IN_FAILURE).count() == 2


@pytest.mark.django_db
def test_logs_omit_passwords_and_session_keys(caplog):
    user, _person, _household = make_member()
    client = Client()
    secret = "Synthetic-login-secret-99!"
    caplog.set_level(logging.DEBUG)
    client.post(reverse("login"), {"username": user.username, "password": secret})
    session_key = client.session.session_key
    assert secret not in caplog.text
    if session_key:
        assert session_key not in caplog.text


def _client_ip_request(*, remote="127.0.0.1", forwarded=None):
    extra = {}
    if forwarded is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/sign-in/", REMOTE_ADDR=remote, **extra)


def test_client_ip_ignores_forwarded_for_unless_proxy_trust_is_enabled():
    request = _client_ip_request(remote="203.0.113.20", forwarded="198.51.100.1, 192.0.2.60")
    with override_settings(TRUST_PROXY_FORWARDED_FOR=False):
        assert client_ip(request) == "203.0.113.20"
    with override_settings(TRUST_PROXY_FORWARDED_FOR=True):
        assert client_ip(request) == "192.0.2.60"


def test_client_ip_falls_back_when_the_trusted_forwarded_address_is_invalid():
    request = _client_ip_request(remote="203.0.113.20", forwarded="198.51.100.1, not-an-ip")
    with override_settings(TRUST_PROXY_FORWARDED_FOR=True):
        assert client_ip(request) == "203.0.113.20"


@pytest.mark.django_db
@override_settings(TRUST_PROXY_FORWARDED_FOR=False)
def test_sign_in_log_does_not_trust_spoofed_forwarded_for():
    user, person, _household = make_member()
    client = Client(REMOTE_ADDR="203.0.113.20")
    client.post(
        reverse("login"),
        {"username": user.username, "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="198.51.100.1, 192.0.2.60",
    )
    success = MemberSecurityEvent.objects.visible_to(person).get(event_type=EVENT_TYPES.SIGN_IN_SUCCESS)
    assert success.ip_address == "203.0.113.20"


@pytest.mark.django_db
@override_settings(TRUST_PROXY_FORWARDED_FOR=True)
def test_sign_in_log_uses_rightmost_forwarded_for_when_proxy_is_trusted():
    user, person, _household = make_member()
    client = Client(REMOTE_ADDR="127.0.0.1")
    client.post(
        reverse("login"),
        {"username": user.username, "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="198.51.100.1, 192.0.2.60",
    )
    success = MemberSecurityEvent.objects.visible_to(person).get(event_type=EVENT_TYPES.SIGN_IN_SUCCESS)
    assert success.ip_address == "192.0.2.60"

