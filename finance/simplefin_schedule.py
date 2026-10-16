import re
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from typing import NamedTuple

from django.utils import timezone

# (name, minimum, maximum) for each of the five fields. Weekday allows 7 for Sunday.
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)
_PART = re.compile(r"(?:\*|(\d+)(?:-(\d+))?)(?:/(\d+))?")
_SEARCH_DAYS = 400


class CronSchedule(NamedTuple):
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset  # 0 is Sunday; a 7 in the expression is stored as 0.
    day_restricted: bool
    weekday_restricted: bool


def _parse_part(part, name, minimum, maximum):
    match = _PART.fullmatch(part)
    if match is None:
        if re.search(r"[A-Za-z]", part):
            raise ValueError("SIMPLEFIN_SYNC_CRON supports numbers only (for example 1-5 for Monday to Friday)")
        raise ValueError(f"SIMPLEFIN_SYNC_CRON has an invalid {name} field")
    start_text, end_text, step_text = match.groups()
    step = int(step_text) if step_text is not None else 1
    if step < 1:
        raise ValueError(f"SIMPLEFIN_SYNC_CRON has a {name} step below 1")
    if start_text is None:
        start, end = minimum, maximum
    else:
        start = int(start_text)
        # "5/10" runs from 5 to the field's maximum, as in cron.
        end = int(end_text) if end_text is not None else (maximum if step_text is not None else start)
    if not (minimum <= start <= maximum and minimum <= end <= maximum):
        raise ValueError(f"SIMPLEFIN_SYNC_CRON has a {name} outside {minimum}-{maximum}")
    if start > end:
        raise ValueError(f"SIMPLEFIN_SYNC_CRON has a {name} range that runs backwards")
    return range(start, end + 1, step)


def _parse_field(text, name, minimum, maximum):
    values = set()
    for part in text.split(","):
        values.update(_parse_part(part, name, minimum, maximum))
    return frozenset(values)


def parse_five_field_cron(expression: str) -> CronSchedule:
    """Parse and fully validate a numeric five-field cron schedule.

    Raises ValueError for anything the matcher cannot evaluate, so a bad
    setting fails at startup instead of in the daily pass.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("SIMPLEFIN_SYNC_CRON must be one five-field cron schedule")
    minutes, hours, days, months, weekdays = (
        _parse_field(text, *spec) for text, spec in zip(fields, _FIELDS)
    )
    return CronSchedule(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        # Cron accepts both 0 and 7 for Sunday.
        weekdays=frozenset(value % 7 for value in weekdays),
        day_restricted=fields[2] != "*",
        weekday_restricted=fields[4] != "*",
    )


def _date_matches(schedule: CronSchedule, when) -> bool:
    if when.month not in schedule.months:
        return False
    # Standard cron: when both day-of-month and day-of-week are restricted, a
    # date matches if EITHER does; otherwise both must match.
    day_ok = when.day in schedule.days
    weekday_ok = (when.weekday() + 1) % 7 in schedule.weekdays
    if schedule.day_restricted and schedule.weekday_restricted:
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def cron_matches(expression: str, when: datetime) -> bool:
    schedule = parse_five_field_cron(expression)
    return when.minute in schedule.minutes and when.hour in schedule.hours and _date_matches(schedule, when)


def next_cron_datetime(expression: str, after: datetime) -> datetime:
    schedule = parse_five_field_cron(expression)
    # Wall-clock times in after's zone, from the next whole minute.
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    times = [time(hour, minute) for hour in sorted(schedule.hours) for minute in sorted(schedule.minutes)]
    for offset in range(_SEARCH_DAYS + 1):
        day = cursor.date() + timedelta(days=offset)
        if not _date_matches(schedule, day):
            continue
        for at in times:
            candidate = datetime.combine(day, at, tzinfo=cursor.tzinfo)
            if candidate >= cursor:
                return candidate
    raise ValueError("SIMPLEFIN_SYNC_CRON does not match a time in the next year")


def next_scheduled_sync(expression: str, now=None):
    when = now or timezone.localtime()
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return next_cron_datetime(expression, timezone.localtime(when))


def seconds_until(due, now) -> float:
    # Compare in UTC: subtracting two times in the same zone measures wall-clock
    # time, which is an hour off across a daylight-saving change.
    return max(0.0, (due.astimezone(dt_timezone.utc) - now.astimezone(dt_timezone.utc)).total_seconds())


def schedule_after_sync(expression: str, last_due, now):
    """Next run after a sync that was due at last_due, given the current time.

    Uses the scheduled due time, not 'now plus a minute of sleep', so a sync
    that ends after a minute boundary still runs that next minute. If that
    time is already due, wait is 0.
    """
    due = next_scheduled_sync(expression, last_due)
    return due, seconds_until(due, now)
