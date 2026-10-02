"""Investment performance: growth versus contributions from statement entries.

Implements the Modified Dietz calculation recorded in docs/requirements.md,
"Investment performance (2026-10-02, #18)".
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

from .cash_flow import format_minor
from .models import BalanceSnapshot

ESTIMATE_METHOD_NOTE = "Modified Dietz; contributions assumed mid-period."
INSUFFICIENT_HISTORY = "Fewer than two statement entries recorded."
MISSING_RETURN_NOTE = "One or more periods in this range have no return; showing value change only."
NO_DATA_IN_RANGE = "No statement entries fall in this range yet."
NON_POSITIVE_AVERAGE = "Average invested balance was not positive."


def _statement_entries(account, today):
    return list(
        account.balance_snapshots.filter(
            source=BalanceSnapshot.Source.MANUAL,
            net_contribution_minor__isnull=False,
            snapshot_date__lte=today,
        ).order_by("snapshot_date", "pk")
    )


def _pct_display(fraction):
    if fraction is None:
        return None
    percent = (fraction * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percent}%"


def _build_period(entry0, entry1):
    v0 = entry0.amount_minor
    v1 = entry1.amount_minor
    c = entry1.net_contribution_minor
    growth = v1 - v0 - c
    denom = Decimal(v0) + Decimal(c) / 2
    if denom <= 0:
        return_fraction = None
        return_reason = NON_POSITIVE_AVERAGE
    else:
        return_fraction = Decimal(growth) / denom
        return_reason = None
    return SimpleNamespace(
        start_date=entry0.snapshot_date,
        end_date=entry1.snapshot_date,
        start_value_minor=v0,
        end_value_minor=v1,
        start_value_display=format_minor(v0),
        end_value_display=format_minor(v1),
        contribution_minor=c,
        contribution_display=format_minor(c),
        growth_minor=growth,
        growth_display=format_minor(growth),
        return_fraction=return_fraction,
        return_display=_pct_display(return_fraction),
        return_reason=return_reason,
    )


def _build_periods(entries):
    return [_build_period(entries[i], entries[i + 1]) for i in range(len(entries) - 1)]


def _year_ago(today):
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        # Feb 29 on a non-leap target year.
        return date(today.year - 1, 2, 28)


def _insufficient_summary(label):
    return SimpleNamespace(
        label=label,
        has_data=False,
        partial=False,
        start_date=None,
        end_date=None,
        contributions_minor=None,
        contributions_display=None,
        growth_minor=None,
        growth_display=None,
        return_fraction=None,
        return_display=None,
        note=INSUFFICIENT_HISTORY,
    )


def _empty_range_summary(label):
    return SimpleNamespace(
        label=label,
        has_data=False,
        partial=False,
        start_date=None,
        end_date=None,
        contributions_minor=None,
        contributions_display=None,
        growth_minor=None,
        growth_display=None,
        return_fraction=None,
        return_display=None,
        note=NO_DATA_IN_RANGE,
    )


def _find_start_index(entry_dates, range_start, today):
    """The index of the statement entry a range's linked chain starts from.

    Prefers the latest entry on or before range_start; without one, falls
    back to the earliest entry inside the range and marks it partial.
    """
    start_index = None
    for index, entry_date in enumerate(entry_dates):
        if entry_date <= range_start:
            start_index = index
        else:
            break
    if start_index is not None:
        return start_index, False
    for index, entry_date in enumerate(entry_dates):
        if range_start < entry_date <= today:
            return index, True
    return None, False


def _range_summary(entries, periods, *, label, range_start, today, all_time):
    if len(entries) < 2:
        return _insufficient_summary(label)

    if all_time:
        start_index, partial = 0, False
    else:
        entry_dates = [entry.snapshot_date for entry in entries]
        start_index, partial = _find_start_index(entry_dates, range_start, today)

    if start_index is None or start_index >= len(periods):
        return _empty_range_summary(label)

    included = periods[start_index:]
    contributions_minor = sum(period.contribution_minor for period in included)
    growth_minor = sum(period.growth_minor for period in included)
    missing_return = any(period.return_fraction is None for period in included)

    if missing_return:
        return_fraction = None
        return_display = None
        note = MISSING_RETURN_NOTE
    else:
        linked = Decimal(1)
        for period in included:
            linked *= Decimal(1) + period.return_fraction
        return_fraction = linked - Decimal(1)
        return_display = _pct_display(return_fraction)
        note = "Partial range: no statement entry on or before the start date." if partial else None

    return SimpleNamespace(
        label=label,
        has_data=True,
        partial=partial,
        start_date=included[0].start_date,
        end_date=included[-1].end_date,
        contributions_minor=contributions_minor,
        contributions_display=format_minor(contributions_minor),
        growth_minor=growth_minor,
        growth_display=format_minor(growth_minor),
        return_fraction=return_fraction,
        return_display=return_display,
        note=note,
    )


def account_performance(account, today):
    """Statement-entry periods and YTD/1-year/all-time summaries for one account."""
    entries = _statement_entries(account, today)
    periods = _build_periods(entries)
    year_start = today.replace(month=1, day=1)
    summaries = SimpleNamespace(
        ytd=_range_summary(entries, periods, label="Year to date", range_start=year_start, today=today, all_time=False),
        one_year=_range_summary(entries, periods, label="1 year", range_start=_year_ago(today), today=today, all_time=False),
        all_time=_range_summary(entries, periods, label="All time", range_start=None, today=today, all_time=True),
    )
    return SimpleNamespace(
        account=account,
        statement_count=len(entries),
        periods=periods,
        summaries=summaries,
        estimate_note=ESTIMATE_METHOD_NOTE,
    )
