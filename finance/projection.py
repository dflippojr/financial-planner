"""Month-by-month cash-flow projection from planned items and recurring series."""

from calendar import monthrange
from datetime import date, timedelta
from types import SimpleNamespace

from .cash_flow import GROUPING_MONTH, format_minor, period_label

HORIZONS = (3, 6, 12, 24)
DEFAULT_HORIZON = 12
CADENCE_ONE_TIME = "one_time"
CADENCE_WEEKLY = "weekly"
CADENCE_BIWEEKLY = "biweekly"
CADENCE_MONTHLY = "monthly"
CADENCE_QUARTERLY = "quarterly"
CADENCE_ANNUAL = "annual"
KIND_INCOME = "income"
KIND_EXPENSE = "expense"
SOURCE_PLANNED = "planned"
SOURCE_SERIES = "series"
MAX_OCCURRENCE_STEPS = 4000


def add_calendar_months(value, months):
    year = value.year
    month = value.month + months
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def first_projection_month(today):
    return add_calendar_months(today.replace(day=1), 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def step_occurrence(value, cadence):
    if cadence == CADENCE_WEEKLY:
        return value + timedelta(days=7)
    if cadence == CADENCE_BIWEEKLY:
        return value + timedelta(days=14)
    if cadence == CADENCE_MONTHLY:
        return add_calendar_months(value, 1)
    if cadence == CADENCE_QUARTERLY:
        return add_calendar_months(value, 3)
    if cadence == CADENCE_ANNUAL:
        return add_calendar_months(value, 12)
    raise ValueError(f"Unknown cadence {cadence}.")


def occurrence_dates(start, end, cadence, window_start, window_end):
    if cadence == CADENCE_ONE_TIME:
        if window_start <= start <= window_end and (end is None or start <= end):
            return (start,)
        return ()
    dates = []
    current = start
    for _ in range(MAX_OCCURRENCE_STEPS):
        if current > window_end:
            break
        if end is not None and current > end:
            break
        if current >= window_start:
            dates.append(current)
        current = step_occurrence(current, cadence)
    return tuple(dates)


def _contribution(item, count):
    total = item.amount_minor * count
    return SimpleNamespace(
        source=item.source,
        source_id=item.source_id,
        name=item.name,
        kind=item.kind,
        cadence=item.cadence,
        occurrence_count=count,
        amount_minor=total,
        amount_display=format_minor(total, item.currency),
    )


def _empty_month(window, today):
    return SimpleNamespace(
        start=window.start,
        end=window.end,
        label=period_label(window, today=today),
        projected=True,
        missing_import=False,
        drilldown_url="",
        income_minor=0,
        spending_minor=0,
        net_minor=0,
        income_display=format_minor(0),
        spending_display=format_minor(0),
        net_display=format_minor(0),
        contributions=(),
    )


def project_cash_flow(items, *, today, horizon=DEFAULT_HORIZON):
    """Sum planned and series amounts into N calendar months after today.

    `items` is an iterable of SimpleNamespace rows with name, kind, amount_minor
    (positive), currency, start, end, cadence, source, and source_id.
    """
    if horizon not in HORIZONS:
        raise ValueError("Horizon must be 3, 6, 12, or 24 months.")
    first_month = first_projection_month(today)
    rows = []
    for offset in range(horizon):
        start = add_calendar_months(first_month, offset)
        end = month_end(start)
        window = SimpleNamespace(
            grouping=GROUPING_MONTH,
            calendar_start=start,
            calendar_end=end,
            start=start,
            end=end,
        )
        income = 0
        spending = 0
        contributions = []
        for item in items:
            hits = occurrence_dates(item.start, item.end, item.cadence, start, end)
            if not hits:
                continue
            contribution = _contribution(item, len(hits))
            contributions.append(contribution)
            if item.kind == KIND_INCOME:
                income += contribution.amount_minor
            else:
                spending += contribution.amount_minor
        net = income - spending
        month = _empty_month(window, today)
        month.income_minor = income
        month.spending_minor = spending
        month.net_minor = net
        month.income_display = format_minor(income)
        month.spending_display = format_minor(spending)
        month.net_display = format_minor(net)
        month.contributions = tuple(contributions)
        rows.append(month)
    return tuple(rows)
