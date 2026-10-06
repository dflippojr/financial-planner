from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .cash_flow import (
    GROUPING_MONTH,
    MAX_REPORT_PERIODS,
    _change_from_previous,
    _combine_category_spending,
    _filter_query,
    category_color_index,
    format_minor,
    iter_period_windows,
    period_count,
    period_label,
    previous_equal_range,
    spending_by_category_report,
)
from .category_services import spending_by_category_by_window
from .models import Category


CHART_CATEGORY_LIMIT = 8
OTHER_KEY = "other"
OTHER_NAME = "Other"


def _average_minor(amounts):
    if not amounts:
        return 0
    return int(
        (Decimal(sum(amounts)) / Decimal(len(amounts))).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _amounts_by_key(rows):
    return {row.key: row.spending_minor for row in rows}


def spending_category_trend_report(
    principal,
    *,
    date_from,
    date_to,
    grouping=GROUPING_MONTH,
    account=None,
    scope="",
    today=None,
    tag=None,
):
    if period_count(date_from, date_to, grouping) > MAX_REPORT_PERIODS:
        raise ValueError("Too many periods for one report.")
    today = today or timezone.localdate()
    overall = spending_by_category_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        account=account,
        scope=scope,
        grouping=grouping,
        tag=tag,
    )
    categories = [
        SimpleNamespace(
            key=row.key,
            name=row.name,
            color_index=row.color_index,
            spending_minor=row.spending_minor,
            detail_url=row.detail_url,
        )
        for row in overall.rows
    ]
    periods = []
    windows = list(iter_period_windows(date_from, date_to, grouping))
    named = {item.pk: item for item in Category.objects.visible_to(principal)}
    window_spending = spending_by_category_by_window(
        principal,
        [(window.start, window.end) for window in windows],
        accounts=overall.accounts,
        tag=tag,
    )
    for window, (total_spending_minor, by_category_id) in zip(windows, window_spending):
        combined = _combine_category_spending(by_category_id, named)
        amounts = {item["filter_value"]: item["spending_minor"] for item in combined.values()}
        values = [amounts.get(item.key, 0) for item in categories]
        periods.append(
            SimpleNamespace(
                start=window.start,
                end=window.end,
                label=period_label(window, today=today),
                total_spending_minor=total_spending_minor,
                total_spending_display=format_minor(total_spending_minor),
                values=values,
                displays=[format_minor(value) for value in values],
            )
        )
    ranked_keys = [item.key for item in categories]
    chart_keys = ranked_keys[:CHART_CATEGORY_LIMIT]
    other_indexes = [
        index for index, item in enumerate(categories) if item.key not in set(chart_keys)
    ]
    chart_series = [
        SimpleNamespace(
            key=item.key,
            name=item.name,
            color_index=item.color_index,
            detail_url=item.detail_url,
            values=[period.values[index] for period in periods],
            displays=[period.displays[index] for period in periods],
        )
        for index, item in enumerate(categories)
        if item.key in set(chart_keys)
    ]
    if other_indexes:
        other_values = [sum(period.values[index] for index in other_indexes) for period in periods]
        chart_series.append(
            SimpleNamespace(
                key=OTHER_KEY,
                name=OTHER_NAME,
                color_index=category_color_index(OTHER_KEY),
                detail_url="",
                values=other_values,
                displays=[format_minor(value) for value in other_values],
            )
        )
    return SimpleNamespace(
        accounts=overall.accounts,
        categories=categories,
        periods=periods,
        chart_series=chart_series,
        total_spending_minor=overall.total_spending_minor,
        total_spending_display=overall.total_spending_display,
        has_visible_transactions=overall.has_visible_transactions,
        includes_investment=overall.includes_investment,
        investment_notice=overall.investment_notice,
    )


def spending_trend_chart_data(report):
    return {
        "total_spending_minor": report.total_spending_minor,
        "total_spending_display": report.total_spending_display,
        "categories": [
            {
                "key": item.key,
                "name": item.name,
                "color_index": item.color_index,
                "detail_url": item.detail_url,
            }
            for item in report.categories
        ],
        "periods": [
            {
                "label": period.label,
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "total_spending_minor": period.total_spending_minor,
                "total_spending_display": period.total_spending_display,
                "values": period.values,
                "displays": period.displays,
            }
            for period in report.periods
        ],
        "chart_series": [
            {
                "key": series.key,
                "name": series.name,
                "color_index": series.color_index,
                "detail_url": series.detail_url,
                "values": series.values,
                "displays": series.displays,
            }
            for series in report.chart_series
        ],
    }


def _category_total(report, category_key):
    amounts = _amounts_by_key(report.rows)
    return amounts.get(category_key, 0)


def category_spending_trend_report(
    principal,
    *,
    category_key,
    category_name,
    date_from,
    date_to,
    grouping=GROUPING_MONTH,
    account=None,
    scope="",
    today=None,
    tag=None,
):
    trend = spending_category_trend_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        grouping=grouping,
        account=account,
        scope=scope,
        today=today,
        tag=tag,
    )
    color_key = "uncategorized" if category_key == "uncategorized" else int(category_key)
    color_index = category_color_index(color_key)
    category_index = next(
        (index for index, item in enumerate(trend.categories) if item.key == category_key),
        None,
    )
    periods = []
    values = []
    for period in trend.periods:
        amount = 0 if category_index is None else period.values[category_index]
        values.append(amount)
        periods.append(
            SimpleNamespace(
                start=period.start,
                end=period.end,
                label=period.label,
                spending_minor=amount,
                spending_display=format_minor(amount),
            )
        )
    current_total = sum(values)
    previous_from, previous_to = previous_equal_range(date_from, date_to)
    previous_total = 0
    if previous_from is not None:
        previous_report = spending_by_category_report(
            principal,
            date_from=previous_from,
            date_to=previous_to,
            account=account,
            scope=scope,
            grouping=grouping,
            tag=tag,
        )
        previous_total = _category_total(previous_report, category_key)
    average_minor = _average_minor(values)
    query = _filter_query(
        date_from,
        date_to,
        account=account,
        scope=scope,
        category=category_key,
        tag=tag,
    )
    return SimpleNamespace(
        key=category_key,
        name=category_name,
        color_index=color_index,
        periods=periods,
        total_spending_minor=current_total,
        total_spending_display=format_minor(current_total),
        average_minor=average_minor,
        average_display=format_minor(average_minor),
        previous_from=previous_from,
        previous_to=previous_to,
        previous_spending_minor=previous_total,
        previous_spending_display=format_minor(previous_total),
        spending_change=_change_from_previous(current_total, previous_total),
        drilldown_url=f"{reverse('transaction-list')}?{urlencode(query)}",
        has_visible_transactions=trend.has_visible_transactions,
        includes_investment=trend.includes_investment,
        investment_notice=trend.investment_notice,
        accounts=trend.accounts,
    )


def category_trend_chart_data(report):
    return {
        "name": report.name,
        "color_index": report.color_index,
        "total_spending_minor": report.total_spending_minor,
        "total_spending_display": report.total_spending_display,
        "average_minor": report.average_minor,
        "average_display": report.average_display,
        "periods": [
            {
                "label": period.label,
                "spending_minor": period.spending_minor,
                "spending_display": period.spending_display,
            }
            for period in report.periods
        ],
    }
