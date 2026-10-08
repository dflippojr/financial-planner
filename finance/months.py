"""Calendar month boundaries and shifts, with explicit day handling."""

from calendar import monthrange
from datetime import date


def month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def month_end(value: date) -> date:
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def add_months(value: date, months: int) -> date:
    """Shift by calendar months and return the first of the target month."""
    year, month0 = divmod(value.year * 12 + value.month - 1 + months, 12)
    return date(year, month0 + 1, 1)


def add_months_clamped(value: date, months: int) -> date:
    """Shift by calendar months, keeping the day up to the target month end."""
    target = add_months(value, months)
    return target.replace(day=min(value.day, month_end(target).day))
