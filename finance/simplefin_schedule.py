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
    start = int(base)
    return start <= value <= maximum and (value - start) % step == 0


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
        and _matches_field(day, when.day, 1, 31)
        and _matches_field(month, when.month, 1, 12)
        and _matches_field(weekday, cron_weekday, 0, 6)
    )


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
