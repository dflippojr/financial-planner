from calendar import month_name
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .category_services import income_and_spending_totals
from .models import Account, Category, ImportBatch, Transaction


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


CATEGORY_PALETTE_SIZE = 8


def format_minor(amount_minor, currency="USD"):
    amount = Decimal(amount_minor) / Decimal(100)
    return f"{amount:,.2f} {currency}"


def previous_equal_range(date_from, date_to):
    """The range of the same length that ends the day before date_from.

    Returns (None, None) when that range would start before the earliest date
    Python can represent, so a report starting near year 1 has nothing to
    compare against instead of failing.
    """
    span_days = (date_to - date_from).days + 1
    if date_from.toordinal() - span_days < date.min.toordinal():
        return None, None
    previous_to = date_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=span_days - 1)
    return previous_from, previous_to


def category_color_index(color_key):
    """Stable 0-7 palette index derived from a category id. No extra schema."""
    raw = 2166136261
    for char in str(color_key):
        raw ^= ord(char)
        raw = (raw * 16777619) & 0xFFFFFFFF
    return raw % CATEGORY_PALETTE_SIZE


def _change_from_previous(current_minor, previous_minor):
    delta = current_minor - previous_minor
    if delta > 0:
        direction = "up"
        label = f"up {format_minor(delta)} vs previous range"
    elif delta < 0:
        direction = "down"
        label = f"down {format_minor(-delta)} vs previous range"
    else:
        direction = "flat"
        label = "no change vs previous range"
    return SimpleNamespace(
        minor=delta,
        display=format_minor(delta),
        direction=direction,
        label=label,
    )


def _no_previous_change():
    return SimpleNamespace(minor=None, display="—", direction="none", label="No earlier range to compare")


def _range_summary(current, previous, previous_from, previous_to):
    if previous is None:
        income_change = spending_change = net_change = _no_previous_change()
        previous = SimpleNamespace(income_minor=None, spending_minor=None, net_minor=None)
    else:
        income_change = _change_from_previous(current.income_minor, previous.income_minor)
        spending_change = _change_from_previous(current.spending_minor, previous.spending_minor)
        net_change = _change_from_previous(current.net_minor, previous.net_minor)
    return SimpleNamespace(
        income_minor=current.income_minor,
        spending_minor=current.spending_minor,
        net_minor=current.net_minor,
        income_display=format_minor(current.income_minor),
        spending_display=format_minor(current.spending_minor),
        net_display=format_minor(current.net_minor),
        previous_from=previous_from,
        previous_to=previous_to,
        previous_income_minor=previous.income_minor,
        previous_spending_minor=previous.spending_minor,
        previous_net_minor=previous.net_minor,
        income_change=income_change,
        spending_change=spending_change,
        net_change=net_change,
    )


def _shift_month_start(value, months):
    year = value.year
    month = value.month + months
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, 1)


def default_date_range(today=None):
    """Last 12 full months plus the current month through today."""
    today = today or timezone.localdate()
    current_month_start = today.replace(day=1)
    return _shift_month_start(current_month_start, -12), today


def date_range_presets(today=None):
    """Named ranges that match the cash-flow filter presets."""
    today = today or timezone.localdate()
    month_start = today.replace(day=1)
    last_month_start = _shift_month_start(month_start, -1)
    return (
        SimpleNamespace(
            key="this-month",
            label="This month",
            date_from=month_start,
            date_to=today,
        ),
        SimpleNamespace(
            key="last-month",
            label="Last month",
            date_from=last_month_start,
            date_to=month_start - timedelta(days=1),
        ),
        SimpleNamespace(
            key="last-3-months",
            label="Last 3 months",
            date_from=_shift_month_start(month_start, -2),
            date_to=today,
        ),
        SimpleNamespace(
            key="last-12-months",
            label="Last 12 months",
            date_from=_shift_month_start(month_start, -11),
            date_to=today,
        ),
        SimpleNamespace(
            key="year-to-date",
            label="Year to date",
            date_from=date(today.year, 1, 1),
            date_to=today,
        ),
    )


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


def selected_accounts(principal, *, account=None, scope="", cash_flow_only=False, accounts=None):
    if accounts is None:
        query = Account.objects.visible_to(principal)
    elif hasattr(accounts, "filter"):
        query = accounts
    else:
        query = Account.objects.filter(pk__in=[item.pk for item in accounts])
    query = query.order_by("name", "pk")
    if cash_flow_only:
        query = query.for_cash_flow()
    if scope:
        query = query.filter(scope=scope)
    if account is not None:
        query = query.filter(pk=account.pk)
    return list(query)


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


def _filter_query(date_from, date_to, *, account=None, scope="", category=None, tag=None):
    query = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    if account is not None:
        query["account"] = str(account.pk)
    if scope:
        query["scope"] = scope
    if category is not None:
        query["category"] = category
    if tag is not None:
        query["tag"] = str(tag.pk)
    return query


def _drilldown_url(window, *, account=None, scope="", tag=None):
    return f"{reverse('transaction-list')}?{urlencode(_filter_query(window.start, window.end, account=account, scope=scope, tag=tag))}"


def format_percent(amount_minor, total_minor):
    if total_minor == 0:
        return "—"
    percent = (Decimal(amount_minor) * Decimal(100) / Decimal(total_minor)).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )
    return f"{percent}%"


def _uncategorized_bucket():
    return {
        "name": "Uncategorized",
        "filter_value": "uncategorized",
        "color_key": "uncategorized",
        "spending_minor": 0,
    }


def _combine_category_spending(by_category_id, named):
    combined = {}
    for category_id, amount in by_category_id.items():
        category = named.get(category_id)
        if category is None or category.code == Category.Code.UNCATEGORIZED:
            bucket = combined.setdefault("uncategorized", _uncategorized_bucket())
        else:
            bucket = combined.setdefault(
                category.pk,
                {
                    "name": category.name,
                    "filter_value": str(category.pk),
                    "color_key": category.pk,
                    "spending_minor": 0,
                },
            )
        bucket["spending_minor"] += amount
    combined.setdefault("uncategorized", _uncategorized_bucket())
    return combined


def spending_category_detail_url(
    filter_value,
    date_from,
    date_to,
    *,
    account=None,
    scope="",
    grouping=GROUPING_MONTH,
    tag=None,
):
    if filter_value == "uncategorized":
        path = reverse("spending-category-uncategorized")
    else:
        path = reverse("spending-category-detail", args=[int(filter_value)])
    query = _filter_query(date_from, date_to, account=account, scope=scope, tag=tag)
    query["grouping"] = grouping
    return f"{path}?{urlencode(query)}"


def _spending_row(item, *, total_spending, date_from, date_to, account, scope, grouping=GROUPING_MONTH, tag=None):
    spending_minor = item["spending_minor"]
    query = _filter_query(
        date_from,
        date_to,
        account=account,
        scope=scope,
        category=item["filter_value"],
        tag=tag,
    )
    return SimpleNamespace(
        key=item["filter_value"],
        name=item["name"],
        spending_minor=spending_minor,
        spending_display=format_minor(spending_minor),
        percent_display=format_percent(spending_minor, total_spending),
        is_net_refund=spending_minor < 0,
        color_index=category_color_index(item["color_key"]),
        drilldown_url=f"{reverse('transaction-list')}?{urlencode(query)}",
        detail_url=spending_category_detail_url(
            item["filter_value"],
            date_from,
            date_to,
            account=account,
            scope=scope,
            grouping=grouping,
            tag=tag,
        ),
    )


def spending_by_category_report(
    principal,
    *,
    date_from,
    date_to,
    account=None,
    scope="",
    grouping=GROUPING_MONTH,
    tag=None,
    accounts=None,
):
    accounts = selected_accounts(
        principal, account=account, scope=scope, cash_flow_only=True, accounts=accounts
    )
    totals = income_and_spending_totals(
        principal,
        date_from=date_from,
        date_to=date_to,
        accounts=accounts,
        tag=tag,
    )
    named = {item.pk: item for item in Category.objects.visible_to(principal)}
    combined = _combine_category_spending(totals.spending_by_category_id, named)
    rows = [
        _spending_row(
            item,
            total_spending=totals.spending_minor,
            date_from=date_from,
            date_to=date_to,
            account=account,
            scope=scope,
            grouping=grouping,
            tag=tag,
        )
        for item in combined.values()
    ]
    rows.sort(key=lambda row: (-row.spending_minor, row.name))
    visible_transactions = (
        Transaction.objects.visible_to(principal)
        .filter(status=Transaction.Status.ACTIVE, account__in=accounts)
        .exists()
        if accounts
        else False
    )
    return SimpleNamespace(
        accounts=accounts,
        rows=rows,
        total_spending_minor=totals.spending_minor,
        total_spending_display=format_minor(totals.spending_minor),
        has_visible_transactions=visible_transactions,
        includes_investment=any(item.account_type == Account.Type.INVESTMENT for item in accounts),
        investment_notice=INVESTMENT_NOTICE,
    )


def cash_flow_report(
    principal,
    *,
    date_from,
    date_to,
    grouping=GROUPING_MONTH,
    account=None,
    scope="",
    today=None,
    tag=None,
    accounts=None,
):
    if period_count(date_from, date_to, grouping) > MAX_REPORT_PERIODS:
        raise ValueError("Too many periods for one report.")
    today = today or timezone.localdate()
    accounts = selected_accounts(
        principal, account=account, scope=scope, cash_flow_only=True, accounts=accounts
    )
    batches_by_account = _batches_by_account(principal, accounts)
    account_filter = accounts
    periods = []
    for window in iter_period_windows(date_from, date_to, grouping):
        totals = income_and_spending_totals(
            principal,
            date_from=window.start,
            date_to=window.end,
            accounts=account_filter,
            tag=tag,
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
                drilldown_url=_drilldown_url(window, account=account, scope=scope, tag=tag),
            )
        )
    visible_transactions = (
        Transaction.objects.visible_to(principal)
        .filter(status=Transaction.Status.ACTIVE, account__in=accounts)
        .exists()
        if accounts
        else False
    )
    current = income_and_spending_totals(
        principal,
        date_from=date_from,
        date_to=date_to,
        accounts=account_filter,
        tag=tag,
    )
    previous_from, previous_to = previous_equal_range(date_from, date_to)
    previous = None
    if previous_from is not None:
        previous = income_and_spending_totals(
            principal,
            date_from=previous_from,
            date_to=previous_to,
            accounts=account_filter,
            tag=tag,
        )
    return SimpleNamespace(
        accounts=accounts,
        periods=periods,
        summary=_range_summary(current, previous, previous_from, previous_to),
        has_visible_transactions=visible_transactions,
        includes_investment=any(item.account_type == Account.Type.INVESTMENT for item in accounts),
        investment_notice=INVESTMENT_NOTICE,
    )


def _period_chart_row(period):
    return {
        "label": period.label,
        "income_minor": period.income_minor,
        "spending_minor": period.spending_minor,
        "net_minor": period.net_minor,
        "income_display": period.income_display,
        "spending_display": period.spending_display,
        "net_display": period.net_display,
        "missing_import": period.missing_import,
        "drilldown_url": period.drilldown_url,
    }


def _change_chart_row(change):
    return {
        "minor": change.minor,
        "display": change.display,
        "direction": change.direction,
        "label": change.label,
    }


def _projected_chart_row(period):
    row = _period_chart_row(period)
    row["projected"] = True
    row["contributions"] = [
        {
            "source": item.source,
            "source_id": item.source_id,
            "name": item.name,
            "kind": item.kind,
            "cadence": item.cadence,
            "occurrence_count": item.occurrence_count,
            "amount_minor": item.amount_minor,
            "amount_display": item.amount_display,
        }
        for item in period.contributions
    ]
    return row


def cash_flow_chart_data(report):
    summary = report.summary
    periods = []
    for period in report.periods:
        row = _period_chart_row(period)
        row["projected"] = False
        periods.append(row)
    for period in getattr(report, "projected_periods", ()) or ():
        periods.append(_projected_chart_row(period))
    return {
        "periods": periods,
        "summary": {
            "income_minor": summary.income_minor,
            "spending_minor": summary.spending_minor,
            "net_minor": summary.net_minor,
            "income_display": summary.income_display,
            "spending_display": summary.spending_display,
            "net_display": summary.net_display,
            "previous_from": summary.previous_from.isoformat() if summary.previous_from else None,
            "previous_to": summary.previous_to.isoformat() if summary.previous_to else None,
            "previous_income_minor": summary.previous_income_minor,
            "previous_spending_minor": summary.previous_spending_minor,
            "previous_net_minor": summary.previous_net_minor,
            "income_change": _change_chart_row(summary.income_change),
            "spending_change": _change_chart_row(summary.spending_change),
            "net_change": _change_chart_row(summary.net_change),
        },
    }


def _donut_rows(rows):
    """Rows a donut can draw: only categories with positive spending.

    A net-refund category has a negative total, which a pie cannot show, so it
    stays in the tiles and table and is left out of the chart. Shares are of
    the charted (positive) total, not of net spending.
    """
    positive = [row for row in rows if row.spending_minor > 0]
    charted_total = sum(row.spending_minor for row in positive)
    return [
        {
            "name": row.name,
            "color_index": row.color_index,
            "spending_minor": row.spending_minor,
            "spending_display": row.spending_display,
            "share_display": format_percent(row.spending_minor, charted_total),
            "drilldown_url": row.drilldown_url,
        }
        for row in positive
    ]


def spending_chart_data(report):
    return {
        "total_spending_minor": report.total_spending_minor,
        "total_spending_display": report.total_spending_display,
        "has_net_refund": any(row.is_net_refund for row in report.rows),
        "chart_rows": _donut_rows(report.rows),
        "rows": [
            {
                "name": row.name,
                "color_index": row.color_index,
                "spending_minor": row.spending_minor,
                "spending_display": row.spending_display,
                "percent_display": row.percent_display,
                "is_net_refund": row.is_net_refund,
                "drilldown_url": row.drilldown_url,
            }
            for row in report.rows
        ],
    }
