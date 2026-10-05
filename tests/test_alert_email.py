from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.alert_email import (
    email_notices_available, notify_after_alert_run, send_run_notices, send_test_notice,
)
from finance.alert_services import raise_alert, run_daily_alert_pass, schedule_after_new_transactions, settings_for
from finance.models import Alert
from finance.reauth import RECENT_AUTH_SESSION_KEY
from finance.simplefin_errors import SimpleFinError
from finance.simplefin_services import sync_connection
from tests.test_alerts import make_account, make_household, make_person
from tests.test_simplefin import connect_owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def smtp(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_HOST = "smtp.example.invalid"
    settings.DEFAULT_FROM_EMAIL = "planner@example.invalid"
    settings.ALERT_EMAIL_BASE_URL = "https://planner.example.invalid"


def opt_in(person):
    prefs = settings_for(person)
    prefs.email_enabled = True
    prefs.notification_email = f"{person.user.username}@example.invalid"
    prefs.save()
    return prefs


def new_alert(person, key="new", kind=Alert.Kind.SYNC, account=None):
    return raise_alert(
        [person], kind, "Secret Merchant $123.45 Synthetic Checking",
        "/accounts/?secret=detail", key, account=account,
    )


def test_defaults_and_unconfigured_send_nothing(smtp, settings):
    person = make_person("synthetic")
    prefs = settings_for(person)
    assert not prefs.email_enabled
    ids = [a.pk for a in new_alert(person)]
    send_run_notices(ids)
    assert not mail.outbox
    prefs = opt_in(person)
    settings.EMAIL_HOST = ""
    assert not email_notices_available()
    send_run_notices(ids)
    assert not send_test_notice(prefs)
    assert not mail.outbox


def test_notice_contains_only_count_kinds_and_inbox_url(smtp):
    person = make_person("synthetic")
    opt_in(person)
    account = make_account(person, name="Private Account Name")
    alerts = new_alert(person)
    alerts += new_alert(person, "large", Alert.Kind.LARGE_TRANSACTION, account)
    alerts += new_alert(person, "sync2")
    send_run_notices([a.pk for a in alerts])
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.body == (
        "You have 3 new alerts in Financial Planner: Large transaction, Sync.\n\n"
        "Open your alerts: https://planner.example.invalid/alerts/\n"
    )
    assert message.subject == "Financial Planner alerts"
    for detail in ("Secret Merchant", "$123.45", "Synthetic Checking", "Private Account Name", "secret=detail"):
        assert detail not in message.body + message.subject


def test_excludes_read_old_and_invisible_alerts(smtp):
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    opt_in(member)
    private = make_account(owner)
    hidden = new_alert(member, "hidden", Alert.Kind.LARGE_TRANSACTION, private)
    read = new_alert(member, "read")
    Alert.objects.filter(pk=read[0].pk).update(read_at=timezone.now())
    new_alert(member, "old")
    send_run_notices([a.pk for a in hidden + read])
    send_run_notices([])
    assert not mail.outbox


def test_one_per_recipient_nested_runs_and_no_dedupe_repeats(smtp, django_capture_on_commit_callbacks):
    owner = make_person("owner")
    other = make_person("other")
    opt_in(owner)
    opt_in(other)

    @notify_after_alert_run
    def nested():
        new_alert(owner, "nested")
        new_alert(other, "other")

    @notify_after_alert_run
    def run():
        new_alert(owner)
        nested()

    with django_capture_on_commit_callbacks(execute=True):
        run()
        assert not mail.outbox
    assert len(mail.outbox) == 2
    with django_capture_on_commit_callbacks(execute=True):
        run()
    assert len(mail.outbox) == 2
    assert {tuple(m.to) for m in mail.outbox} == {
        ("owner@example.invalid",), ("other@example.invalid",),
    }


def test_rollback_never_sends(smtp, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    opt_in(person)

    @notify_after_alert_run
    def run():
        new_alert(person)

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ValueError), transaction.atomic():
            run()
            raise ValueError("synthetic rollback")
    assert not mail.outbox
    assert not Alert.objects.exists()


def test_deferred_transaction_alerts_join_run_batch(smtp, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    opt_in(person)

    @notify_after_alert_run
    def run():
        new_alert(person)
        schedule_after_new_transactions([])

    with patch("finance.alert_services.after_new_transactions", side_effect=lambda rows: new_alert(person, "deferred")):
        with django_capture_on_commit_callbacks(execute=True):
            run()
            assert not mail.outbox
    assert len(mail.outbox) == 1
    assert "2 new alerts" in mail.outbox[0].body


def test_failure_logs_no_address_or_exception_and_keeps_inbox(smtp, caplog, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    prefs = opt_in(person)

    @notify_after_alert_run
    def run():
        new_alert(person)

    with patch("finance.alert_email.send_mail", side_effect=RuntimeError(prefs.notification_email)):
        with django_capture_on_commit_callbacks(execute=True):
            run()
    assert Alert.objects.filter(recipient=person, read_at__isnull=True).count() == 1
    assert "Alert email delivery failed." in caplog.text
    assert prefs.notification_email not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_zero_sent_is_logged_and_next_member_still_receives(smtp, caplog):
    person = make_person("synthetic")
    prefs = opt_in(person)
    with patch("finance.alert_email.send_mail", return_value=0):
        assert not send_test_notice(prefs)
    assert "Alert email delivery failed." in caplog.text
    assert send_test_notice(prefs)
    assert len(mail.outbox) == 1
    assert "test notice" in mail.outbox[0].body


def test_daily_pass_captures_alerts_not_in_return_list(smtp, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    opt_in(person)
    with patch("finance.monthly_review.generate_due_monthly_reviews", side_effect=lambda **kw: new_alert(person)):
        with django_capture_on_commit_callbacks(execute=True):
            run_daily_alert_pass()
    assert len(mail.outbox) == 1


def test_failed_sync_delivers_generic_notice(smtp, monkeypatch, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    make_household(person)
    connection = connect_owner(person, monkeypatch)
    opt_in(person)
    with patch("finance.simplefin_services.fetch_accounts", side_effect=SimpleFinError("Synthetic failure")):
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(SimpleFinError):
                sync_connection(person, connection.pk)
    assert len(mail.outbox) == 1
    assert "1 new alert" in mail.outbox[0].body
    assert "Synthetic failure" not in mail.outbox[0].body


def test_successful_sync_in_outer_transaction_delivers_after_callbacks(smtp, monkeypatch, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    make_household(person)
    connection = connect_owner(person, monkeypatch)
    opt_in(person)
    with patch("finance.alert_services.after_new_transactions", side_effect=lambda rows: new_alert(person)):
        with django_capture_on_commit_callbacks(execute=True):
            with transaction.atomic():
                result = sync_connection(person, connection.pk)
            assert result["imported"] == 0
            assert not mail.outbox
    assert len(mail.outbox) == 1


def test_scheduler_combines_sync_and_daily_notices(smtp, monkeypatch, django_capture_on_commit_callbacks):
    person = make_person("synthetic")
    make_household(person)
    connect_owner(person, monkeypatch)
    opt_in(person)
    with patch("finance.management.commands.sync_simplefin.sync_all_connections", side_effect=lambda: new_alert(person)), \
         patch("finance.management.commands.sync_simplefin.run_daily_alert_pass", side_effect=lambda: new_alert(person, "daily")):
        with django_capture_on_commit_callbacks(execute=True):
            call_command("sync_simplefin")
    assert len(mail.outbox) == 1
    assert "2 new alerts" in mail.outbox[0].body


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def stamp_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()


def test_controls_hidden_without_smtp_and_post_cannot_enable():
    person = make_person("synthetic")
    client = signed_in(person)
    url = reverse("settings-alerts")
    assert b"Notification address" not in client.get(url).content
    client.post(url, {"action": "save-email", "email_enabled": "on", "notification_email": "synthetic@example.invalid"})
    assert not settings_for(person).email_enabled
    assert settings_for(person).notification_email == ""


def test_address_change_requires_auth_enable_disable_does_not(smtp):
    person = make_person("synthetic")
    other = make_person("other")
    prefs = opt_in(person)
    client = signed_in(person)
    url = reverse("settings-alerts")
    assert b"Notification address" in client.get(url).content
    data = {"action": "save-email", "email_enabled": "on", "notification_email": "changed@example.invalid"}
    response = client.post(url, data)
    assert response.url.startswith(reverse("reauth"))
    prefs.refresh_from_db()
    assert prefs.notification_email == "synthetic@example.invalid"
    stamp_auth(client)
    client.post(url, data)
    prefs.refresh_from_db()
    assert prefs.email_enabled
    assert prefs.notification_email == "changed@example.invalid"
    assert settings_for(other).notification_email == ""
    session = client.session
    session.pop(RECENT_AUTH_SESSION_KEY)
    session.save()
    data.pop("email_enabled")
    assert client.post(url, data).url == url
    prefs.refresh_from_db()
    assert not prefs.email_enabled


@pytest.mark.parametrize("address", ["", "invalid address"])
def test_address_validation(smtp, address):
    person = make_person("synthetic")
    client = signed_in(person)
    stamp_auth(client)
    response = client.post(reverse("settings-alerts"), {
        "action": "save-email", "email_enabled": "on", "notification_email": address,
    })
    assert response.status_code == 200
    assert response.context["email_settings_form"].errors
    assert not settings_for(person).email_enabled


def test_test_button_uses_only_saved_own_address(smtp):
    person = make_person("synthetic")
    opt_in(person)
    client = signed_in(person)
    response = client.post(reverse("settings-alerts"), {
        "action": "test-email", "notification_email": "someone-else@example.invalid",
    })
    assert b"Test notice sent." in response.content
    assert mail.outbox[0].to == ["synthetic@example.invalid"]
    assert not Alert.objects.exists()
    prefs = settings_for(person)
    prefs.email_enabled = False
    prefs.save()
    response = client.post(reverse("settings-alerts"), {"action": "test-email"})
    assert b"Test notice could not be sent." in response.content
    assert len(mail.outbox) == 1
