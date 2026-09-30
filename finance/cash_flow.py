from calendar import month_name
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .category_services import income_and_spending_totals
from .models import Account, ImportBatch, Transaction


MAX_REPORT_DATE = date(9998, 12, 31)
# Each period runs its own queries, so one request must not ask for an
# unbounded number of them. 500 weeks is almost ten years.
MAX_REPORT_PERIODS = 500
GROUPING_MONTH = "month"
GROUPING_WEEK = "week"
GROUPING_QUARTER = "quarter"
GROUPING_YEAR = "year"
GROUPINGS = (GROUPING_MONTH, GROUPING_WEEK, GROUPING_QUARTER, GROUPING_YEAR)
UNKNOWN_GROUPING = "Unknown grouping."
INVESTMENT_NOTICE = (
    "Investment activity is left out of income and spending until those "
    "transaction types are verified."
)


def format_minor(amount_minor, currency="USD"):
    amount = Decimal(amount_minor) / Decimal(100)
    return f"{amount:,.2f} {currency}"


def default_date_range(today=None):
    """Last 12 full months plus the current month through today."""
    today = today or timezone.localdate()
    current_month_start = today.replace(day=1)
    year = current_month_start.year
    month = current_month_start.month - 12
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1), today


def period_start(value, grouping):
    if grouping == GROUPING_WEEK:
        return value - timedelta(days=value.weekday())
    if grouping == GROUPING_MONTH:
        return value.replace(day=1)
    if grouping == GROUPING_QUARTER:
        return date(value.year, ((value.month - 1) // 3) * 3 + 1, 1)
    if grouping == GROUPING_YEAR:
        return date(value.year, 1, 1)
    raise ValueError(UNKNOWN_GROUPING)


def period_count(date_from, date_to, grouping):
    """Number of periods a range spans, computed without iterating them."""
    if grouping == GROUPING_WEEK:
        return (period_start(date_to, grouping) - period_start(date_from, grouping)).days // 7 + 1
    if grouping == GROUPING_MONTH:
        return (date_to.year - date_from.year) * 12 + date_to.month - date_from.month + 1
    if grouping == GROUPING_QUARTER:
        return (date_to.year - date_from.year) * 4 + (date_to.month - 1) // 3 - (date_from.month - 1) // 3 + 1
    if grouping == GROUPING_YEAR:
        return date_to.year - date_from.year + 1
    raise ValueError(UNKNOWN_GROUPING)


def next_period_start(start, grouping):
    if grouping == GROUPING_WEEK:
        return start + timedelta(days=7)
    if grouping == GROUPING_MONTH:
        if start.month == 12:
            return date(start.year + 1, 1, 1)
        return date(start.year, start.month + 1, 1)
    if grouping == GROUPING_QUARTER:
        month = start.month + 3
        year = start.year
        if month > 12:
            month -= 12
            year += 1
        return date(year, month, 1)
    if grouping == GROUPING_YEAR:
        return date(start.year + 1, 1, 1)
    raise ValueError(UNKNOWN_GROUPING)


def iter_period_windows(date_from, date_to, grouping):
    cursor = period_start(date_from, grouping)
    while cursor <= date_to:
        following = next_period_start(cursor, grouping)
        full_end = following - timedelta(days=1)
        yield SimpleNamespace(
            grouping=grouping,
            calendar_start=cursor,
            calendar_end=full_end,
            start=max(cursor, date_from),
            end=min(full_end, date_to),
        )
        cursor = following


def period_label(window, *, today):
    grouping_start = window.calendar_start
    if window.grouping == GROUPING_YEAR:
        base = str(grouping_start.year)
    elif window.grouping == GROUPING_QUARTER:
        quarter = (grouping_start.month - 1) // 3 + 1
        base = f"{grouping_start.year} Q{quarter}"
    elif window.grouping == GROUPING_MONTH:
        base = f"{month_name[grouping_start.month]} {grouping_start.year}"
    else:
        base = f"{window.start.isoformat()} to {window.end.isoformat()}"
    clipped = window.start > window.calendar_start or window.end < window.calendar_end
    if clipped or (window.start <= today <= window.end and today < window.calendar_end):
        return f"{base} (partial)"
    return base


def selected_accounts(principal, *, account=None, scope=""):
    accounts = Account.objects.visible_to(principal).order_by("name", "pk")
    if scope:
        accounts = accounts.filter(scope=scope)
    if account is not None:
        accounts = accounts.filter(pk=account.pk)
    return list(accounts)


def _batches_by_account(principal, accounts):
    batches = (
        ImportBatch.objects.visible_to(principal)
        .filter(status=ImportBatch.Status.ACTIVE, account__in=accounts)
        .only("account_id", "date_range_start", "date_range_end")
    )
    grouped = {account.pk: [] for account in accounts}
    for batch in batches:
        grouped[batch.account_id].append(batch)
    return grouped


def _period_missing_import(window, accounts, batches_by_account):
    for account in accounts:
        covered = False
        for batch in batches_by_account.get(account.pk, ()):
            if batch.date_range_start <= window.end and batch.date_range_end >= window.start:
                covered = True
                break
        if not covered:
            return True
    return False


def _drilldown_url(window, *, account=None, scope=""):
    query = {"date_from": window.start.isoformat(), "date_to": window.end.isoformat()}
    if account is not None:
        query["account"] = str(account.pk)
    if scope:
        query["scope"] = scope
    return f"{reverse('transaction-list')}?{urlencode(query)}"


def cash_flow_report(
    principal,
    *,
    date_from,
    date_to,
    grouping=GROUPING_MONTH,
    account=None,
    scope="",
    today=None,
):
    if period_count(date_from, date_to, grouping) > MAX_REPORT_PERIODS:
        raise ValueError("Too many periods for one report.")
    today = today or timezone.localdate()
    accounts = selected_accounts(principal, account=account, scope=scope)
    batches_by_account = _batches_by_account(principal, accounts)
    account_filter = accounts
    periods = []
    for window in iter_period_windows(date_from, date_to, grouping):
        totals = income_and_spending_totals(
            principal,
            date_from=window.start,
            date_to=window.end,
            accounts=account_filter,
        )
        missing = bool(accounts) and _period_missing_import(window, accounts, batches_by_account)
        periods.append(
            SimpleNamespace(
                start=window.start,
                end=window.end,
                label=period_label(window, today=today),
                income_minor=totals.income_minor,
                spending_minor=totals.spending_minor,
                net_minor=totals.net_minor,
                income_display=format_minor(totals.income_minor),
                spending_display=format_minor(totals.spending_minor),
                net_display=format_minor(totals.net_minor),
                missing_import=missing,
                drilldown_url=_drilldown_url(window, account=account, scope=scope),
            )
        )
    visible_transactions = (
        Transaction.objects.visible_to(principal)
        .filter(status=Transaction.Status.ACTIVE, account__in=accounts)
        .exists()
        if accounts
        else False
    )
    return SimpleNamespace(
        accounts=accounts,
        periods=periods,
        has_visible_transactions=visible_transactions,
        includes_investment=any(item.account_type == Account.Type.INVESTMENT for item in accounts),
        investment_notice=INVESTMENT_NOTICE,
        chart=chart_points(periods),
    )


def chart_points(periods, *, width=720, height=220, pad=36):
    if not periods:
        return SimpleNamespace(width=width, height=height, lines=(), zero_y=height // 2, labels=())
    peak = max(max(row.income_minor, row.spending_minor, abs(row.net_minor)) for row in periods)
    peak = peak or 1
    inner_width = width - 2 * pad
    inner_height = height - 2 * pad
    zero_y = pad + inner_height // 2
    count = len(periods)
    step = inner_width / max(count - 1, 1)

    def series(attr):
        points = []
        for index, row in enumerate(periods):
            x = pad if count == 1 else pad + index * step
            y = zero_y - (getattr(row, attr) / peak) * (inner_height / 2)
            points.append(f"{x:.1f},{y:.1f}")
        return " ".join(points)

    labels = []
    for index, row in enumerate(periods):
        x = pad if count == 1 else pad + index * step
        labels.append(SimpleNamespace(x=f"{x:.1f}", y=str(height - 8), text=row.label))
    return SimpleNamespace(
        width=width,
        height=height,
        zero_y=zero_y,
        lines=(
            SimpleNamespace(name="Income", points=series("income_minor"), color="#0b6e4f"),
            SimpleNamespace(name="Spending", points=series("spending_minor"), color="#9b2226"),
            SimpleNamespace(name="Net", points=series("net_minor"), color="#1d3557"),
        ),
        labels=labels,
    )
