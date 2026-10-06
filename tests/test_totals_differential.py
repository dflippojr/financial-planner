"""The SQL totals must match the original per-row Python computation exactly."""
from collections import defaultdict
from datetime import date
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from finance.cash_flow import GROUPING_MONTH, cash_flow_report, iter_period_windows
from finance.category_services import (
    _person_for,
    assign_category,
    income_and_spending_by_account,
    income_and_spending_by_tag,
    income_and_spending_by_window,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    split_transaction,
    spending_by_category_by_window,
)
from finance.models import (
    Account,
    Category,
    RefundLink,
    Tag,
    Transaction,
    TransactionSplit,
    TransactionTag,
    TransferPair,
)
from tests.test_cash_flow import make_account, make_household, make_person, make_transaction


def reference_totals(principal, *, date_from=None, date_to=None, accounts=None, tag=None):
    """The pre-SQL implementation, kept as the oracle."""
    person = _person_for(principal)
    visible_accounts = Account.objects.visible_to(person)
    if accounts is not None:
        visible_accounts = visible_accounts.filter(pk__in=[getattr(item, "pk", item) for item in accounts])
    transactions = Transaction.objects.visible_to(person).filter(
        status=Transaction.Status.ACTIVE,
        kind=Transaction.Kind.CASH_FLOW,
        account_id__in=visible_accounts.values("pk"),
    )
    if tag is not None:
        transactions = transactions.filter(tags=tag).distinct()
    if date_from:
        transactions = transactions.filter(transaction_date__gte=date_from)
    if date_to:
        transactions = transactions.filter(transaction_date__lte=date_to)
    excluded = {
        tx_id
        for pair in TransferPair.objects.excluding_income_and_spending().visible_to(person)
        for tx_id in (pair.leg_a_id, pair.leg_b_id)
    }
    rows = list(transactions)
    refunds = set(
        RefundLink.objects.filter(refund_id__in=[item.pk for item in rows]).values_list("refund_id", flat=True)
    )
    split_ids = [item.pk for item in rows if item.category_source == Transaction.CategorySource.SPLIT]
    splits_by_txn = defaultdict(list)
    for part in TransactionSplit.objects.filter(transaction_id__in=split_ids):
        splits_by_txn[part.transaction_id].append(part)
    income = spending = 0
    spending_by_category = defaultdict(int)
    income_by_category = defaultdict(int)
    for item in rows:
        if item.pk in excluded:
            continue
        if item.pk in refunds:
            spending -= item.amount_minor
            spending_by_category[item.category_id] -= item.amount_minor
            continue
        parts = splits_by_txn.get(item.pk)
        split_parts = item.category_source == Transaction.CategorySource.SPLIT and parts
        if item.amount_minor > 0:
            income += item.amount_minor
            if split_parts:
                for part in parts:
                    income_by_category[part.category_id] += part.amount_minor
            else:
                income_by_category[item.category_id] += item.amount_minor
        elif item.amount_minor < 0:
            spending += -item.amount_minor
            if split_parts:
                for part in parts:
                    spending_by_category[part.category_id] += -part.amount_minor
            else:
                spending_by_category[item.category_id] += -item.amount_minor
    return income, spending, dict(spending_by_category), dict(income_by_category)


def as_tuple(totals):
    return (
        totals.income_minor,
        totals.spending_minor,
        totals.spending_by_category_id,
        totals.income_by_category_id,
    )


@pytest.fixture
def ledger(db):
    owner = make_person("owner")
    partner = make_person("partner")
    household = make_household(owner, partner)
    categories = {c.name: c for c in Category.objects.filter(household=household)}
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    checking = make_account(owner, name="Checking")
    card = make_account(owner, name="Card", account_type=Account.Type.CREDIT_CARD)
    brokerage = make_account(owner, name="Brokerage", account_type=Account.Type.INVESTMENT)
    hidden = make_account(partner, name="Partner private")
    seq = iter(range(1, 1000))

    def txn(account, amount, day, *, kind=Transaction.Kind.CASH_FLOW, category=None, who=owner):
        row = make_transaction(
            who,
            account,
            amount_minor=amount,
            transaction_date=date(2026, 1, day),
            fingerprint=f"{next(seq):064d}",
            kind=kind,
        )
        if category is not None:
            assign_category(who, row.pk, categories[category].pk)
            row.refresh_from_db()
        return row

    txn(checking, 300000, 2, category="Income")
    txn(checking, -4500, 3, category="Groceries")
    txn(checking, -1250, 4)
    txn(shared, -9900, 5, category="Dining")
    txn(shared, 2500, 6, category="Income")
    txn(brokerage, -7777, 7, kind=Transaction.Kind.INVESTMENT_ACTIVITY)
    txn(hidden, -3333, 8, who=partner, category="Groceries")
    # Transfer pair: checking pays the card.
    txn(checking, -20000, 9)
    txn(card, 20000, 9)
    # A purchase with a partial refund.
    purchase = txn(card, -8000, 10, category="Shopping")
    refund = txn(card, 3000, 12)
    # Split spending and split income.
    split_buy = txn(checking, -10000, 13)
    split_pay = txn(checking, 50000, 14)
    # A refund of one part of a split purchase.
    split_card = txn(card, -6000, 15)
    split_refund = txn(card, 1500, 16)
    # Outside the January range.
    make_transaction(
        owner, checking, amount_minor=-999, transaction_date=date(2025, 12, 20), fingerprint=f"{next(seq):064d}"
    )
    refresh_transfer_pairs(owner)
    link_refund(owner, refund.pk, purchase.pk)
    split_transaction(
        owner, split_buy.pk, [(categories["Groceries"].pk, -6000), (categories["Dining"].pk, -4000)]
    )
    split_transaction(
        owner, split_pay.pk, [(categories["Income"].pk, 30000), (categories["Gifts and donations"].pk, 20000)]
    )
    split_transaction(
        owner, split_card.pk, [(categories["Groceries"].pk, -2000), (categories["Shopping"].pk, -4000)]
    )
    link_refund(owner, split_refund.pk, split_card.pk, original_part_id=split_card.splits.first().pk)
    tag = Tag.objects.create(household=household, name="trip")
    for item in (purchase, split_buy, refund):
        TransactionTag.objects.create(transaction=item, tag=tag)
    return SimpleNamespace(owner=owner, partner=partner, checking=checking, shared=shared, tag=tag)


CASES = (
    {},
    {"date_from": date(2026, 1, 1), "date_to": date(2026, 1, 31)},
    {"date_from": date(2026, 1, 9), "date_to": date(2026, 1, 12)},
    {"date_from": date(2026, 1, 13)},
    {"date_to": date(2026, 1, 8)},
)


@pytest.mark.django_db
@pytest.mark.parametrize("window", CASES)
@pytest.mark.parametrize("viewer", ["owner", "partner"])
@pytest.mark.parametrize("filter_name", ["none", "tag", "accounts"])
def test_sql_totals_match_reference(ledger, window, viewer, filter_name):
    principal = getattr(ledger, viewer)
    extra = {}
    if filter_name == "tag":
        extra["tag"] = ledger.tag
    elif filter_name == "accounts":
        extra["accounts"] = [ledger.checking, ledger.shared]
    assert as_tuple(income_and_spending_totals(principal, **window, **extra)) == reference_totals(
        principal, **window, **extra
    )


@pytest.mark.django_db
def test_ledger_exercises_every_branch(ledger):
    income, spending, spending_by_category, income_by_category = reference_totals(ledger.owner)
    assert income > 0 and spending > 0
    assert len(spending_by_category) >= 4 and len(income_by_category) >= 2
    assert income_and_spending_totals(ledger.owner).spending_minor != income_and_spending_totals(
        ledger.partner
    ).spending_minor


@pytest.mark.django_db
@pytest.mark.parametrize("viewer", ["owner", "partner"])
def test_window_totals_match_per_window_reference(ledger, viewer):
    principal = getattr(ledger, viewer)
    windows = [
        (w.start, w.end) for w in iter_period_windows(date(2025, 11, 5), date(2026, 2, 20), GROUPING_MONTH)
    ]
    got = income_and_spending_by_window(principal, windows)
    expected = [reference_totals(principal, date_from=s, date_to=e)[:2] for s, e in windows]
    assert got == expected
    assert income_and_spending_by_window(principal, []) == []


@pytest.mark.django_db
def test_report_query_count_does_not_grow_with_periods(ledger):
    def count(end):
        with CaptureQueriesContext(connection) as queries:
            cash_flow_report(
                ledger.owner,
                date_from=date(2026, 1, 1),
                date_to=end,
                grouping=GROUPING_MONTH,
                today=date(2027, 1, 1),
            )
        return len(queries)

    assert count(date(2026, 12, 31)) == count(date(2026, 3, 31))


@pytest.mark.django_db
@pytest.mark.parametrize("viewer", ["owner", "partner"])
def test_category_windows_match_per_window_reference(ledger, viewer):
    principal = getattr(ledger, viewer)
    windows = [
        (w.start, w.end) for w in iter_period_windows(date(2025, 12, 1), date(2026, 1, 31), GROUPING_MONTH)
    ]
    got = spending_by_category_by_window(principal, windows)
    for (start, end), (total, by_category) in zip(windows, got):
        _, spending, expected, _ = reference_totals(principal, date_from=start, date_to=end)
        assert (total, by_category) == (spending, expected)
    split_windows = [(date(2026, 1, 1), date(2026, 1, 12)), (date(2026, 1, 13), date(2026, 1, 31))]
    for (start, end), (total, by_category) in zip(
        split_windows, spending_by_category_by_window(principal, split_windows)
    ):
        _, spending, expected, _ = reference_totals(principal, date_from=start, date_to=end)
        assert (total, by_category) == (spending, expected)


@pytest.mark.django_db
@pytest.mark.parametrize("viewer", ["owner", "partner"])
def test_account_and_tag_groups_match_reference(ledger, viewer):
    principal = getattr(ledger, viewer)
    accounts = list(Account.objects.visible_to(principal))
    grouped = income_and_spending_by_account(
        principal, accounts, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31)
    )
    for account in accounts:
        income, spending, _, _ = reference_totals(
            principal, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31), accounts=[account]
        )
        assert grouped[account.pk] == (income, spending)
    by_tag = income_and_spending_by_tag(principal, [ledger.tag], date_from=date(2026, 1, 1))
    income, spending, _, _ = reference_totals(principal, date_from=date(2026, 1, 1), tag=ledger.tag)
    assert by_tag[ledger.tag.pk] == (income, spending)
