"""Private Google Sheet month-total comparison against cash-flow totals."""

import csv
import io
import re
from calendar import month_name
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.db import transaction
from django.utils import timezone

from .cash_flow import default_date_range, format_minor, selected_accounts
from .category_services import income_and_spending_totals
from .models import Person, SheetComparisonSettings, SheetMonthTotal
from .months import month_end, month_start


_DENIED = "Operation is not permitted."
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_MONTH_ROWS = 240
DEFAULT_TOLERANCE_MINOR = 100
SESSION_KEY = "sheet_comparison_pending"
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._ -]{1,255}$")
MONTH_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m",
    "%m/%Y",
    "%m-%Y",
    "%b %Y",
    "%B %Y",
    "%b-%Y",
    "%B-%Y",
)


class SheetCsvError(ValidationError):
    """User-facing CSV error that never includes cell contents."""


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist as exc:
            raise PermissionDenied(_DENIED) from exc
    raise PermissionDenied(_DENIED)


def settings_for(principal):
    return SheetComparisonSettings.objects.visible_to(principal).first()


def _source_name(filename):
    name = (filename or "sheet.csv").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or not SAFE_FILENAME.fullmatch(name):
        return "uploaded.csv"
    return name


def parse_sheet_month(value):
    text = (value or "").strip()
    if not text:
        raise ValueError
    for pattern in MONTH_FORMATS:
        try:
            parsed = datetime.strptime(text, pattern).date()
        except ValueError:
            continue
        return month_start(parsed)
    raise ValueError


def _parse_money(value):
    normalized = (value or "").strip().replace("$", "").replace(" ", "")
    if not normalized:
        raise ValueError
    negative = False
    if normalized.startswith("(") and normalized.endswith(")"):
        negative = True
        normalized = normalized[1:-1]
    normalized = normalized.replace(",", "")
    amount = Decimal(normalized)
    if not amount.is_finite():
        raise ValueError
    if negative:
        amount = -amount
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError
    return int(minor)


def spending_minor_from_cell(value, spending_sign):
    parsed = _parse_money(value)
    if spending_sign == SheetComparisonSettings.SpendingSign.SIGNED:
        return -parsed
    if parsed < 0:
        raise ValueError
    return parsed


def read_sheet_csv(raw_bytes):
    if raw_bytes is None or len(raw_bytes) > MAX_FILE_BYTES:
        raise SheetCsvError("Choose a CSV file of at most 5 MB.")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SheetCsvError("The file must be UTF-8 CSV.") from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        headers = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise SheetCsvError("The CSV needs a header row and one row per month.") from exc
    headers = tuple(item.strip() for item in headers)
    if not headers or any(not item for item in headers) or len(headers) != len(set(headers)):
        raise SheetCsvError("Column names must be unique and non-empty.")
    rows = []
    try:
        for number, cells in enumerate(reader, start=2):
            if number - 1 > MAX_MONTH_ROWS:
                raise SheetCsvError("The CSV has too many month rows.")
            if len(cells) < len(headers):
                cells = list(cells) + [""] * (len(headers) - len(cells))
            rows.append(dict(zip(headers, (cell.strip() for cell in cells[: len(headers)]), strict=True)))
    except csv.Error as exc:
        raise SheetCsvError("That file couldn't be read as CSV.") from exc
    if not rows:
        raise SheetCsvError("The CSV needs a header row and one row per month.")
    return headers, rows


def mapping_matches_headers(settings_row, headers):
    if settings_row is None:
        return False
    needed = {settings_row.month_column, settings_row.income_column, settings_row.spending_column}
    return needed <= set(headers)


def _row_totals(row, mapping):
    month = parse_sheet_month(row.get(mapping.month_column, ""))
    income_minor = _parse_money(row.get(mapping.income_column, ""))
    spending = spending_minor_from_cell(row.get(mapping.spending_column, ""), mapping.spending_sign)
    return month, income_minor, spending


def parsed_month_totals(rows, mapping):
    by_month = {}
    for row in rows:
        try:
            month, income_minor, spending = _row_totals(row, mapping)
        except (ValueError, InvalidOperation, KeyError) as exc:
            raise SheetCsvError("A month, income, or spending value could not be read.") from exc
        if month in by_month:
            raise SheetCsvError("Each month may appear only once.")
        by_month[month] = (income_minor, spending)
    return by_month


@transaction.atomic
def save_mapping(principal, *, month_column, income_column, spending_column, spending_sign, headers):
    person = _person_for(principal)
    columns = (month_column, income_column, spending_column)
    if len(set(columns)) != 3:
        raise SheetCsvError("Choose three different columns.")
    if any(column not in headers for column in columns):
        raise SheetCsvError("Choose columns from this CSV.")
    if spending_sign not in SheetComparisonSettings.SpendingSign.values:
        raise SheetCsvError("Choose how spending is signed.")
    settings_row, _created = SheetComparisonSettings.objects.update_or_create(
        member=person,
        defaults={
            "month_column": month_column,
            "income_column": income_column,
            "spending_column": spending_column,
            "spending_sign": spending_sign,
        },
    )
    return SheetComparisonSettings.objects.visible_to(person).get(pk=settings_row.pk)


@transaction.atomic
def store_month_totals(principal, by_month, source):
    person = _person_for(principal)
    source_name = _source_name(source)
    kept_notes = {
        item.month: item.note
        for item in SheetMonthTotal.objects.visible_to(person)
    }
    SheetMonthTotal.objects.visible_to(person).delete()
    SheetMonthTotal.objects.bulk_create(
        [
            SheetMonthTotal(
                member=person,
                month=month,
                income_minor=income_minor,
                spending_minor=spending_minor,
                source=source_name,
                note=kept_notes.get(month, ""),
            )
            for month, (income_minor, spending_minor) in sorted(by_month.items())
        ]
    )
    return list(SheetMonthTotal.objects.visible_to(person).order_by("month"))


def set_tolerance(principal, tolerance_minor):
    person = _person_for(principal)
    if tolerance_minor is None or tolerance_minor < 0:
        raise SheetCsvError("Tolerance must be zero or greater.")
    settings_row = SheetComparisonSettings.objects.visible_to(person).first()
    if settings_row is None:
        raise SheetCsvError("Upload month totals before setting a tolerance.")
    settings_row.tolerance_minor = tolerance_minor
    settings_row.save(update_fields=("tolerance_minor", "updated_at"))
    return settings_row


def save_month_note(principal, month, note):
    person = _person_for(principal)
    month = month_start(month)
    row = SheetMonthTotal.objects.visible_to(person).filter(month=month).first()
    if row is None:
        raise PermissionDenied(_DENIED)
    row.note = note or ""
    row.save(update_fields=("note", "updated_at"))
    return row


@transaction.atomic
def delete_comparison_data(principal):
    person = _person_for(principal)
    SheetMonthTotal.objects.visible_to(person).delete()
    SheetComparisonSettings.objects.visible_to(person).delete()


def _app_window(month, today):
    start = month_start(month)
    full_end = month_end(month)
    end = min(full_end, today)
    if end < start:
        end = start
    return SimpleNamespace(start=start, end=end, calendar_end=full_end)


def _month_transactions_url(window, *, account=None, scope="", tag=None):
    query = {"date_from": window.start.isoformat(), "date_to": window.end.isoformat()}
    if account is not None:
        query["account"] = str(account.pk)
    if scope:
        query["scope"] = scope
    if tag is not None:
        query["tag"] = str(tag.pk)
    return f"{reverse('transaction-list')}?{urlencode(query)}"


def comparison_rows(principal, *, account=None, scope="", tag=None, today=None):
    person = _person_for(principal)
    today = today or timezone.localdate()
    settings_row = SheetComparisonSettings.objects.visible_to(person).first()
    tolerance = settings_row.tolerance_minor if settings_row else DEFAULT_TOLERANCE_MINOR
    accounts = selected_accounts(person, account=account, scope=scope, cash_flow_only=True)
    rows = []
    for stored in SheetMonthTotal.objects.visible_to(person).order_by("-month"):
        window = _app_window(stored.month, today)
        app = income_and_spending_totals(
            person,
            date_from=window.start,
            date_to=window.end,
            accounts=accounts,
            tag=tag,
        )
        sheet_net = stored.income_minor - stored.spending_minor
        income_diff = app.income_minor - stored.income_minor
        spending_diff = app.spending_minor - stored.spending_minor
        net_diff = app.net_minor - sheet_net
        matches = abs(net_diff) <= tolerance
        rows.append(
            SimpleNamespace(
                month=stored.month,
                label=f"{month_name[stored.month.month]} {stored.month.year}",
                source=stored.source,
                note=stored.note,
                sheet_income_minor=stored.income_minor,
                sheet_spending_minor=stored.spending_minor,
                sheet_net_minor=sheet_net,
                app_income_minor=app.income_minor,
                app_spending_minor=app.spending_minor,
                app_net_minor=app.net_minor,
                income_diff_minor=income_diff,
                spending_diff_minor=spending_diff,
                net_diff_minor=net_diff,
                sheet_income_display=format_minor(stored.income_minor),
                sheet_spending_display=format_minor(stored.spending_minor),
                sheet_net_display=format_minor(sheet_net),
                app_income_display=format_minor(app.income_minor),
                app_spending_display=format_minor(app.spending_minor),
                app_net_display=format_minor(app.net_minor),
                income_diff_display=format_minor(income_diff),
                spending_diff_display=format_minor(spending_diff),
                net_diff_display=format_minor(net_diff),
                matches=matches,
                drilldown_url=_month_transactions_url(window, account=account, scope=scope, tag=tag),
            )
        )
    range_from, _range_to = default_date_range(today)
    recent = [row for row in rows if row.month >= month_start(range_from)]
    matched = sum(1 for row in recent if row.matches)
    return SimpleNamespace(
        rows=rows,
        settings=settings_row,
        tolerance_minor=tolerance,
        tolerance_display=format_minor(tolerance),
        recent_count=len(recent),
        recent_matched=matched,
        accounts=accounts,
    )
