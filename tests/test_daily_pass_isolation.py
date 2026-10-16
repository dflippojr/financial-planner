"""One failing step or member does not stop the rest of the daily alert pass (#513). Synthetic data only."""

import logging
from contextlib import ExitStack
from datetime import date
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import InterfaceError, OperationalError

from finance.alert_services import DailyAlertPassIncomplete, run_daily_alert_pass
from finance.bills_calendar import evaluate_expected_balance_alerts
from finance.models import AlertSettings
from finance.monthly_review import generate_due_monthly_reviews
from finance.pass_isolation import MemberFailures
from ops.backup.audit_journal import query
from tests.test_recurring import make_household, make_person

pytestmark = pytest.mark.django_db

SECRET = "Synthetic Merchant 12.34"

# Every step of the pass, in order, and the function each one calls.
STEPS = (
    ("sync_alerts", "finance.alert_services.evaluate_sync_alerts"),
    ("recurring_refresh", "finance.alert_services.refresh_all_recurring_series"),
    ("budget_alerts", "finance.alert_services.evaluate_active_budget_alerts"),
    ("large_transaction_alerts", "finance.alert_services.evaluate_recent_large_transactions"),
    ("monthly_reviews", "finance.monthly_review.generate_due_monthly_reviews"),
    ("backup_alerts", "finance.backup_health.evaluate_backup_alerts"),
    ("expected_balance_alerts", "finance.bills_calendar.evaluate_expected_balance_alerts"),
    ("import_due_alerts", "finance.import_due.evaluate_import_due_alerts"),
    ("purge_read_alerts", "finance.alert_services.purge_old_read_alerts"),
    ("purge_security_events", "finance.security_services.purge_old_security_events"),
    ("purge_login_throttles", "finance.auth_services.purge_expired_login_throttles"),
    ("purge_expired_sessions", "finance.security_services.purge_expired_sessions"),
    ("purge_stale_member_sessions", "finance.security_services.purge_stale_member_sessions"),
    ("purge_pending_sign_ins", "finance.passkey_services.purge_stale_pending_sign_ins"),
    ("receipt_sweep", "finance.receipt_services.sweep_orphan_receipt_files"),
)


def patched_steps(stack, failing, error):
    mocks = {}
    for name, target in STEPS:
        side_effect = error if name == failing else None
        mocks[name] = stack.enter_context(patch(target, return_value=[], side_effect=side_effect))
    return mocks


def daily_alert_outcomes(directory):
    return [row["outcome"] for row in query(directory) if row["operation"] == "daily_alerts"]


@pytest.mark.parametrize("failing", [name for name, _target in STEPS])
def test_a_failing_step_does_not_stop_any_other_step(failing, caplog, monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    with ExitStack() as stack:
        mocks = patched_steps(stack, failing, RuntimeError(SECRET))
        stack.enter_context(caplog.at_level(logging.ERROR, logger="finance.alert_services"))
        with pytest.raises(DailyAlertPassIncomplete) as raised:
            run_daily_alert_pass(today=date(2026, 9, 3))

    assert raised.value.steps == (failing,)
    assert all(mock.call_count == 1 for mock in mocks.values())
    assert f"step {failing} failed (RuntimeError)" in caplog.text
    assert SECRET not in caplog.text
    assert daily_alert_outcomes(tmp_path) == ["started", "failed"]


def test_a_clean_pass_records_success(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    with ExitStack() as stack:
        patched_steps(stack, None, None)
        run_daily_alert_pass(today=date(2026, 9, 3))

    assert daily_alert_outcomes(tmp_path) == ["started", "succeeded"]


@pytest.mark.parametrize("error", [OperationalError("synthetic"), InterfaceError("synthetic")])
def test_database_unavailable_stops_the_pass(error, monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    with ExitStack() as stack:
        mocks = patched_steps(stack, "budget_alerts", error)
        stack.enter_context(patch("finance.pass_isolation.connection.is_usable", return_value=False))
        with pytest.raises(type(error)):
            run_daily_alert_pass(today=date(2026, 9, 3))

    assert mocks["backup_alerts"].call_count == 0
    assert daily_alert_outcomes(tmp_path) == ["started", "failed"]


def test_a_deadlock_on_a_live_connection_does_not_stop_the_pass(monkeypatch, tmp_path):
    # PostgreSQL reports deadlocks and lock timeouts as OperationalError too.
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    with ExitStack() as stack:
        mocks = patched_steps(stack, "budget_alerts", OperationalError("synthetic deadlock"))
        with pytest.raises(DailyAlertPassIncomplete) as raised:
            run_daily_alert_pass(today=date(2026, 9, 3))

    assert raised.value.steps == ("budget_alerts",)
    assert mocks["receipt_sweep"].call_count == 1


def test_sync_command_reports_failed_steps_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    stderr = StringIO()
    with ExitStack() as stack:
        patched_steps(stack, "backup_alerts", RuntimeError(SECRET))
        call_command("sync_simplefin", stdout=StringIO(), stderr=stderr)

    assert "backup_alerts" in stderr.getvalue()
    assert SECRET not in stderr.getvalue()
    assert daily_alert_outcomes(tmp_path) == ["started", "failed"]


def test_one_members_monthly_review_failure_does_not_stop_the_others(caplog):
    first = make_person("first")
    second = make_person("second")
    make_household(first, second)
    reached = []

    def store(person, _month, **_kwargs):
        reached.append(person.pk)
        if person.pk == first.pk:
            raise RuntimeError(SECRET)
        return object(), True

    with (
        patch("finance.monthly_review.store_monthly_review", side_effect=store),
        caplog.at_level(logging.ERROR, logger="finance.monthly_review"),
        pytest.raises(MemberFailures),
    ):
        generate_due_monthly_reviews(today=date(2026, 9, 3))

    assert reached == [first.pk, second.pk]
    assert f"member {first.pk} failed (RuntimeError)" in caplog.text
    assert SECRET not in caplog.text


def test_one_members_expected_balance_failure_does_not_stop_the_others(caplog):
    first = make_person("first")
    second = make_person("second")
    make_household(first, second)
    for person in (first, second):
        AlertSettings.objects.update_or_create(person=person, defaults={"expected_balance_enabled": True})
    reached = []

    def evaluate(person, **_kwargs):
        reached.append(person.pk)
        if person.pk == first.pk:
            raise RuntimeError(SECRET)
        return []

    with (
        patch("finance.bills_calendar.evaluate_expected_balance_alert", side_effect=evaluate),
        caplog.at_level(logging.ERROR, logger="finance.bills_calendar"),
        pytest.raises(MemberFailures),
    ):
        evaluate_expected_balance_alerts(today=date(2026, 9, 3))

    assert sorted(reached) == sorted([first.pk, second.pk])
    assert f"member {first.pk} failed (RuntimeError)" in caplog.text
    assert SECRET not in caplog.text


def test_database_unavailable_in_a_member_loop_still_propagates():
    first = make_person("first")
    make_household(first)

    with (
        patch("finance.monthly_review.store_monthly_review", side_effect=OperationalError("synthetic")),
        patch("finance.pass_isolation.connection.is_usable", return_value=False),
        pytest.raises(OperationalError),
    ):
        generate_due_monthly_reviews(today=date(2026, 9, 3))


def test_a_members_failure_marks_the_daily_pass_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    first = make_person("first")
    second = make_person("second")
    make_household(first, second)
    reviewed = []

    def store(person, _month, **_kwargs):
        if person.pk == first.pk:
            raise RuntimeError(SECRET)
        reviewed.append(person.pk)
        return object(), False

    with (
        patch("finance.monthly_review.store_monthly_review", side_effect=store),
        pytest.raises(DailyAlertPassIncomplete) as raised,
    ):
        run_daily_alert_pass(today=date(2026, 9, 3))

    assert raised.value.steps == ("monthly_reviews",)
    assert reviewed == [second.pk]
    assert daily_alert_outcomes(tmp_path) == ["started", "failed"]
