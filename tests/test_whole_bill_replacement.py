"""A plan that replaces one member's copy of a joint bill replaces it in every member's own view (#522).

Synthetic data only.
"""

from datetime import date

import pytest

from finance.bills_calendar import calendar_inputs
from finance.models import Account, Membership, PlannedItem
from finance.planning_services import projected_months_for, visible_projection_inputs
from tests.test_savings_goal_plan import TODAY, household_fixture, make_account, make_confirmed_series, make_person


def _replace(owner, series, *, household=None, name="Synthetic Electric plan"):
    return PlannedItem.objects.create(
        owner=owner,
        household=household,
        scope=PlannedItem.Scope.HOUSEHOLD if household else PlannedItem.Scope.PRIVATE,
        name=name,
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=250_000,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
        replaces_series=series,
    )


# The household fixture's own planned income and rent are not part of these bills.
FIXTURE_ITEMS = {"Synthetic pay", "Synthetic rent"}


def _views(person, scope=""):
    projection = sorted(item.name for item in visible_projection_inputs(person.user, scope=scope))
    calendar = sorted(item.name for item in calendar_inputs(person.user, scope=scope, today=TODAY))
    return [name for name in projection if name not in FIXTURE_ITEMS], [name for name in calendar if name not in FIXTURE_ITEMS]


def _joint_bill():
    owner, member, household = household_fixture()
    shared = Account.objects.get(name="Synthetic Joint")
    owner_series = make_confirmed_series(owner, "Synthetic Electric", shared)
    member_series = make_confirmed_series(member, "Synthetic Electric", shared, same_charges_as=owner_series)
    return owner, member, household, owner_series, member_series


@pytest.mark.django_db
def test_a_household_plan_replaces_the_bill_once_in_every_members_combined_view():
    owner, member, household, owner_series, _member_series = _joint_bill()
    _replace(owner, owner_series, household=household)

    expected = (["Synthetic Electric plan"], ["Synthetic Electric plan"])
    for person in (owner, member):
        assert _views(person) == expected
        assert _views(person, scope="household") == expected


@pytest.mark.django_db
def test_the_combined_projection_counts_the_plan_not_the_members_copy():
    owner, member, household, owner_series, _member_series = _joint_bill()
    _replace(owner, owner_series, household=household)

    def bills(person):
        return {
            (contribution.name, contribution.amount_minor)
            for month in projected_months_for(person.user, today=TODAY)
            for contribution in month.contributions
            if contribution.name not in FIXTURE_ITEMS
        }

    assert bills(owner) == bills(member) == {("Synthetic Electric plan", 250_000)}


@pytest.mark.django_db
def test_a_replaced_bill_linked_through_a_third_members_copy_is_replaced_whole():
    owner, member, household = household_fixture()
    shared = Account.objects.get(name="Synthetic Joint")
    third = make_person("third")
    Membership.objects.create(person=third, household=household)
    owner_series = make_confirmed_series(owner, "Synthetic Electric", shared, months=(5, 6))
    member_series = make_confirmed_series(member, "Synthetic Electric", shared, months=(8, 9))
    # Only the third member's copy shares charges with both: June with the owner's, August with the member's.
    bridge = make_confirmed_series(third, "Synthetic Electric", shared, months=())
    for series, month in ((owner_series, 6), (member_series, 8)):
        stored = series.members.get(transaction__transaction_date=date(2026, month, 20))
        bridge.members.create(transaction=stored.transaction)
    _replace(owner, owner_series, household=household)

    assert _views(member)[0] == ["Synthetic Electric plan"]
    member_series.refresh_from_db()
    assert not set(owner_series.members.values_list("transaction", flat=True)) & set(
        member_series.members.values_list("transaction", flat=True)
    )


@pytest.mark.django_db
def test_two_distinct_bills_from_one_merchant_are_unaffected():
    owner, member, household, owner_series, _member_series = _joint_bill()
    shared = Account.objects.get(name="Synthetic Joint")
    make_confirmed_series(member, "Synthetic Electric", shared, amount_minor=90_000)
    _replace(owner, owner_series, household=household)

    items = [item for item in visible_projection_inputs(member.user) if item.source == "series"]

    assert [(item.name, item.amount_minor) for item in items] == [("Synthetic Electric", 90_000)]


@pytest.mark.django_db
def test_a_private_plan_replacing_the_viewers_own_series_behaves_as_before():
    _owner, member, _household = household_fixture()
    private = make_account(member, name="Member Private Checking")
    own = make_confirmed_series(member, "Synthetic Phone", private, amount_minor=8_000)
    make_confirmed_series(member, "Synthetic Gym", private, amount_minor=5_000)
    _replace(member, own, name="Synthetic Phone plan")

    assert _views(member)[0] == ["Synthetic Gym", "Synthetic Phone plan"]
    assert _views(member, scope="private")[0] == ["Synthetic Gym", "Synthetic Phone plan"]
