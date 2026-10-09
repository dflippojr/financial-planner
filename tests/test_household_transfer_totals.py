"""Household-scope totals judge a transfer pair by its household legs, so every member gets one answer (#297)."""

from datetime import date
from hashlib import sha256

import pytest

from finance.cash_flow import cash_flow_report, selected_accounts
from finance.category_services import income_and_spending_totals, refresh_transfer_pairs
from finance.models import Account, RecurringSeries, RecurringSeriesMember, TransferPair
from finance.planning_services import visible_projection_inputs
from finance.savings_goal_plan import build_funding_plan
from tests.test_categorization import make_account, make_household, make_person, make_transaction
from tests.test_savings_goal_plan import TODAY, make_goal, monthly_surplus, summary

pytestmark = pytest.mark.django_db

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def _cross_scope_pair(*, private_leg=True):
    """Owner moves 500.00 out of a household account into their own private account."""
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    if private_leg:
        other = make_account(owner, name="Owner Private")
    else:
        other = make_account(owner, name="Shared Savings", scope=Account.Scope.HOUSEHOLD, household=household)
    out_leg = make_transaction(
        owner, shared, transaction_date=date(2026, 8, 10), amount_minor=-50_000, description="Synthetic transfer out"
    )
    make_transaction(
        owner, other, transaction_date=date(2026, 8, 10), amount_minor=50_000, description="Synthetic transfer in"
    )
    refresh_transfer_pairs(owner)
    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    return owner, member, household, out_leg


def _totals(person, scope):
    accounts = selected_accounts(person, scope=scope, cash_flow_only=True)
    totals = income_and_spending_totals(person, date_from=AUGUST[0], date_to=AUGUST[1], accounts=accounts)
    return totals.income_minor, totals.spending_minor


def _report(person, scope):
    report = cash_flow_report(person, date_from=AUGUST[0], date_to=AUGUST[1], scope=scope, today=TODAY)
    return [(period.income_minor, period.spending_minor) for period in report.periods]


def test_household_totals_count_the_household_leg_for_every_member():
    owner, member, _household, _out_leg = _cross_scope_pair()

    assert _totals(owner, "household") == _totals(member, "household") == (0, 50_000)
    assert _report(owner, "household") == _report(member, "household") == [(0, 50_000)]


def test_private_and_combined_views_keep_excluding_a_pair_the_viewer_fully_sees():
    owner, member, _household, _out_leg = _cross_scope_pair()

    assert _totals(owner, "private") == (0, 0)
    assert _totals(owner, "") == (0, 0)
    # The member sees only the household leg, as before.
    assert _totals(member, "") == (0, 50_000)


def test_household_pair_stays_excluded_for_every_member():
    owner, member, _household, _out_leg = _cross_scope_pair(private_leg=False)

    assert _totals(owner, "household") == _totals(member, "household") == (0, 0)
    assert _totals(owner, "") == _totals(member, "") == (0, 0)


def test_household_funding_plan_surplus_and_buffer_match_for_every_member():
    owner, member, household, _out_leg = _cross_scope_pair()
    monthly_surplus(owner, 400_000, 100_000, household=household)
    make_goal(owner, "Shared sofa", 450_000, priority=1, scope="household", household=household)

    owner_plan = build_funding_plan(owner.user, today=TODAY, scope="household")
    member_plan = build_funding_plan(member.user, today=TODAY, scope="household")

    assert summary(owner_plan) == summary(member_plan)
    assert owner_plan.buffer_minor == 50_000 // 3


def test_household_projection_keeps_a_series_on_the_household_leg():
    owner, _member, _household, out_leg = _cross_scope_pair()
    series = RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic transfer",
        display_name="Synthetic transfer",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-50_000,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=sha256(b"synthetic-transfer-series").hexdigest(),
    )
    RecurringSeriesMember.objects.create(series=series, transaction=out_leg, source="manual")

    def names(**kwargs):
        return [item.name for item in visible_projection_inputs(owner, **kwargs)]

    assert names(scope="household") == ["Synthetic transfer"]
    assert names() == []
