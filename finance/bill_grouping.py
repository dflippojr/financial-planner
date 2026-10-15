"""One definition of a household bill, shared by the Recurring page, cancel and the projection.

Members may each confirm their own series for the same household bill. Series
belong to one bill when shared charges link them, directly or through a third
series, so a charge is one bill whatever name or cadence each member gave it.
The copy with the latest eligible charge holds the current price and schedule,
then the lowest id. Everything here is pure grouping and eligibility; the page
keeps its own presentation, and detection stays in `recurring_services`.
"""

from django.db.models import Exists, OuterRef

from .category_services import exclusion_exists_for
from .models import Account, RecurringSeries, RecurringSeriesMember, Transaction


def eligible_bill_charges(principal, *, household_only=True):
    """Charges that count as a bill's history: the ones the actual cash flow counts.

    Active cash-flow charges on live accounts the viewer can see, leaving out
    confirmed transfer legs, so a transfer or an archived account's charge never
    sets a bill's last date or ranks its copies. `household_only` judges
    transfer pairs by their household legs, so every member gets one answer.
    """
    return (
        Transaction.objects.visible_to(principal)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            account__status=Account.Status.ACTIVE,
            account__archived_at__isnull=True,
        )
        .annotate(_excluded=exclusion_exists_for(principal, household_only=household_only))
        .filter(_excluded=False)
    )


def stored_charge_ids(series_ids):
    """Each series' stored charge ids, as {series id: set of transaction ids}."""
    charges = {pk: set() for pk in series_ids}
    rows = RecurringSeriesMember.objects.filter(series_id__in=list(series_ids)).values_list(
        "series_id", "transaction_id"
    )
    for series_id, transaction_id in rows:
        charges[series_id].add(transaction_id)
    return charges


def group_bills(charges_by_series):
    """Split series ids into bills: lists of ids linked by a shared charge, in input order."""
    parent = {pk: pk for pk in charges_by_series}

    def find(pk):
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    first_owner = {}
    for pk, charge_ids in charges_by_series.items():
        for charge_id in charge_ids:
            if charge_id in first_owner:
                parent[find(pk)] = find(first_owner[charge_id])
            else:
                first_owner[charge_id] = pk
    bills = {}
    for pk in charges_by_series:
        bills.setdefault(find(pk), []).append(pk)
    return list(bills.values())


def is_current_candidate(series):
    """A copy that still projects the bill: confirmed, active and not cancelled."""
    return (
        series.status == RecurringSeries.Status.CONFIRMED and series.is_active and series.cancelled_at is None
    )


def choose_copy(copies, latest_ordinal):
    """The copy holding the current price: latest eligible charge, then lowest id.

    `latest_ordinal` maps a series id to the ordinal of its last eligible
    charge; a copy with none ranks last.
    """
    return min(copies, key=lambda series: (-latest_ordinal.get(series.pk, 0), series.pk))


def charges_in(queryset, series_ids):
    """`queryset` narrowed to charges stored on any of `series_ids`."""
    return queryset.filter(
        Exists(
            RecurringSeriesMember.objects.filter(series_id__in=list(series_ids), transaction_id=OuterRef("pk"))
        )
    )
