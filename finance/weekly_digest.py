"""Weekly digest: new recurring series, price changes and large transactions in one week.

Everything is computed on request from existing data and the viewer's visible
accounts. The outlier signals are the ones the app already raises alerts for:
the recurring price drift (recurring_review) and the member's large-transaction
threshold (AlertSettings).
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db.models import Min, Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor
from .category_services import exclusion_exists_for
from .models import AlertSettings, RecurringSeries, RecurringSeriesMember, Transaction, _person_for
from .recurring_review import DEFAULT_PRICE_CHANGE_PERCENT, acknowledged_amounts, price_changes
from .recurring_services import CADENCE_DAYS, MIN_DETECTED_CHARGES

WEEK_CURRENT = "current"
WEEK_LAST = "last"
WEEKS = (WEEK_CURRENT, WEEK_LAST)
_NEW_SERIES_STATUSES = (RecurringSeries.Status.SUGGESTED, RecurringSeries.Status.CONFIRMED)
# A cadence the detector has no period for counts as monthly: the most common
# subscription cadence, and short enough that charges from a history import
# (a year or more old) stay out.
_FALLBACK_CADENCE = RecurringSeries.Cadence.MONTHLY


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


def recent_span_days(cadence):
    """How long before its creation a new series' first charge may be (#698).

    The detector suggests a series only once it has MIN_DETECTED_CHARGES charges
    on the cadence, so a genuinely new subscription's first charge is already
    about two periods old when its series appears; a hand-made series needs
    fewer charges. The span is

        MIN_DETECTED_CHARGES * (period + tolerance)

    with the period and date tolerance taken from the detector's CADENCE_DAYS:
    one period more than detection needs, plus each charge's allowed lateness.
    That is 30 days weekly, 51 biweekly, 99 monthly, 285 quarterly and 1,110
    annual. A cadence missing from CADENCE_DAYS uses the monthly span.
    """
    period, tolerance = CADENCE_DAYS.get(cadence, CADENCE_DAYS[_FALLBACK_CADENCE])
    return MIN_DETECTED_CHARGES * (period + tolerance)


def _first_charge_is_recent(series):
    created_on = timezone.localdate(series.created_at)
    return series.first_charge >= created_on - timedelta(days=recent_span_days(series.cadence))


def _series_url(series):
    return f"{reverse('recurring-review')}#series-{series.pk}"


def _active_members():
    """Members whose charge is still active.

    Undoing an import archives its charges, but their memberships stay until the
    Recurring page next refreshes the series; the digest must not count them meanwhile.
    """
    return RecurringSeriesMember.objects.filter(transaction__status=Transaction.Status.ACTIVE)


def _new_series(principal, start, end):
    """Series spotted in the window whose first active charge is recent (#698).

    Importing history creates series this week for charges that began months or
    years ago; those are not new, so a series counts only when its first charge
    is within recent_span_days of its creation. Rows show that first charge's
    date and sort by it, newest first.
    """
    since, until = _local_bounds(start, end)
    rows = (
        RecurringSeries.objects.visible_to(principal)
        # An inactive series has lost its members, possibly to revoked access, so it is left out.
        .filter(status__in=_NEW_SERIES_STATUSES, is_active=True, created_at__gte=since, created_at__lt=until)
        # Only active charges count, as in _active_members; a series with none has no first charge.
        .annotate(
            first_charge=Min(
                "members__transaction__transaction_date",
                filter=Q(members__transaction__status=Transaction.Status.ACTIVE),
            )
        )
        .filter(first_charge__isnull=False)
        .order_by("-first_charge", "display_name", "pk")
    )
    return [
        DigestItem(
            title=series.display_name,
            detail=f"{series.get_cadence_display()} · {series.get_status_display()}",
            amount_display=format_minor(abs(series.typical_amount_minor), series.currency),
            on=series.first_charge,
            url=_series_url(series),
        )
        for series in rows
        # The span depends on each row's cadence and creation day, so it is checked on the fetched rows.
        if _first_charge_is_recent(series)
    ]


def _price_changes(principal, start, end, percent):
    # Only series with a charge in the window can have their latest charge there.
    candidates = list(
        RecurringSeries.objects.visible_to(principal)
        .filter(
            status=RecurringSeries.Status.CONFIRMED,
            members__transaction__status=Transaction.Status.ACTIVE,
            members__transaction__transaction_date__gte=start,
            members__transaction__transaction_date__lte=end,
        )
        .distinct()
        .prefetch_related(Prefetch("members", queryset=_active_members().select_related("transaction__account")))
    )
    return [
        DigestItem(
            title=change.series.display_name,
            detail=f"Usually {change.previous_display} · {change.percent_display}",
            amount_display=change.new_display,
            on=change.charge_date,
            url=_series_url(change.series),
        )
        for change in price_changes(candidates, percent, acknowledged_amounts(principal, candidates))
        if start <= change.charge_date <= end
    ]


def _large_transactions(principal, start, end, threshold):
    if threshold is None or threshold <= 0:
        return []
    rows = (
        Transaction.objects.visible_to(principal)
        .filter(
            Q(amount_minor__gte=threshold) | Q(amount_minor__lte=-threshold),
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            transaction_date__gte=start,
            transaction_date__lte=end,
        )
        .exclude(exclusion_exists_for(principal))
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
    )
