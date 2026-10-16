"""A bill due late in the month keeps its day after a short month (#505). Synthetic data only."""

from datetime import date
from types import SimpleNamespace

import pytest

from finance.bill_grouping import eligible_bill_charges
from finance.models import Account, Alert, RecurringSeries
from finance.planning_services import visible_projection_inputs
from finance.projection import occurrence_dates
from finance.recurring_review import missed_charge, next_expected_date, raise_recurring_review_alerts
from finance.recurring_services import (
    confirm_recurring_series,
    detect_recurring_series,
    next_charge_date,
    refresh_recurring_series,
    resolve_household_bills,
)
from tests.test_recurring_review import (
    add_member,
    make_account,
    make_household,
    make_person,
    make_series,
    make_transaction,
)

MONTHLY = RecurringSeries.Cadence.MONTHLY
RENT_DATES = (date(2029, 10, 31), date(2029, 11, 30), date(2029, 12, 31), date(2030, 1, 31), date(2030, 2, 28))


def _rows(dates, description="Synthetic Rent"):
    return [
        SimpleNamespace(pk=index, description=description, transaction_date=day, amount_minor=-150000, currency="USD")
        for index, day in enumerate(dates, start=1)
    ]


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        # A short month does not move the anchor day.
        ((date(2030, 1, 31), date(2030, 2, 28)), date(2030, 3, 31)),
        ((date(2028, 1, 31), date(2028, 2, 29)), date(2028, 3, 31)),
        # A charge that slipped into the next month stands for the month it was due in.
        ((date(2030, 1, 31), date(2030, 2, 28), date(2030, 4, 1)), date(2030, 4, 30)),
        ((date(2030, 1, 30), date(2030, 3, 1)), date(2030, 3, 30)),
        # A charge a day early, before the 28th, keeps the anchor too.
        ((date(2029, 12, 31), date(2030, 1, 31), date(2030, 2, 27)), date(2030, 3, 31)),
        # A bill on the 28th still expects the 28th.
        ((date(2030, 1, 28), date(2030, 2, 28)), date(2030, 3, 28)),
        # A bill due mid-month steps from its last charge, even as it drifts earlier.
        ((date(2030, 2, 14), date(2030, 3, 13), date(2030, 4, 11)), date(2030, 5, 11)),
        # A bill due on the 1st that once posted early on the 30th stays a 1st bill.
        ((date(2030, 1, 1), date(2030, 1, 30), date(2030, 3, 1)), date(2030, 4, 1)),
        # A bill that moved to a new day follows its latest charge.
        ((date(2030, 4, 30), date(2030, 5, 31), date(2030, 6, 15)), date(2030, 7, 15)),
        ((date(2029, 12, 31), date(2030, 3, 31)), date(2030, 6, 30)),
    ],
)
def test_next_charge_date_keeps_the_anchor_day(dates, expected):
    cadence = RecurringSeries.Cadence.QUARTERLY if dates[-1] == date(2030, 3, 31) else MONTHLY
    assert next_charge_date(dates, cadence) == expected


@pytest.mark.parametrize(
    "dates",
    [
        (date(2030, 1, 31), date(2030, 2, 28), date(2030, 4, 1)),
        (date(2028, 1, 31), date(2028, 2, 29), date(2028, 4, 1)),
        (date(2029, 12, 30), date(2030, 1, 30), date(2030, 3, 1), date(2030, 3, 30)),
        (*RENT_DATES, date(2030, 4, 1), date(2030, 4, 30), date(2030, 5, 31), date(2030, 7, 1)),
        (date(2030, 2, 14), date(2030, 3, 13), date(2030, 4, 11), date(2030, 5, 10), date(2030, 6, 12)),
        (date(2029, 12, 31), date(2030, 1, 31), date(2030, 2, 27), date(2030, 3, 31)),
    ],
)
def test_detection_chains_across_a_clamped_month(dates):
    (series,) = detect_recurring_series(_rows(dates))

    assert series.cadence == MONTHLY
    assert series.transaction_ids == tuple(range(1, len(dates) + 1))


def test_detection_keeps_a_chain_whose_anchor_differs_from_the_first_found():
    # Jan 31 reaches Feb 28 first, but its anchor (31) would expect Mar 31 and
    # miss Mar 27; the chain from Feb 1 anchors on the 28th and reaches it.
    (series,) = detect_recurring_series(
        _rows((date(2030, 1, 31), date(2030, 2, 1), date(2030, 2, 28), date(2030, 3, 27)))
    )

    assert series.transaction_ids == (2, 3, 4)
    assert series.status == RecurringSeries.Status.SUGGESTED


def test_detection_keeps_a_chain_whose_anchor_splits_one_charge_later():
    # Mar 30 is reached from Feb 27 and from Mar 2 with the same anchor, but
    # only the chain through Mar 2 lets May 1 stand for Apr 30.
    (series,) = detect_recurring_series(
        _rows((date(2030, 2, 27), date(2030, 3, 2), date(2030, 3, 30), date(2030, 5, 1), date(2030, 6, 3)))
    )

    assert series.cadence == MONTHLY
    assert series.transaction_ids == (2, 3, 4, 5)


@pytest.mark.django_db
def test_a_confirmed_bill_on_the_31st_continues_with_a_charge_on_the_1st():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    for day in RENT_DATES:
        make_transaction(owner, account, transaction_date=day, amount_minor=-150000, description="Synthetic Rent")
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    confirm_recurring_series(owner, series.pk)
    assert next_expected_date(series) == date(2030, 3, 31)

    make_transaction(owner, account, transaction_date=date(2030, 4, 1), amount_minor=-150000, description="Synthetic Rent")
    refresh_recurring_series(owner)

    series = RecurringSeries.objects.get(is_active=True)
    assert date(2030, 4, 1) in set(series.members.values_list("transaction__transaction_date", flat=True))
    assert missed_charge(series, owner, today=date(2030, 4, 2)) is None
    assert raise_recurring_review_alerts(owner, [series], today=date(2030, 4, 2)) == []
    assert not Alert.objects.filter(kind=Alert.Kind.RECURRING_MISSED).exists()
    assert next_expected_date(series) == date(2030, 4, 30)


@pytest.mark.django_db
def test_the_projection_keeps_the_anchor_day_after_february():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(owner, name="Synthetic Rent", cadence=MONTHLY, typical_minor=-150000)
    for day in RENT_DATES:
        add_member(series, make_transaction(owner, account, transaction_date=day, amount_minor=-150000, description="Synthetic Rent"))

    (item,) = [item for item in visible_projection_inputs(owner) if item.source == "series"]

    assert item.start == date(2030, 3, 31)
    window = occurrence_dates(item.start, item.end, item.cadence, date(2030, 3, 1), date(2031, 2, 28), item.anchor_day)
    assert window[:4] == [date(2030, 3, 31), date(2030, 4, 30), date(2030, 5, 31), date(2030, 6, 30)]
    assert window[-1] == date(2031, 2, 28)


@pytest.mark.django_db
def test_household_continuation_follows_the_anchor_day():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    series = make_series(owner, name="Synthetic Rent", cadence=MONTHLY, typical_minor=-150000)
    for day in RENT_DATES:
        add_member(series, make_transaction(owner, shared, transaction_date=day, amount_minor=-150000, description="Synthetic Rent"))
    make_transaction(member, shared, transaction_date=date(2030, 4, 1), amount_minor=-150000, description="Synthetic Rent")

    (bill,) = resolve_household_bills([series], eligible_bill_charges(member))

    assert bill.last_on == date(2030, 4, 1)
