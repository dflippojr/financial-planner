"""Budgets page: category-budgeted spending against the month's total (#725).

Synthetic data only. The total must be the Spending page's own figure, and the
gap is everything outside every category budget; an overall budget never
covers it (owner decision on #725).
"""

from datetime import date

import pytest
from django.urls import reverse

from finance.budget_services import budget_coverage
from finance.cash_flow import spending_by_category_report
from finance.category_services import assign_category, confirm_transfer_pair, link_refund, refresh_transfer_pairs
from finance.models import Account, Budget, TransferPair
from finance.untracked_transfers import mark_untracked_transfer
from tests.test_budgets import (
    add_budget,
    make_account,
    make_household,
    make_person,
    make_transaction,
    signed_in,
)


MONTH = date(2026, 1, 1)
MONTH_END = date(2026, 1, 31)


def _fingerprint(char):
    return char * 64


def _ledger():
    """Owner with private and household accounts; refunds, a transfer and an untracked transfer."""
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Private Checking")
    shared = make_account(owner, name="Shared Card", scope=Account.Scope.HOUSEHOLD, household=household)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    transportation = household.categories.get(name="Transportation")
    rows = [
        (private, -5000, groceries, "1"),
        (private, 700, groceries, "2"),  # a refund, linked to row 1 below
        (private, -2500, dining, "3"),
        (shared, -4000, groceries, "4"),
        (shared, -1800, transportation, "5"),
        (shared, -900, None, "6"),  # uncategorized
        (private, -30_000, None, "7"),  # transfer out, paired below
        (shared, 30_000, None, "9"),  # transfer in
    ]
    made = {}
    for account, amount, category, char in rows:
        txn = make_transaction(
            owner, account, amount_minor=amount, description=f"Synthetic row {char}", fingerprint=_fingerprint(char)
        )
        made[char] = txn
        if category is not None:
            assign_category(owner, txn.pk, category.pk)
    link_refund(owner, made["2"].pk, made["1"].pk)
    untracked = make_transaction(
        owner, private, amount_minor=-12_000, description="Synthetic untracked move", fingerprint=_fingerprint("8")
    )
    mark_untracked_transfer(owner, untracked.pk)
    refresh_transfer_pairs(owner)
    (pair,) = TransferPair.objects.filter(status=TransferPair.Status.SUGGESTED)
    confirm_transfer_pair(owner, pair.pk)
    return owner, member, household, groceries, dining, transportation


def _spending_total(principal, scope=""):
    return spending_by_category_report(principal, date_from=MONTH, date_to=MONTH_END, scope=scope).total_spending_minor


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["", Account.Scope.PRIVATE, Account.Scope.HOUSEHOLD])
def test_coverage_total_matches_spending_report(scope):
    owner, member, _household, groceries, _dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    for principal in (owner, member):
        coverage = budget_coverage(principal.user, MONTH, scope=scope)
        assert coverage.total_spending_minor == _spending_total(principal, scope)
        assert coverage.budgeted_minor + coverage.gap_minor == coverage.total_spending_minor
        assert sum(row.spending_minor for row in coverage.gap_rows) == coverage.gap_minor


@pytest.mark.django_db
def test_refunds_transfers_and_untracked_transfers_follow_spending():
    owner, _member, _household, groceries, _dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    coverage = budget_coverage(owner.user, MONTH)
    # 5000 - 700 + 2500 + 4000 + 1800 + 900; the transfer and untracked move are not spending.
    assert coverage.total_spending_minor == 13_500
    assert coverage.budgeted_minor == 5000 - 700 + 4000
    assert {row.name: row.spending_minor for row in coverage.gap_rows} == {
        "Dining": 2500,
        "Transportation": 1800,
        "Uncategorized": 900,
    }


@pytest.mark.django_db
def test_gap_category_leaves_the_gap_once_it_has_a_category_budget():
    owner, _member, _household, groceries, dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    before = budget_coverage(owner.user, MONTH)
    assert "Dining" in {row.name for row in before.gap_rows}

    add_budget(owner, category=dining, amount_minor=5000)
    after = budget_coverage(owner.user, MONTH)
    assert "Dining" not in {row.name for row in after.gap_rows}
    assert after.gap_minor == before.gap_minor - 2500
    assert after.budgeted_minor == before.budgeted_minor + 2500
    assert after.total_spending_minor == before.total_spending_minor


@pytest.mark.django_db
def test_overall_budget_does_not_shrink_the_gap():
    owner, _member, household, groceries, _dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    before = budget_coverage(owner.user, MONTH)

    add_budget(owner, category=None, amount_minor=1_000_000)
    add_budget(owner, category=None, amount_minor=1_000_000, scope=Budget.Scope.HOUSEHOLD, household=household)
    after = budget_coverage(owner.user, MONTH)
    assert after.gap_minor == before.gap_minor
    assert after.budgeted_minor == before.budgeted_minor
    assert [(row.name, row.spending_minor) for row in after.gap_rows] == [
        (row.name, row.spending_minor) for row in before.gap_rows
    ]


@pytest.mark.django_db
def test_no_category_budgets_puts_all_spending_in_the_gap():
    owner, _member, _household, _groceries, _dining, _transportation = _ledger()
    coverage = budget_coverage(owner.user, MONTH)
    assert coverage.budgeted_minor == 0
    assert coverage.gap_minor == coverage.total_spending_minor == 13_500


@pytest.mark.django_db
def test_household_budget_covers_only_household_account_spending():
    owner, _member, household, groceries, _dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000, scope=Budget.Scope.HOUSEHOLD, household=household)

    everything = budget_coverage(owner.user, MONTH)
    # The household budget measures household accounts only, so private groceries stay outside it.
    assert everything.budgeted_minor == 4000
    assert {row.name: row.spending_minor for row in everything.gap_rows}["Groceries"] == 5000 - 700

    shared_only = budget_coverage(owner.user, MONTH, scope=Account.Scope.HOUSEHOLD)
    assert shared_only.budgeted_minor == 4000
    assert "Groceries" not in {row.name for row in shared_only.gap_rows}

    private_only = budget_coverage(owner.user, MONTH, scope=Account.Scope.PRIVATE)
    assert private_only.budgeted_minor == 0


@pytest.mark.django_db
def test_gap_rows_link_to_transactions_filtered_by_category_month_and_scope():
    owner, _member, _household, groceries, dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    coverage = budget_coverage(owner.user, MONTH, scope=Account.Scope.HOUSEHOLD)
    rows = {row.name: row for row in coverage.gap_rows}
    assert set(rows) == {"Transportation", "Uncategorized"}
    url = rows["Uncategorized"].drilldown_url
    assert url.startswith(reverse("transaction-list") + "?")
    for part in ("date_from=2026-01-01", "date_to=2026-01-31", "scope=household", "category=uncategorized"):
        assert part in url
    assert f"category={dining.pk}" not in rows["Transportation"].drilldown_url


def _coverage_section(body):
    start = body.index('aria-label="Spending outside category budgets"')
    return body[start:body.index("</section>", start)]


@pytest.mark.django_db
def test_budgets_page_shows_budgeted_total_and_gap_for_month_and_scope():
    owner, _member, _household, groceries, _dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    client = signed_in(owner)

    section = _coverage_section(client.get(reverse("budgets") + "?month=2026-01").content.decode())
    assert "\u2212$83.00" in section  # budgeted: 50.00 - 7.00 + 40.00
    assert "\u2212$135.00" in section  # total, the Spending page figure
    assert "\u2212$52.00" in section  # gap: 25 + 18 + 9
    assert "category=uncategorized" in section
    assert ">Dining</a>" in section
    assert ">Groceries</a>" not in section

    household = client.get(reverse("budgets") + "?month=2026-01&scope=household").content.decode()
    section = _coverage_section(household)
    assert "\u2212$67.00" in section  # total on household accounts: 40 + 18 + 9
    assert "\u2212$27.00" in section  # gap on household accounts: 18 + 9
    assert ">Dining</a>" not in section
    assert 'href="/planning/budgets/?month=2026-01&amp;scope=private"' in section
    # The month arrows keep the chosen scope.
    assert 'href="/planning/budgets/?month=2026-02&amp;scope=household"' in household


@pytest.mark.django_db
def test_spending_page_shows_the_same_total():
    owner, _member, _household, groceries, _dining, _transportation = _ledger()
    add_budget(owner, category=groceries, amount_minor=10_000)
    spending = signed_in(owner).get(
        reverse("spending-by-category") + "?date_from=2026-01-01&date_to=2026-01-31&grouping=month"
    )
    assert spending.context["report"].total_spending_minor == budget_coverage(owner.user, MONTH).total_spending_minor


@pytest.mark.django_db
def test_member_page_never_counts_another_members_private_spending():
    owner, member, household, groceries, _dining, _transportation = _ledger()
    add_budget(member, category=groceries, amount_minor=10_000, scope=Budget.Scope.HOUSEHOLD, household=household)
    body = signed_in(member).get(reverse("budgets") + "?month=2026-01").content.decode()
    coverage = budget_coverage(member.user, MONTH)
    assert coverage.total_spending_minor == 4000 + 1800 + 900
    assert "Dining" not in {row.name for row in coverage.gap_rows}
    section = _coverage_section(body)
    assert "$25.00" not in section
    assert "$135.00" not in section
    assert "\u2212$67.00" in section
