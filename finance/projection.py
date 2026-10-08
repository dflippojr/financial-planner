"""Month-by-month cash-flow projection from planned items and recurring series."""

from datetime import timedelta
from types import SimpleNamespace

from .cash_flow import GROUPING_MONTH, format_minor, period_label
from .months import add_months, add_months_clamped, month_end

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
_WEEK_DAYS = {CADENCE_WEEKLY: 7, CADENCE_BIWEEKLY: 14}
_CALENDAR_MONTHS = {
    CADENCE_MONTHLY: 1,
    CADENCE_QUARTERLY: 3,
    CADENCE_ANNUAL: 12,
}


def first_projection_month(today):
    return add_months(today, 1)


def step_occurrence(value, cadence):
    days = _WEEK_DAYS.get(cadence)
    if days is not None:
        return value + timedelta(days=days)
    months = _CALENDAR_MONTHS.get(cadence)
    if months is not None:
        return add_months_clamped(value, months)
    raise ValueError(f"Unknown cadence {cadence}.")


def _first_on_or_after(start, cadence, window_start):
    if start >= window_start:
        return start
    days = _WEEK_DAYS.get(cadence)
    if days is not None:
        delta = (window_start - start).days
        steps = (delta + days - 1) // days
        return start + timedelta(days=steps * days)
    months = _CALENDAR_MONTHS.get(cadence)
    if months is None:
        raise ValueError(f"Unknown cadence {cadence}.")
    total_months = (window_start.year - start.year) * 12 + (window_start.month - start.month)
    n = max(total_months // months, 0)
    candidate = add_months_clamped(start, n * months)
    while candidate < window_start:
        n += 1
        candidate = add_months_clamped(start, n * months)
    return candidate


def _advance_from_start(start, current, cadence):
    days = _WEEK_DAYS.get(cadence)
    if days is not None:
        return current + timedelta(days=days)
    months = _CALENDAR_MONTHS.get(cadence)
    if months is None:
        raise ValueError(f"Unknown cadence {cadence}.")
    elapsed = (current.year - start.year) * 12 + (current.month - start.month)
    return add_months_clamped(start, elapsed + months)


def occurrence_dates(start, end, cadence, window_start, window_end):
    dates = []
    if cadence == CADENCE_ONE_TIME:
        if window_start <= start <= window_end and (end is None or start <= end):
            dates.append(start)
        return dates
    if start > window_end or (end is not None and end < window_start):
        return dates
    current = _first_on_or_after(start, cadence, window_start)
    while current <= window_end:
        if end is not None and current > end:
            break
        if current >= window_start:
            dates.append(current)
        current = _advance_from_start(start, current, cadence)
    return dates


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
        start = add_months(first_month, offset)
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
