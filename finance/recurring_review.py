"""Upcoming charges, price-change flags, and missed-charge review for recurring series."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor
from .models import Account, Alert, ImportBatch, RecurringSeries
from .projection import step_occurrence
from .recurring_services import CADENCE_DAYS, typical_amount_minor_from

PRICE_CHANGE_RATIO = Decimal("0.10")
UPCOMING_DAYS = 30


def _ordered_transactions(series):
    members = getattr(series, "_prefetched_objects_cache", {}).get("members")
    if members is None:
        members = series.members.select_related("transaction", "transaction__account").all()
    rows = [member.transaction for member in members]
    rows.sort(key=lambda row: (row.transaction_date, row.pk))
    return rows


def last_charge_date(series):
    rows = _ordered_transactions(series)
    if not rows:
        return None
    return rows[-1].transaction_date


def next_expected_date(series):
    last_on = last_charge_date(series)
    if last_on is None:
        return None
    try:
        return step_occurrence(last_on, series.cadence)
    except (OverflowError, ValueError):
        # The last charge is so far in the future that the next one would
        # fall past the end of the calendar.
        return None


def cadence_tolerance_days(cadence):
    return CADENCE_DAYS[cadence][1]


def _counts_in_review(series):
    return (
        series.status == RecurringSeries.Status.CONFIRMED
        and series.is_active
        and series.cancelled_at is None
    )


def series_accounts(series):
    seen = {}
    for txn in _ordered_transactions(series):
        seen[txn.account_id] = txn.account
    return list(seen.values())


def missing_import_on(principal, accounts, on_date):
    if not accounts:
        return False
    covered = set(
        ImportBatch.objects.visible_to(principal)
        .filter(
            status=ImportBatch.Status.ACTIVE,
            account__in=accounts,
            date_range_start__lte=on_date,
            date_range_end__gte=on_date,
        )
        .exclude(source=ImportBatch.Source.MANUAL)
        .values_list("account_id", flat=True)
    )
    return any(account.pk not in covered for account in accounts)


def _within_cadence_tolerance(actual, expected, cadence):
    return abs((actual - expected).days) <= cadence_tolerance_days(cadence)


def _has_charge_near(series, expected):
    return any(
        _within_cadence_tolerance(txn.transaction_date, expected, series.cadence)
        for txn in _ordered_transactions(series)
    )


def _ratio(previous_minor, new_minor):
    previous = abs(previous_minor)
    if previous == 0:
        return None
    return (Decimal(abs(new_minor) - previous)) / Decimal(previous)


def _percent_display(ratio):
    percent = (ratio * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percent}%"


@dataclass(frozen=True)
class UpcomingCharge:
    series: RecurringSeries
    expected_on: object
    amount_minor: int
    amount_display: str


@dataclass(frozen=True)
class PriceChange:
    series: RecurringSeries
    previous_minor: int
    new_minor: int
    previous_display: str
    new_display: str
    percent_display: str
    charge_date: object
    ratio: Decimal


@dataclass(frozen=True)
class MissedCharge:
    series: RecurringSeries
    expected_on: object
    amount_display: str
    missing_import: bool


def upcoming_charge(series, *, today):
    if not _counts_in_review(series):
        return None
    expected = next_expected_date(series)
    if expected is None:
        return None
    horizon = today + timedelta(days=UPCOMING_DAYS)
    if expected < today or expected > horizon:
        return None
    return UpcomingCharge(
        series=series,
        expected_on=expected,
        amount_minor=series.typical_amount_minor,
        amount_display=series.amount_display,
    )


def price_change(series):
    if not _counts_in_review(series):
        return None
    rows = _ordered_transactions(series)
    if len(rows) < 2:
        return None
    latest = rows[-1]
    typical = typical_amount_minor_from(rows)
    ratio = _ratio(typical, latest.amount_minor)
    if ratio is None or abs(ratio) < PRICE_CHANGE_RATIO:
        return None
    if series.acknowledged_amount_minor == latest.amount_minor:
        return None
    previous = typical_amount_minor_from(rows[:-1])
    display_ratio = _ratio(previous, latest.amount_minor) or ratio
    return PriceChange(
        series=series,
        previous_minor=previous,
        new_minor=latest.amount_minor,
        previous_display=format_minor(abs(previous), series.currency),
        new_display=format_minor(abs(latest.amount_minor), series.currency),
        percent_display=_percent_display(display_ratio),
        charge_date=latest.transaction_date,
        ratio=display_ratio,
    )


def missed_charge(series, principal, *, today):
    if not _counts_in_review(series):
        return None
    expected = next_expected_date(series)
    if expected is None:
        return None
    if (expected - today).days >= -cadence_tolerance_days(series.cadence):
        return None
    if _has_charge_near(series, expected):
        return None
    accounts = series_accounts(series)
    return MissedCharge(
        series=series,
        expected_on=expected,
        amount_display=series.amount_display,
        missing_import=missing_import_on(principal, accounts, expected),
    )


def resumed_transactions(series):
    if series.cancelled_at is None:
        return []
    cutoff = timezone.localdate(series.cancelled_at)
    return [txn for txn in _ordered_transactions(series) if txn.transaction_date > cutoff]


def is_resumed(series):
    return bool(resumed_transactions(series))


def upcoming_charges(series_list, *, today):
    rows = [item for series in series_list if (item := upcoming_charge(series, today=today))]
    rows.sort(key=lambda item: (item.expected_on, item.series.display_name, item.series.pk))
    return rows


def price_changes(series_list):
    rows = [item for series in series_list if (item := price_change(series))]
    rows.sort(key=lambda item: (item.charge_date, item.series.display_name, item.series.pk), reverse=True)
    return rows


def missed_charges(series_list, principal, *, today):
    rows = [item for series in series_list if (item := missed_charge(series, principal, today=today))]
    rows.sort(key=lambda item: (item.expected_on, item.series.display_name, item.series.pk))
    return rows


def _alert_audience(series):
    from .alert_services import audience_for_account

    accounts = series_accounts(series)
    if not accounts:
        return [series.person], None
    if all(account.scope == Account.Scope.HOUSEHOLD for account in accounts):
        account = accounts[0]
        return audience_for_account(account), account
    private = next((account for account in accounts if account.scope == Account.Scope.PRIVATE), accounts[0])
    return [series.person], private


def raise_recurring_review_alerts(principal, series_list, *, today):
    from .alert_services import raise_alert

    created = []
    link = reverse("recurring-review")
    for change in price_changes(series_list):
        recipients, account = _alert_audience(change.series)
        title = (
            f"{change.series.display_name} changed from {change.previous_display} to {change.new_display}"
        )
        created.extend(
            raise_alert(
                recipients,
                Alert.Kind.RECURRING_PRICE,
                title,
                link,
                f"recurring:{change.series.pk}:{change.new_minor}",
                account=account,
            )
        )
    for missed in missed_charges(series_list, principal, today=today):
        recipients, account = _alert_audience(missed.series)
        title = f"{missed.series.display_name} was expected on {missed.expected_on.isoformat()} and was not seen"
        created.extend(
            raise_alert(
                recipients,
                Alert.Kind.RECURRING_MISSED,
                title,
                link,
                f"recurring:{missed.series.pk}:missed:{missed.expected_on.isoformat()}",
                account=account,
            )
        )
    return created


def build_recurring_review(principal, series_list, *, today):
    upcoming = upcoming_charges(series_list, today=today)
    changes = price_changes(series_list)
    missed = missed_charges(series_list, principal, today=today)
    cancelled = [
        SimpleNamespace(series=series, resumed=is_resumed(series), resumed_transactions=resumed_transactions(series))
        for series in series_list
        if series.status == RecurringSeries.Status.CONFIRMED and series.cancelled_at is not None
    ]
    return upcoming, changes, missed, cancelled
