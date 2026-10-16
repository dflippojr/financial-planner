"""Upcoming charges, price-change flags, and missed-charge review for recurring series."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor
from .models import Account, Alert, AlertSettings, ImportBatch, RecurringSeries, _person_for
from .recurring_services import CADENCE_DAYS, next_charge_date

# A member's AlertSettings.recurring_price_percent; this is its default.
DEFAULT_PRICE_CHANGE_PERCENT = 10
# The usual price is the median of at most this many charges before the latest.
PRICE_BASELINE_CHARGES = 12
MIN_PRIOR_CHARGES = 2
UPCOMING_DAYS = 30


def _ordered_transactions(series):
    # A household bill's row reads the bill's current household charges, so it
    # does not wait for the owning member's series to refresh.
    bill_charges = getattr(series, "bill_charges", None)
    if bill_charges is not None:
        return bill_charges
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
    # None when there is no charge, or when the last one is so far in the
    # future that the next would fall past the end of the calendar.
    return next_charge_date([row.transaction_date for row in _ordered_transactions(series)], series.cadence)


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


def is_overdue(expected, cadence, today):
    """Past its expected date but still inside the cadence tolerance, so not yet missed.

    Review calls such a charge "due" and the bills calendar counts it today.
    """
    late_days = (today - expected).days
    return 0 < late_days <= cadence_tolerance_days(cadence)


def _within_cadence_tolerance(actual, expected, cadence):
    return abs((actual - expected).days) <= cadence_tolerance_days(cadence)


def _has_charge_near(series, expected):
    return any(
        _within_cadence_tolerance(txn.transaction_date, expected, series.cadence)
        for txn in _ordered_transactions(series)
    )


def _median_abs(values):
    ordered = sorted(abs(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[middle])
    return (Decimal(ordered[middle - 1]) + Decimal(ordered[middle])) / 2


def _percent_display(ratio):
    percent = (ratio * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percent}%"


@dataclass(frozen=True)
class UpcomingCharge:
    series: RecurringSeries
    expected_on: object
    amount_minor: int
    amount_display: str
    due: bool = False


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
    due = is_overdue(expected, series.cadence, today)
    if (expected < today and not due) or expected > horizon:
        return None
    return UpcomingCharge(
        series=series,
        expected_on=expected,
        amount_minor=series.typical_amount_minor,
        amount_display=series.amount_display,
        due=due,
    )


def price_drift(series):
    """The latest charge measured against the median of the charges before it.

    Returns a PriceChange whatever the size of the drift, or None when there is
    no baseline (fewer than two prior charges or a zero median) or the member
    already dismissed this amount. Callers apply a threshold with exceeds_percent.
    """
    if not _counts_in_review(series):
        return None
    rows = _ordered_transactions(series)
    prior = rows[:-1][-PRICE_BASELINE_CHARGES:]
    if len(prior) < MIN_PRIOR_CHARGES:
        return None
    latest = rows[-1]
    if series.acknowledged_amount_minor == latest.amount_minor:
        return None
    median = _median_abs(item.amount_minor for item in prior)
    if median == 0:
        return None
    ratio = (Decimal(abs(latest.amount_minor)) - median) / median
    usual = int(median.to_integral_value(rounding=ROUND_HALF_UP))
    return PriceChange(
        series=series,
        previous_minor=-usual,
        new_minor=latest.amount_minor,
        previous_display=format_minor(usual, series.currency),
        new_display=format_minor(abs(latest.amount_minor), series.currency),
        percent_display=_percent_display(ratio),
        charge_date=latest.transaction_date,
        ratio=ratio,
    )


def exceeds_percent(change, percent):
    """Strictly greater: a drift of exactly the threshold does not flag."""
    return abs(change.ratio) * 100 > percent


def price_change(series, percent=DEFAULT_PRICE_CHANGE_PERCENT):
    change = price_drift(series)
    if change is None or not exceeds_percent(change, percent):
        return None
    return change


def price_change_percent_for(principal):
    person = _person_for(principal)
    if person is None:
        return DEFAULT_PRICE_CHANGE_PERCENT
    value = (
        AlertSettings.objects.filter(person=person).values_list("recurring_price_percent", flat=True).first()
    )
    return value or DEFAULT_PRICE_CHANGE_PERCENT


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


def price_changes(series_list, percent=DEFAULT_PRICE_CHANGE_PERCENT):
    rows = [item for series in series_list if (item := price_change(series, percent))]
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
    from .alert_services import raise_alert, settings_for

    created = []
    link = reverse("recurring-review")
    percents = {}

    def recipient_percent(person):
        if person.pk not in percents:
            percents[person.pk] = settings_for(person).recurring_price_percent
        return percents[person.pk]

    drifts = [item for series in series_list if (item := price_drift(series))]
    for change in drifts:
        audience, account = _alert_audience(change.series)
        recipients = [person for person in audience if exceeds_percent(change, recipient_percent(person))]
        if not recipients:
            continue
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
                source_accounts=series_accounts(change.series),
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
                source_accounts=series_accounts(missed.series),
            )
        )
    return created


def build_recurring_review(principal, series_list, *, today):
    upcoming = upcoming_charges(series_list, today=today)
    changes = price_changes(series_list, price_change_percent_for(principal))
    missed = missed_charges(series_list, principal, today=today)
    cancelled = [
        SimpleNamespace(series=series, resumed=is_resumed(series), resumed_transactions=resumed_transactions(series))
        for series in series_list
        if series.status == RecurringSeries.Status.CONFIRMED and series.cancelled_at is not None
    ]
    return upcoming, changes, missed, cancelled
