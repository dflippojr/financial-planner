from datetime import datetime, timedelta

from django.utils import timezone


def parse_five_field_cron(expression: str) -> tuple[str, str, str, str, str]:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("SIMPLEFIN_SYNC_CRON must be one five-field cron schedule")
    return tuple(fields)


def _match_step(part: str, value: int, minimum: int, maximum: int) -> bool:
    base, step_text = part.split("/", 1)
    step = int(step_text)
    if step < 1:
        return False
    if base == "*":
        return (value - minimum) % step == 0
    if "-" in base:
        start_text, end_text = base.split("-", 1)
        start, end = int(start_text), int(end_text)
    else:
        start, end = int(base), maximum
    return start <= value <= end and (value - start) % step == 0


def _match_one(part: str, value: int, minimum: int, maximum: int) -> bool:
    if "/" in part:
        return _match_step(part, value, minimum, maximum)
    if "-" in part:
        start_text, end_text = part.split("-", 1)
        return int(start_text) <= value <= int(end_text)
    return int(part) == value


def _matches_field(field: str, value: int, minimum: int, maximum: int) -> bool:
    if field == "*":
        return True
    return any(_match_one(part, value, minimum, maximum) for part in field.split(","))


def cron_matches(expression: str, when: datetime) -> bool:
    minute, hour, day, month, weekday = parse_five_field_cron(expression)
    cron_weekday = (when.weekday() + 1) % 7
    return (
        _matches_field(minute, when.minute, 0, 59)
        and _matches_field(hour, when.hour, 0, 23)
        and _day_matches(day, weekday, when.day, cron_weekday)
        and _matches_field(month, when.month, 1, 12)
    )


def _day_matches(day: str, weekday: str, day_of_month: int, cron_weekday: int) -> bool:
    # Standard cron: when both day-of-month and day-of-week are restricted, a
    # time matches if EITHER does; otherwise both must match.
    day_ok = _matches_field(day, day_of_month, 1, 31)
    weekday_ok = _weekday_matches(weekday, cron_weekday)
    if day != "*" and weekday != "*":
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def _weekday_matches(field: str, cron_weekday: int) -> bool:
    # Cron accepts both 0 and 7 for Sunday.
    if _matches_field(field, cron_weekday, 0, 7):
        return True
    return cron_weekday == 0 and _matches_field(field, 7, 0, 7)


def next_cron_datetime(expression: str, after: datetime) -> datetime:
    parse_five_field_cron(expression)
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + timedelta(days=400)
    while cursor <= limit:
        if cron_matches(expression, cursor):
            return cursor
        cursor += timedelta(minutes=1)
    raise ValueError("SIMPLEFIN_SYNC_CRON does not match a time in the next year")


def next_scheduled_sync(expression: str, now=None):
    when = now or timezone.localtime()
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return next_cron_datetime(expression, timezone.localtime(when))


def seconds_until(due, now) -> float:
    return max(0.0, (due - now).total_seconds())


def schedule_after_sync(expression: str, last_due, now):
    """Next run after a sync that was due at last_due, given the current time.

    Uses the scheduled due time, not 'now plus a minute of sleep', so a sync
    that ends after a minute boundary still runs that next minute. If that
    time is already due, wait is 0.
    """
    due = next_scheduled_sync(expression, last_due)
    return due, seconds_until(due, now)
