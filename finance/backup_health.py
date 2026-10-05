from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Alert, Person


STALE_AFTER = timedelta(hours=26)
# The check runs weekly by default; one day of grace before it counts as overdue.
RESTORE_CHECK_STALE_AFTER = timedelta(days=8)
STATUS_KEYS = (
    "last_success_at",
    "dump_name",
    "size_bytes",
    "table_count",
    "last_error",
    "offsite_success_at",
    "offsite_error",
    "offsite_configured",
    "restore_check_at",
    "restore_check_error",
    "restore_check_enabled",
)


def operator_people():
    names = [name.strip() for name in settings.OPERATOR_USERNAMES.split(",") if name.strip()]
    if names:
        return list(
            Person.objects.filter(user__username__in=names).select_related("user").order_by("pk")
        )
    earliest = Person.objects.select_related("user").order_by("created_at", "pk").first()
    return [earliest] if earliest is not None else []


def person_is_operator(person):
    if person is None:
        return False
    return any(operator.pk == person.pk for operator in operator_people())


def _parse_iso(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = parse_datetime(text)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def read_backup_status(path=None):
    status_path = Path(path or settings.BACKUP_STATUS_PATH)
    try:
        text = status_path.read_text(encoding="utf-8")
    except OSError:
        return None
    data = {key: "" for key in STATUS_KEYS}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in data:
            data[key] = value
    data["last_success"] = _parse_iso(data["last_success_at"])
    data["offsite_success"] = _parse_iso(data["offsite_success_at"])
    data["restore_check"] = _parse_iso(data["restore_check_at"])
    # Status files from before the restore check have no flag: neither on nor off.
    data["restore_check_off"] = data["restore_check_enabled"].strip() == "0"
    return data


def _offsite_configured(status):
    return (status.get("offsite_configured") or "").strip() in {"1", "true", "yes"}


def _offsite_is_unhealthy(status, now):
    if (status.get("offsite_error") or "").strip():
        return True
    if not _offsite_configured(status):
        return False
    offsite = status.get("offsite_success")
    if offsite is None:
        return True
    return now - offsite > STALE_AFTER


def _restore_check_enabled(status):
    return (status.get("restore_check_enabled") or "").strip() in {"1", "true", "yes"}


def _restore_check_failed(status):
    return bool((status.get("restore_check_error") or "").strip())


def _restore_check_overdue(status, now):
    if not _restore_check_enabled(status):
        return False
    # Never checked is not overdue: every backup run with the check on either
    # records a pass or sets restore_check_error, and a backup that stops
    # running is already caught by the nightly staleness rule.
    passed = status.get("restore_check")
    return passed is not None and now - passed > RESTORE_CHECK_STALE_AFTER


def backup_is_unhealthy(status, *, now=None):
    if status is None:
        return True
    now = now or timezone.now()
    if (status.get("last_error") or "").strip():
        return True
    last = status.get("last_success")
    if last is None or now - last > STALE_AFTER:
        return True
    if _offsite_is_unhealthy(status, now):
        return True
    return _restore_check_failed(status) or _restore_check_overdue(status, now)


def backup_alert_title(status, *, now=None):
    if status is None:
        return "No backup status found"
    if (status.get("last_error") or "").strip():
        return "The latest backup run failed"
    now = now or timezone.now()
    if _offsite_is_unhealthy(status, now):
        return "The off-site backup copy failed"
    last = status.get("last_success")
    if last is not None and now - last <= STALE_AFTER:
        if _restore_check_failed(status):
            return "The weekly restore check failed"
        if _restore_check_overdue(status, now):
            return "The weekly restore check has not passed in 8 days"
    return "Nightly backups have not succeeded"


def evaluate_backup_alerts(*, now=None, today=None):
    from .alert_services import raise_alert

    now = now or timezone.now()
    today = today or timezone.localdate()
    status = read_backup_status()
    if not backup_is_unhealthy(status, now=now):
        Alert.objects.filter(kind=Alert.Kind.BACKUP).delete()
        return []
    return raise_alert(
        operator_people(),
        Alert.Kind.BACKUP,
        backup_alert_title(status, now=now),
        reverse("settings-data"),
        f"backup:{today.isoformat()}",
    )


def settings_backup_context(person):
    if not person_is_operator(person):
        return {"is_backup_operator": False, "backup_status": None}
    return {"is_backup_operator": True, "backup_status": read_backup_status()}
