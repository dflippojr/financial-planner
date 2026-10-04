"""Calendar-year totals assembled from the same report functions as other pages."""

from __future__ import annotations

import csv
import io
from datetime import date
from types import SimpleNamespace

from django.utils import timezone

from .cash_flow import (
    GROUPING_MONTH,
    cash_flow_report,
    format_minor,
    income_by_category_report,
    selected_accounts,
    spending_by_category_report,
)
from .category_services import income_and_spending_totals
from .export import money_decimal
from .models import RecurringSeries, Tag
from .net_worth import net_worth_report
from .recurring_services import confirmed_totals

CSV_SECTIONS = (
    "cash-flow",
    "spending",
    "income",
    "accounts",
    "tags",
    "recurring",
    "net-worth",
)

SCOPE_LABELS = {
    "": "All visible accounts",
    "private": "Private",
    "household": "Household",
}


def last_full_year(today=None):
    today = today or timezone.localdate()
    return max(today.year - 1, 1)


def year_bounds(year):
    return date(year, 1, 1), date(year, 12, 31)


def parse_report_year(raw, *, today=None):
    default = last_full_year(today)
    if raw in (None, ""):
        return default
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return default
    if year < 1 or year > 9998:
        return default
    return year


def _as_of_net_worth(principal, as_of, scope):
    if as_of < date(1, 1, 1):
        return None
    start = as_of.replace(day=1)
    report = net_worth_report(
        principal,
        date_from=start,
        date_to=as_of,
        scope=scope,
        today=as_of,
    )
    if not report.periods:
        return None
    return report.periods[-1]


def _account_rows(principal, date_from, date_to, scope):
    accounts = selected_accounts(principal, scope=scope, cash_flow_only=True)
    rows = []
    for account in accounts:
        totals = income_and_spending_totals(
            principal,
            date_from=date_from,
            date_to=date_to,
            accounts=[account],
        )
        rows.append(
            SimpleNamespace(
                account_id=account.pk,
                name=account.name,
                currency=account.currency,
                income_minor=totals.income_minor,
                spending_minor=totals.spending_minor,
                net_minor=totals.net_minor,
                income_display=format_minor(totals.income_minor, account.currency),
                spending_display=format_minor(totals.spending_minor, account.currency),
                net_display=format_minor(totals.net_minor, account.currency),
            )
        )
    rows.sort(key=lambda row: (row.name, row.account_id))
    return rows


def _tag_rows(principal, date_from, date_to, accounts):
    rows = []
    tags = Tag.objects.visible_to(principal).active().order_by("name", "pk")
    for tag in tags:
        totals = income_and_spending_totals(
            principal,
            date_from=date_from,
            date_to=date_to,
            accounts=accounts,
            tag=tag,
        )
        if totals.income_minor == 0 and totals.spending_minor == 0:
            continue
        rows.append(
            SimpleNamespace(
                tag_id=tag.pk,
                name=tag.name,
                income_minor=totals.income_minor,
                spending_minor=totals.spending_minor,
                net_minor=totals.net_minor,
                income_display=format_minor(totals.income_minor),
                spending_display=format_minor(totals.spending_minor),
                net_display=format_minor(totals.net_minor),
            )
        )
    return rows


def _recurring_rows(principal):
    visible = RecurringSeries.objects.visible_to(principal).filter(
        status=RecurringSeries.Status.CONFIRMED,
        is_active=True,
        cancelled_at__isnull=True,
    )
    series_list = list(visible)
    monthly_minor, annual_minor = confirmed_totals(series_list)
    rows = [
        SimpleNamespace(
            series_id=series.pk,
            name=series.display_name,
            cadence=series.cadence,
            currency=series.currency,
            annual_minor=series.annual_minor,
            annual_display=series.annual_display,
        )
        for series in series_list
    ]
    rows.sort(key=lambda row: (-row.annual_minor, row.name, row.series_id))
    currency = rows[0].currency if rows else "USD"
    return SimpleNamespace(
        rows=rows,
        annual_minor=annual_minor,
        monthly_minor=monthly_minor,
        annual_display=format_minor(annual_minor, currency),
    )


def _net_worth_points(principal, year, scope):
    date_from, date_to = year_bounds(year)
    start_as_of = date_from - date.resolution
    start = _as_of_net_worth(principal, start_as_of, scope) if year > 1 else None
    end = _as_of_net_worth(principal, date_to, scope)
    return SimpleNamespace(
        start=start,
        end=end,
        start_as_of=start_as_of if year > 1 else None,
        end_as_of=date_to,
    )


def year_end_report(principal, *, year, scope="", today=None, generated_at=None):
    today = today or timezone.localdate()
    generated_at = generated_at or timezone.now()
    date_from, date_to = year_bounds(year)
    cash_flow = cash_flow_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        grouping=GROUPING_MONTH,
        scope=scope,
        today=today,
    )
    spending = spending_by_category_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
        grouping=GROUPING_MONTH,
    )
    income = income_by_category_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
    )
    missing_months = [period for period in cash_flow.periods if period.missing_import]
    return SimpleNamespace(
        year=year,
        scope=scope or "",
        scope_label=SCOPE_LABELS.get(scope or "", SCOPE_LABELS[""]),
        date_from=date_from,
        date_to=date_to,
        generated_at=generated_at,
        cash_flow=cash_flow,
        spending=spending,
        income=income,
        accounts=_account_rows(principal, date_from, date_to, scope),
        tags=_tag_rows(principal, date_from, date_to, cash_flow.accounts),
        recurring=_recurring_rows(principal),
        net_worth=_net_worth_points(principal, year, scope),
        missing_months=missing_months,
    )


def csv_rows_for_section(report, section):
    if section == "cash-flow":
        for period in report.cash_flow.periods:
            yield {
                "month": period.start.isoformat()[:7],
                "label": period.label,
                "income": money_decimal(period.income_minor),
                "spending": money_decimal(period.spending_minor),
                "net": money_decimal(period.net_minor),
                "missing_import": "true" if period.missing_import else "false",
                "currency": "USD",
            }
        return
    if section == "spending":
        for row in report.spending.rows:
            yield {
                "parent": row.parent_name,
                "category": row.name,
                "spending": money_decimal(row.spending_minor),
                "currency": "USD",
            }
        return
    if section == "income":
        for row in report.income.rows:
            yield {
                "parent": row.parent_name,
                "category": row.name,
                "income": money_decimal(row.income_minor),
                "currency": "USD",
            }
        return
    if section == "accounts":
        for row in report.accounts:
            yield {
                "account": row.name,
                "income": money_decimal(row.income_minor),
                "spending": money_decimal(row.spending_minor),
                "net": money_decimal(row.net_minor),
                "currency": row.currency,
            }
        return
    if section == "tags":
        for row in report.tags:
            yield {
                "tag": row.name,
                "income": money_decimal(row.income_minor),
                "spending": money_decimal(row.spending_minor),
                "net": money_decimal(row.net_minor),
                "currency": "USD",
            }
        return
    if section == "recurring":
        for row in report.recurring.rows:
            yield {
                "name": row.name,
                "cadence": row.cadence,
                "annual_cost": money_decimal(row.annual_minor),
                "currency": row.currency,
            }
        return
    if section == "net-worth":
        points = (
            ("start", report.net_worth.start, report.net_worth.start_as_of),
            ("end", report.net_worth.end, report.net_worth.end_as_of),
        )
        for label, period, as_of in points:
            if period is None:
                continue
            yield {
                "point": label,
                "as_of": as_of.isoformat() if as_of else "",
                "assets": money_decimal(period.assets_minor),
                "liabilities": money_decimal(period.liabilities_minor),
                "net": money_decimal(period.net_minor),
                "currency": "USD",
            }
        return
    raise ValueError("Unknown year-end CSV section.")


def csv_fieldnames(section):
    headers = {
        "cash-flow": ("month", "label", "income", "spending", "net", "missing_import", "currency"),
        "spending": ("parent", "category", "spending", "currency"),
        "income": ("parent", "category", "income", "currency"),
        "accounts": ("account", "income", "spending", "net", "currency"),
        "tags": ("tag", "income", "spending", "net", "currency"),
        "recurring": ("name", "cadence", "annual_cost", "currency"),
        "net-worth": ("point", "as_of", "assets", "liabilities", "net", "currency"),
    }
    return headers[section]


def iter_csv_bytes(report, section):
    fieldnames = csv_fieldnames(section)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    yield buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)
    for row in csv_rows_for_section(report, section):
        writer.writerow(row)
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


def csv_filename(year, section):
    return f"year-end-{year}-{section}.csv"
