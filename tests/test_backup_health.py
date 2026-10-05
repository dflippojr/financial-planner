from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.backup_health import backup_alert_title, backup_is_unhealthy, evaluate_backup_alerts
from finance.models import Alert, Household, Membership
from tests.test_alerts import make_person, signed_in


def _write_status(path, **fields):
    defaults = {
        "last_success_at": "",
        "dump_name": "",
        "size_bytes": "",
        "table_count": "",
        "last_error": "",
        "offsite_success_at": "",
        "offsite_error": "",
        "offsite_configured": "",
    }
    defaults.update(fields)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in defaults.items()),
        encoding="utf-8",
    )


@pytest.mark.django_db
def test_backup_alert_fires_once_per_day_and_clears_after_success(tmp_path, settings):
    operator = make_person("operator")
    roommate = make_person("roommate")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=operator, household=household)
    Membership.objects.create(person=roommate, household=household)
    status = tmp_path / "status"
    stale = (timezone.now() - timedelta(hours=27)).astimezone(dt_timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _write_status(status, last_success_at=stale, dump_name="financial_planner_synthetic.dump")

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        first = evaluate_backup_alerts(today=datetime(2026, 10, 4).date())
        second = evaluate_backup_alerts(today=datetime(2026, 10, 4).date())
        assert len(first) == 1
        assert second == []
        assert Alert.objects.filter(kind=Alert.Kind.BACKUP, recipient=operator).count() == 1
        assert not Alert.objects.filter(kind=Alert.Kind.BACKUP, recipient=roommate).exists()
        alert = Alert.objects.get(kind=Alert.Kind.BACKUP)
        assert alert.dedupe_key == "backup:2026-10-04"
        assert "dump" not in alert.title.lower()
        assert "synthetic" not in alert.title.lower()

        _write_status(
            status,
            last_success_at="2026-10-04T06:00:00Z",
            dump_name="financial_planner_synthetic.dump",
        )
        evaluate_backup_alerts(today=datetime(2026, 10, 4).date())
        assert not Alert.objects.filter(kind=Alert.Kind.BACKUP).exists()


@pytest.mark.django_db
def test_failed_backup_run_alerts_earliest_member_when_operators_unset(tmp_path):
    first = make_person("first")
    make_person("second")
    status = tmp_path / "status"
    _write_status(status, last_error="pg_dump failed")

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES=""):
        created = evaluate_backup_alerts(today=datetime(2026, 10, 4).date())

    assert [row.recipient_id for row in created] == [first.pk]
    assert created[0].title == "The latest backup run failed"


@pytest.mark.django_db
def test_daily_alert_pass_includes_backup_health(tmp_path):
    make_person("operator")
    status = tmp_path / "status"
    _write_status(status, last_error="Off-site upload failed")

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        run_daily_alert_pass(today=datetime(2026, 10, 4).date(), now=timezone.now())

    assert Alert.objects.filter(kind=Alert.Kind.BACKUP, dedupe_key="backup:2026-10-04").exists()


@pytest.mark.django_db
def test_non_operators_do_not_see_backup_status(tmp_path):
    operator = make_person("operator")
    other = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=operator, household=household)
    Membership.objects.create(person=other, household=household)
    status = tmp_path / "status"
    _write_status(
        status,
        last_success_at="2026-10-04T06:00:00Z",
        dump_name="financial_planner_synthetic.dump",
        offsite_success_at="2026-10-04T06:01:00Z",
    )

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        operator_page = signed_in(operator).get(reverse("settings-data"))
        member_page = signed_in(other).get(reverse("settings-data"))

    assert operator_page.status_code == 200
    assert b"Last local backup" in operator_page.content
    assert b"Last off-site copy" in operator_page.content
    assert member_page.status_code == 200
    assert b"Last local backup" not in member_page.content
    assert b"Last off-site copy" not in member_page.content
    assert b"financial_planner_synthetic.dump" not in member_page.content


def test_missing_backup_status_is_unhealthy():
    assert backup_is_unhealthy(None) is True


@pytest.mark.django_db
def test_missing_status_file_alerts_and_settings_say_none_found(tmp_path):
    operator = make_person("operator")
    missing = tmp_path / "health" / "status"

    with override_settings(BACKUP_STATUS_PATH=str(missing), OPERATOR_USERNAMES="operator"):
        created = evaluate_backup_alerts(today=datetime(2026, 10, 4).date())
        page = signed_in(operator).get(reverse("settings-data"))

    assert [row.title for row in created] == ["No backup status found"]
    assert page.status_code == 200
    assert b"No backup status found" in page.content


@pytest.mark.django_db
def test_offsite_error_alerts_and_is_shown_on_settings(tmp_path):
    operator = make_person("operator")
    status = tmp_path / "status"
    _write_status(
        status,
        last_success_at="2026-10-04T06:00:00Z",
        dump_name="financial_planner_synthetic.dump",
        offsite_configured="1",
        offsite_error="Off-site upload failed",
    )

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        created = evaluate_backup_alerts(today=datetime(2026, 10, 4).date())
        page = signed_in(operator).get(reverse("settings-data"))

    assert [row.title for row in created] == ["The off-site backup copy failed"]
    assert page.status_code == 200
    assert b"Off-site upload failed" in page.content


def test_stale_offsite_copy_is_unhealthy_when_configured():
    now = datetime(2026, 10, 4, 12, tzinfo=dt_timezone.utc)
    stale_offsite = now - timedelta(hours=27)
    configured = {
        "last_error": "",
        "offsite_error": "",
        "offsite_configured": "1",
        "last_success": now,
        "offsite_success": stale_offsite,
    }
    assert backup_is_unhealthy(configured, now=now) is True
    local_only = {
        "last_error": "",
        "offsite_error": "",
        "offsite_configured": "0",
        "last_success": now,
        "offsite_success": stale_offsite,
    }
    assert backup_is_unhealthy(local_only, now=now) is False


NOW = datetime(2026, 10, 4, 12, tzinfo=dt_timezone.utc)


def _healthy_status(**fields):
    status = {
        "last_error": "",
        "last_success": NOW - timedelta(hours=6),
        "offsite_error": "",
        "offsite_configured": "0",
        "offsite_success": None,
        "restore_check_error": "",
        "restore_check_enabled": "1",
        "restore_check": NOW - timedelta(days=2),
    }
    status.update(fields)
    return status


def test_recent_restore_check_pass_is_healthy():
    status = _healthy_status()
    assert backup_is_unhealthy(status, now=NOW) is False


def test_failed_restore_check_is_unhealthy_with_its_own_title():
    status = _healthy_status(restore_check_error="Restore check failed: pg_restore could not restore the dump")
    assert backup_is_unhealthy(status, now=NOW) is True
    assert backup_alert_title(status, now=NOW) == "The weekly restore check failed"


def test_restore_check_older_than_eight_days_is_overdue():
    on_time = _healthy_status(restore_check=NOW - timedelta(days=7, hours=23))
    overdue = _healthy_status(restore_check=NOW - timedelta(days=8, hours=1))
    assert backup_is_unhealthy(on_time, now=NOW) is False
    assert backup_is_unhealthy(overdue, now=NOW) is True
    assert backup_alert_title(overdue, now=NOW) == "The weekly restore check has not passed in 8 days"


def test_restore_check_turned_off_is_never_overdue():
    status = _healthy_status(restore_check_enabled="0", restore_check=NOW - timedelta(days=30))
    assert backup_is_unhealthy(status, now=NOW) is False


def test_status_without_restore_check_keys_counts_as_never_checked():
    # A status file from before the restore check existed.
    status = _healthy_status()
    for key in ("restore_check_error", "restore_check_enabled", "restore_check"):
        del status[key]
    assert backup_is_unhealthy(status, now=NOW) is False
    never_checked = _healthy_status(restore_check=None)
    assert backup_is_unhealthy(never_checked, now=NOW) is False


def test_stale_nightly_backup_title_wins_over_an_overdue_restore_check():
    status = _healthy_status(last_success=NOW - timedelta(days=3), restore_check=NOW - timedelta(days=10))
    assert backup_alert_title(status, now=NOW) == "Nightly backups have not succeeded"


@pytest.mark.django_db
def test_failed_restore_check_alerts_operators(tmp_path):
    operator = make_person("operator")
    status = tmp_path / "status"
    now = timezone.now().astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_status(
        status,
        last_success_at=now,
        dump_name="financial_planner_synthetic.dump",
        restore_check_enabled="1",
        restore_check_error="Restore check failed: pg_restore could not restore the dump",
    )

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        created = evaluate_backup_alerts(today=datetime(2026, 10, 4).date())

    assert [(row.recipient_id, row.title) for row in created] == [(operator.pk, "The weekly restore check failed")]


@pytest.mark.django_db
def test_settings_show_the_restore_check_only_to_operators(tmp_path):
    operator = make_person("operator")
    other = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=operator, household=household)
    Membership.objects.create(person=other, household=household)
    status = tmp_path / "status"
    _write_status(
        status,
        last_success_at="2026-10-04T06:00:00Z",
        restore_check_at="2026-10-03T06:05:00Z",
        restore_check_enabled="1",
        restore_check_error="Restore check failed: core table finance_person is missing",
    )

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        operator_page = signed_in(operator).get(reverse("settings-data"))
        member_page = signed_in(other).get(reverse("settings-data"))

    assert b"Last restore check: 2026-10-03" in operator_page.content
    assert b"core table finance_person is missing" in operator_page.content
    assert b"Last restore check" not in member_page.content
    assert b"core table finance_person is missing" not in member_page.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "shown"),
    [
        ({}, b"Last restore check: Never"),
        ({"restore_check_enabled": "1"}, b"Last restore check: Never"),
        ({"restore_check_enabled": "0", "restore_check_at": "2026-09-01T06:00:00Z"}, b"Last restore check: Off"),
    ],
)
def test_settings_restore_check_line_for_never_checked_and_off(tmp_path, fields, shown):
    operator = make_person("operator")
    status = tmp_path / "status"
    _write_status(status, last_success_at="2026-10-04T06:00:00Z", **fields)

    with override_settings(BACKUP_STATUS_PATH=str(status), OPERATOR_USERNAMES="operator"):
        page = signed_in(operator).get(reverse("settings-data"))

    assert shown in page.content
