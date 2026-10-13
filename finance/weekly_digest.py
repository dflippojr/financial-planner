"""Weekly digest: new recurring series, price changes and large transactions in one week.

Everything is computed on request from existing data and the viewer's visible
accounts. The outlier signals are the ones the app already raises alerts for:
the recurring price drift (recurring_review) and the member's large-transaction
threshold (AlertSettings).
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor
from .models import AlertSettings, RecurringSeries, Transaction, TransferPair, _person_for
from .recurring_review import DEFAULT_PRICE_CHANGE_PERCENT, price_changes

WEEK_CURRENT = "current"
WEEK_LAST = "last"
WEEKS = (WEEK_CURRENT, WEEK_LAST)
_NEW_SERIES_STATUSES = (RecurringSeries.Status.SUGGESTED, RecurringSeries.Status.CONFIRMED)


@dataclass(frozen=True)
class DigestItem:
    title: str
    detail: str
    amount_display: str
    on: object
    url: str


@dataclass(frozen=True)
class WeeklyDigest:
    week: str
    start: object
    end: object
    new_series: list
    price_changes: list
    large_transactions: list
    large_threshold_display: str | None
    large_transactions_url: str | None

    @property
    def total(self):
        return len(self.new_series) + len(self.price_changes) + len(self.large_transactions)


def week_window(today, week):
    """Monday-Sunday bounds; the current week ends today, the last week is the previous full week."""
    if week not in WEEKS:
        raise ValueError(f"Unknown digest week: {week!r}")
    monday = today - timedelta(days=today.weekday())
    if week == WEEK_CURRENT:
        return monday, today
    return monday - timedelta(days=7), monday - timedelta(days=1)


def _local_bounds(start, end):
    zone = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(start, time.min), zone),
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), zone),
    )


def _series_url(series):
    return f"{reverse('recurring-review')}#series-{series.pk}"


def _new_series(principal, start, end):
    since, until = _local_bounds(start, end)
    rows = (
        RecurringSeries.objects.visible_to(principal)
        .filter(status__in=_NEW_SERIES_STATUSES, created_at__gte=since, created_at__lt=until)
        .order_by("-created_at", "display_name", "pk")
    )
    return [
        DigestItem(
            title=series.display_name,
            detail=f"{series.get_cadence_display()} · {series.get_status_display()}",
            amount_display=format_minor(abs(series.typical_amount_minor), series.currency),
            on=timezone.localdate(series.created_at),
            url=_series_url(series),
        )
        for series in rows
    ]


def _price_changes(principal, start, end, percent):
    # Only series with a charge in the window can have their latest charge there.
    candidates = (
        RecurringSeries.objects.visible_to(principal)
        .filter(
            status=RecurringSeries.Status.CONFIRMED,
            members__transaction__transaction_date__gte=start,
            members__transaction__transaction_date__lte=end,
        )
        .distinct()
        .prefetch_related("members__transaction__account")
    )
    return [
        DigestItem(
            title=change.series.display_name,
            detail=f"Usually {change.previous_display} · {change.percent_display}",
            amount_display=change.new_display,
            on=change.charge_date,
            url=_series_url(change.series),
        )
        for change in price_changes(list(candidates), percent)
        if start <= change.charge_date <= end
    ]


def _large_transactions(principal, start, end, threshold):
    if threshold is None or threshold <= 0:
        return []
    transfers = TransferPair.objects.excluding_income_and_spending().visible_to(principal)
    rows = (
        Transaction.objects.visible_to(principal)
        .filter(
            Q(amount_minor__gte=threshold) | Q(amount_minor__lte=-threshold),
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            transaction_date__gte=start,
            transaction_date__lte=end,
        )
        .exclude(pk__in=transfers.values("leg_a_id"))
        .exclude(pk__in=transfers.values("leg_b_id"))
        .select_related("account")
        .order_by("-transaction_date", "-pk")
    )
    return [
        DigestItem(
            title=txn.description,
            detail=txn.account.name,
            amount_display=format_minor(txn.amount_minor, txn.currency),
            on=txn.transaction_date,
            url=reverse("transaction-edit", args=[txn.pk]),
        )
        for txn in rows
    ]


def _member_settings(principal):
    person = _person_for(principal)
    values = None
    if person is not None:
        values = (
            AlertSettings.objects.filter(person=person)
            .values_list("large_transaction_minor", "recurring_price_percent")
            .first()
        )
    if values is None:
        return None, DEFAULT_PRICE_CHANGE_PERCENT
    threshold, percent = values
    return threshold, percent or DEFAULT_PRICE_CHANGE_PERCENT


def _large_list_url(start, end, threshold):
    query = urlencode(
        {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "amount_min": f"{Decimal(threshold) / 100:.2f}",
            "amount_mode": "absolute",
        }
    )
    return f"{reverse('transaction-list')}?{query}"


def build_weekly_digest(principal, *, today, week=WEEK_LAST):
    """What started charging the viewer and what looked unusual in one week.

    week is "last" (the previous complete Monday-Sunday week) or "current" (this
    Monday through today), both in local dates. Only series, transactions and
    accounts the viewer may already see are included.
    """
    start, end = week_window(today, week)
    threshold, percent = _member_settings(principal)
    has_threshold = threshold is not None and threshold > 0
    return WeeklyDigest(
        week=week,
        start=start,
        end=end,
        new_series=_new_series(principal, start, end),
        price_changes=_price_changes(principal, start, end, percent),
        large_transactions=_large_transactions(principal, start, end, threshold),
        large_threshold_display=format_minor(threshold) if has_threshold else None,
        large_transactions_url=_large_list_url(start, end, threshold) if has_threshold else None,
    )
