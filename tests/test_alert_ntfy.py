import logging
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance import alert_ntfy
from finance.alert_email import notify_after_alert_run
from finance.alert_ntfy import (
    FakeTransport, HttpTransport, fake_transport, get_transport, ntfy_notices_available, send_run_ntfy,
    send_test_ntfy, valid_topic,
)
from finance.alert_services import raise_alert, save_alert_settings, settings_for
from finance.models import Alert, AlertSettings, AuditEvent
from finance.reauth import RECENT_AUTH_SESSION_KEY
from finance.weekly_digest import DigestItem, WeeklyDigest
from tests.test_alerts import make_account, make_household, make_person

pytestmark = pytest.mark.django_db

TOKEN = "tk_synthetic_ntfy_token_0000"
TOPIC = "synthetic-topic_1"


@pytest.fixture(autouse=True)
def outbox():
    fake_transport.messages.clear()
    yield fake_transport.messages
    fake_transport.messages.clear()


@pytest.fixture
def ntfy(settings):
    settings.NTFY_URL = "https://ntfy.example.invalid"
    settings.NTFY_TOKEN = TOKEN
    settings.ALERT_EMAIL_BASE_URL = "https://planner.example.invalid"


def opt_in(person, topic=TOPIC):
    prefs = settings_for(person)
    prefs.ntfy_enabled = True
    prefs.ntfy_topic = topic
    prefs.save()
    return prefs


def price_alert(person, key="price", title="Synthetic Stream changed from $10.00 to $12.50", account=None):
    return raise_alert([person], Alert.Kind.RECURRING_PRICE, title, "/recurring/", key, account=account)


def empty_digest(**counts):
    item = DigestItem("Synthetic", "detail", "$1.00", date(2026, 10, 5), "/")
    return WeeklyDigest(
        week="last", start=date(2026, 10, 5), end=date(2026, 10, 11),
        new_series=[item] * counts.get("new", 0), price_changes=[item] * counts.get("price", 0),
        large_transactions=[item] * counts.get("large", 0), large_threshold_display=None,
    )


def test_test_settings_never_select_the_http_transport():
    assert django_settings.NTFY_TRANSPORT == "fake"
    assert django_settings.NTFY_URL == ""
    assert isinstance(get_transport(), FakeTransport)


def test_off_by_default_and_without_url(settings):
    person = make_person("synthetic")
    prefs = settings_for(person)
    assert not prefs.ntfy_enabled and prefs.ntfy_topic == ""
    price_alert(person)
    assert not ntfy_notices_available()
    opt_in(person)
    send_run_ntfy()
    assert not send_test_ntfy(prefs)
    settings.NTFY_URL = "https://ntfy.example.invalid"
    prefs.ntfy_enabled = False
    prefs.save()
    send_run_ntfy()
    assert not fake_transport.messages


@pytest.mark.parametrize("topic, ok", [
    ("a", True), ("A-z_09", True), ("x" * 64, True), ("", False), ("x" * 65, False),
    ("has space", False), ("slash/topic", False), ("dot.topic", False), ("topic\n", False), ("é", False),
])
def test_topic_validation(topic, ok):
    assert valid_topic(topic) is ok


def test_price_changes_send_one_message_per_member_per_run(ntfy, outbox):
    first = make_person("first")
    second = make_person("second")
    opt_in(first)
    opt_in(second, "second-topic")
    price_alert(first)
    price_alert(first, "price2", "Synthetic Gym changed from $30.00 to $35.00")
    price_alert(second)
    raise_alert([first], Alert.Kind.SYNC, "Synthetic sync failure", "/accounts/", "sync")
    send_run_ntfy()
    assert len(outbox) == 2
    message = next(m for m in outbox if m["topic"] == TOPIC)
    assert message["title"] == "2 recurring price changes"
    assert message["body"] == (
        "Synthetic Stream changed from $10.00 to $12.50\n"
        "Synthetic Gym changed from $30.00 to $35.00\n\n"
        "Review recurring charges: https://planner.example.invalid/recurring/"
    )
    assert message["click"] == "https://planner.example.invalid/recurring/"
    assert "sync" not in message["body"].lower()
    send_run_ntfy()
    assert len(outbox) == 2
    assert not Alert.objects.filter(kind=Alert.Kind.RECURRING_PRICE, ntfy_notice_sent_at__isnull=True).exists()


def test_disabled_kind_read_and_invisible_alerts_send_nothing(ntfy, outbox):
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    prefs = opt_in(member)
    hidden = price_alert(member, "hidden", account=make_account(owner))
    assert hidden
    read = price_alert(member, "read")
    Alert.objects.filter(pk=read[0].pk).update(read_at=timezone.now())
    send_run_ntfy()
    assert not outbox
    price_alert(member, "new")
    prefs.recurring_price_enabled = False
    prefs.save()
    send_run_ntfy()
    assert not outbox


def test_long_lists_are_capped(ntfy, outbox):
    person = make_person("synthetic")
    opt_in(person)
    for index in range(12):
        price_alert(person, f"p{index}", f"Series {index} changed from $1.00 to $2.00")
    send_run_ntfy()
    lines = outbox[0]["body"].split("\n")
    assert lines[10] == "and 2 more"
    assert outbox[0]["title"] == "12 recurring price changes"


def test_without_base_url_messages_have_no_link(ntfy, settings, outbox):
    settings.ALERT_EMAIL_BASE_URL = ""
    person = make_person("synthetic")
    opt_in(person)
    price_alert(person)
    send_run_ntfy()
    assert outbox[0]["body"] == "Synthetic Stream changed from $10.00 to $12.50"
    assert outbox[0]["click"] == ""


def test_weekly_digest_summary_is_sent_once_per_week(ntfy, outbox):
    person = make_person("synthetic")
    prefs = opt_in(person)
    with patch("finance.weekly_digest.build_weekly_digest", return_value=empty_digest(new=1, price=2, large=1)):
        send_run_ntfy()
        send_run_ntfy()
    assert len(outbox) == 1
    assert outbox[0]["title"] == "Weekly digest"
    assert outbox[0]["body"] == (
        "Week of Oct 5 to Oct 11: 1 new recurring series, 2 price changes, 1 large transaction.\n\n"
        "Open the digest: https://planner.example.invalid/?digest_week=last#weekly-digest"
    )
    prefs.refresh_from_db()
    assert prefs.ntfy_digest_week == date(2026, 10, 5)


def test_empty_digest_sends_nothing(ntfy, outbox):
    opt_in(make_person("synthetic"))
    with patch("finance.weekly_digest.build_weekly_digest", return_value=empty_digest()):
        send_run_ntfy()
    assert not outbox


def test_alert_run_delivers_after_commit_and_failure_does_not_break_it(
    ntfy, outbox, caplog, django_capture_on_commit_callbacks,
):
    person = make_person("synthetic")
    opt_in(person)

    @notify_after_alert_run
    def run():
        price_alert(person)
        return "done"

    with django_capture_on_commit_callbacks(execute=True):
        assert run() == "done"
    assert len(outbox) == 1
    price_alert(person, "later")
    with patch.object(FakeTransport, "send", side_effect=RuntimeError(f"{TOKEN} {TOPIC} body")):
        with django_capture_on_commit_callbacks(execute=True):
            assert run() == "done"
    later = Alert.objects.get(dedupe_key="later")
    assert later.ntfy_notice_sent_at is None
    assert "Alert ntfy delivery failed." in caplog.text


def test_unexpected_member_error_is_contained(ntfy, caplog):
    first = make_person("first")
    second = make_person("second")
    opt_in(first)
    opt_in(second, "second-topic")
    price_alert(first)
    price_alert(second)
    real = alert_ntfy._send_member_ntfy
    calls = []

    def flaky(preference_id):
        calls.append(preference_id)
        if len(calls) == 1:
            raise RuntimeError(TOKEN)
        return real(preference_id)

    with patch("finance.alert_ntfy._send_member_ntfy", side_effect=flaky):
        send_run_ntfy()
    assert len(calls) == 2
    assert len(fake_transport.messages) == 1
    assert TOKEN not in caplog.text


def test_logs_never_contain_token_topic_or_body(ntfy, caplog):
    caplog.set_level(logging.DEBUG)
    person = make_person("synthetic")
    prefs = opt_in(person)
    price_alert(person)
    send_run_ntfy()
    assert send_test_ntfy(prefs)
    with patch.object(FakeTransport, "send", side_effect=RuntimeError(f"{TOKEN} {TOPIC} Synthetic Stream")):
        assert not send_test_ntfy(prefs)
        price_alert(person, "again", "Other Synthetic changed from $1.00 to $2.00")
        send_run_ntfy()
    with patch.object(FakeTransport, "send", return_value=False):
        assert not send_test_ntfy(prefs)
    for secret in (TOKEN, TOPIC, "Synthetic Stream", "$12.50", "Other Synthetic", "test notice"):
        assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "Alert ntfy delivery failed." in caplog.text


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_http_transport_request_shape(settings):
    settings.NTFY_URL = "https://ntfy.example.invalid"
    settings.NTFY_TOKEN = TOKEN
    settings.NTFY_TIMEOUT_SECONDS = 5
    transport = HttpTransport()
    transport.opener = MagicMock()
    transport.opener.open.return_value = _Response()
    assert transport.send(TOPIC, "Weekly digest", "Synthetic body", "https://planner.example.invalid/", "calendar")
    request = transport.opener.open.call_args.args[0]
    assert transport.opener.open.call_args.kwargs == {"timeout": 5}
    assert request.full_url == f"https://ntfy.example.invalid/{TOPIC}"
    assert request.get_method() == "POST"
    assert request.data == b"Synthetic body"
    headers = dict(request.header_items())
    assert headers["Title"] == "Weekly digest"
    assert headers["Click"] == "https://planner.example.invalid/"
    assert headers["Tags"] == "calendar"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert [name for name, value in headers.items() if TOKEN in value] == ["Authorization"]
    assert TOKEN not in request.full_url
    assert transport.opener.open.call_count == 1


def test_http_transport_without_token_or_link(settings):
    settings.NTFY_URL = "http://ntfy.example.invalid"
    settings.NTFY_TOKEN = ""
    transport = HttpTransport()
    transport.opener = MagicMock()
    transport.opener.open.return_value = _Response()
    transport.send(TOPIC, "Title", "Body", "", "")
    headers = dict(transport.opener.open.call_args.args[0].header_items())
    assert "Authorization" not in headers and "Click" not in headers and "Tags" not in headers


def test_http_transport_refuses_redirects():
    handler = alert_ntfy._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid/") is None


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def stamp_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()


def test_controls_hidden_without_url_and_post_cannot_enable():
    person = make_person("synthetic")
    client = signed_in(person)
    url = reverse("settings-alerts")
    assert b"ntfy topic" not in client.get(url).content
    client.post(url, {"action": "save-ntfy", "ntfy_enabled": "on", "ntfy_topic": TOPIC})
    client.post(url, {"action": "test-ntfy"})
    assert not settings_for(person).ntfy_enabled
    assert not fake_transport.messages


def test_settings_save_validate_reauth_and_audit(ntfy):
    person = make_person("synthetic")
    client = signed_in(person)
    url = reverse("settings-alerts")
    assert b"ntfy topic" in client.get(url).content
    response = client.post(url, {"action": "save-ntfy", "ntfy_enabled": "on", "ntfy_topic": TOPIC})
    assert response.url.startswith(reverse("reauth"))
    assert settings_for(person).ntfy_topic == ""
    stamp_auth(client)
    for bad in ("", "bad topic", "x" * 65):
        response = client.post(url, {"action": "save-ntfy", "ntfy_enabled": "on", "ntfy_topic": bad})
        assert response.status_code == 200
        assert response.context["ntfy_settings_form"].errors
    assert client.post(url, {"action": "save-ntfy", "ntfy_enabled": "on", "ntfy_topic": TOPIC}).url == url
    prefs = settings_for(person)
    assert prefs.ntfy_enabled and prefs.ntfy_topic == TOPIC
    event = AuditEvent.objects.filter(action=AuditEvent.Action.NOTIFICATION_ADDRESS_CHANGED).latest("pk")
    assert sorted(event.changed_fields) == ["ntfy_enabled", "ntfy_topic"]
    session = client.session
    session.pop(RECENT_AUTH_SESSION_KEY)
    session.save()
    assert client.post(url, {"action": "save-ntfy", "ntfy_topic": TOPIC}).url == url
    assert not settings_for(person).ntfy_enabled


def test_test_push_reports_success_and_failure(ntfy, outbox):
    person = make_person("synthetic")
    opt_in(person)
    client = signed_in(person)
    url = reverse("settings-alerts")
    response = client.post(url, {"action": "test-ntfy", "ntfy_topic": "someone-else"})
    assert b"Test push sent." in response.content
    assert [m["topic"] for m in outbox] == [TOPIC]
    assert outbox[0]["body"] == (
        "This is a test notice from Financial Planner.\n\n"
        "Open your alerts: https://planner.example.invalid/alerts/"
    )
    with patch.object(FakeTransport, "send", side_effect=OSError("synthetic")):
        response = client.post(url, {"action": "test-ntfy"})
    assert b"Test push could not be sent." in response.content
    prefs = settings_for(person)
    prefs.ntfy_enabled = False
    prefs.save()
    assert b"Test push could not be sent." in client.post(url, {"action": "test-ntfy"}).content
    assert len(outbox) == 1


def test_alert_settings_save_keeps_concurrent_digest_marker(ntfy):
    person = make_person("synthetic")
    stale = opt_in(person)
    AlertSettings.objects.filter(pk=stale.pk).update(ntfy_digest_week=date(2026, 10, 5), ntfy_topic="newer-topic")
    with patch("finance.alert_services.settings_for", return_value=stale):
        save_alert_settings(
            person, sync_enabled=True, recurring_price_enabled=True, recurring_missed_enabled=True,
            budget_enabled=False, large_transaction_enabled=True, monthly_review_enabled=True,
            monthly_review_ai_enabled=True, large_transaction_minor=None,
        )
    current = AlertSettings.objects.get(pk=stale.pk)
    assert current.ntfy_digest_week == date(2026, 10, 5)
    assert current.ntfy_topic == "newer-topic"
    assert not current.budget_enabled
