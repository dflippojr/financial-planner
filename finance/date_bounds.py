"""The window of plausible dates for recorded activity: transactions and balances.

Dates outside it come from typos or malformed provider data. Rejecting them at
the boundary keeps date arithmetic (cadences, month shifts) far from date.max.
"""

from datetime import date, timedelta

from django.utils import timezone

EARLIEST_ACTIVITY_DATE = date(1900, 1, 1)
# Statements can post a day or two ahead of the local date; a year is generous.
FUTURE_ALLOWANCE_DAYS = 366
TOO_EARLY_ERROR = "Date must be on or after January 1, 1900."
TOO_LATE_ERROR = "Date cannot be more than a year in the future."


def latest_activity_date(today=None) -> date:
    return (today or timezone.localdate()) + timedelta(days=FUTURE_ALLOWANCE_DAYS)


def activity_date_error(value: date, today=None) -> str | None:
    """A user-facing reason the date is out of range, or None when it is plausible."""
    if value < EARLIEST_ACTIVITY_DATE:
        return TOO_EARLY_ERROR
    if value > latest_activity_date(today):
        return TOO_LATE_ERROR
    return None
