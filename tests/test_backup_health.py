from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.backup_health import backup_is_unhealthy, evaluate_backup_alerts
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
