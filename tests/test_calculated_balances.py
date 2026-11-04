"""Past month-end balances calculated from transactions (#636). Synthetic data only."""

from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from finance.audit_services import events_for
from finance.calculated_balances import (
    NO_ANCHOR,
    NO_TRANSACTIONS,
    NOT_ELIGIBLE,
    CalculationError,
    calculate,
    delete_calculated_balances,
    save_calculated_balances,
)
from finance.models import Account, AuditEvent, BalanceSnapshot, Category, Household, ImportBatch, TransactionSplit
from finance.net_worth import contribution_parts, net_worth_chart_data, net_worth_report
from finance.reconciliation import eligible_accounts, reconcile
from finance.savings_goal_services import _current_amount
from tests.test_alerts import make_account, make_household, make_person, signed_in
from tests.test_reconciliation import add_batch, add_rows, snapshot

pytestmark = pytest.mark.django_db

SIMPLEFIN = BalanceSnapshot.Source.SIMPLEFIN
MANUAL = BalanceSnapshot.Source.MANUAL
CALCULATED = BalanceSnapshot.Source.CALCULATED
OCT_31 = date(2026, 10, 31)


def calculated(account):
    return list(
        BalanceSnapshot.objects.filter(account=account, source=CALCULATED)
        .order_by("snapshot_date")
        .values_list("snapshot_date", "amount_minor")
    )


def real_rows(account):
    return list(
        BalanceSnapshot.objects.filter(account=account)
        .exclude(source=CALCULATED)
        .order_by("pk")
        .values_list("pk", "snapshot_date", "source", "amount_minor", "note")
    )


def checking_with_history(owner, **options):
    """A checking account: transactions from May 20, a June with none, one SimpleFIN balance on Oct 31.

    May 31 and Jun 30: 423,000. Jul 31: 523,000 (pay on Jul 10). Aug 31: 503,000.
    Sep 30: 498,000. Oct 31: 500,000 (the anchor).
    """
    account = make_account(owner, **options)
    may = add_batch(account, date(2026, 5, 1), date(2026, 5, 31))
    add_rows(may, (date(2026, 5, 20), 50_000))
    later = add_batch(account, date(2026, 6, 1), OCT_31)
    add_rows(
        later,
        (date(2026, 7, 10), 100_000),  # pay
        (date(2026, 8, 5), -20_000),
        (date(2026, 9, 15), -5_000),  # split below: counts once, at the parent amount
        (date(2026, 10, 2), -1_000),
        (date(2026, 10, 20), 3_000),
    )
    parent = account.transactions.get(transaction_date=date(2026, 9, 15))
    household = Household.objects.filter(memberships__person=owner).first() or make_household(owner)
    category = Category.objects.filter(household=household).first()
    TransactionSplit.objects.create(transaction=parent, category=category, amount_minor=-2_000, position=0)
    TransactionSplit.objects.create(transaction=parent, category=category, amount_minor=-3_000, position=1)
    # Investment activity is a kind too; reconciliation counts every kind, and so does this.
    account.transactions.filter(transaction_date=date(2026, 10, 2)).update(kind="investment_activity")
    snapshot(account, OCT_31, 500_000)
    return account


CHECKING_EXPECTED = [
    (date(2026, 5, 31), 423_000),
    (date(2026, 6, 30), 423_000),
    (date(2026, 7, 31), 523_000),
    (date(2026, 8, 31), 503_000),
    (date(2026, 9, 30), 498_000),
]


def card_with_history(owner, *, anchor_source=SIMPLEFIN):
    """Purchases, payments and a refund, then one balance on Oct 31: 300.00 owed."""
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    batch = add_batch(card, date(2026, 8, 1), OCT_31, source=ImportBatch.Source.CAPITAL_ONE)
    add_rows(
        batch,
        (date(2026, 8, 3), -10_000),  # purchase
        (date(2026, 8, 20), -5_000),  # purchase
        (date(2026, 9, 1), 15_000),  # payment
        (date(2026, 9, 10), -40_000),  # purchase
        (date(2026, 9, 12), 4_000),  # refund
        (date(2026, 10, 5), 6_000),  # payment
    )
    # SimpleFIN keeps the protocol sign (owed is negative); a manual entry is the positive amount owed.
    snapshot(card, OCT_31, -30_000 if anchor_source == SIMPLEFIN else 30_000, source=anchor_source)
    return card


# Stored in the SimpleFIN protocol sign whatever the anchor's source: owed is negative.
CARD_EXPECTED = [(date(2026, 8, 31), -15_000), (date(2026, 9, 30), -36_000)]


def test_checking_back_calculation_with_quiet_months_and_a_split():
    account = checking_with_history(make_person("owner"))

    result = calculate(account)

    assert [(row.month_end, row.balance_minor) for row in result.rows] == CHECKING_EXPECTED
    assert result.anchor_date == OCT_31 and result.anchor_source == SIMPLEFIN


def test_credit_card_back_calculation_keeps_the_simplefin_sign():
    owner = make_person("owner")
    card = card_with_history(owner)

    save_calculated_balances(owner, card.pk)

    assert calculated(card) == CARD_EXPECTED
    # Net worth reads them as amounts owed, like the SimpleFIN rows they extend.
    assert contribution_parts(card.account_type, CALCULATED, -15_000) == (0, 15_000)


def test_manual_card_anchor_is_converted_to_transaction_sign():
    owner = make_person("owner")
    card = card_with_history(owner, anchor_source=MANUAL)

    save_calculated_balances(owner, card.pk)

    assert calculated(card) == CARD_EXPECTED


def test_incomplete_coverage_flags_every_month_end_whose_window_has_a_gap():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, date(2026, 5, 1), date(2026, 5, 31)), (date(2026, 5, 20), 50_000))
    # Nothing covers June, which only May 31's window (Jun 1 to Oct 31) includes.
    add_rows(add_batch(account, date(2026, 7, 1), OCT_31), (date(2026, 7, 10), 100_000))
    snapshot(account, OCT_31, 500_000)

    flags = {row.month_end: row.incomplete for row in calculate(account).rows}

    assert flags == {
        date(2026, 5, 31): True,
        date(2026, 6, 30): False,  # its window starts on Jul 1
        date(2026, 7, 31): False,
        date(2026, 8, 31): False,
        date(2026, 9, 30): False,
    }


def test_manual_batches_do_not_count_as_coverage():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, date(2026, 9, 1), date(2026, 9, 1), source=ImportBatch.Source.MANUAL),
             (date(2026, 9, 1), -1_000))
    snapshot(account, OCT_31, 10_000)

    assert [row.incomplete for row in calculate(account).rows] == [True]


def test_the_earliest_real_balance_is_the_anchor_and_later_ones_are_untouched():
    owner = make_person("owner")
    account = checking_with_history(owner)
    snapshot(account, date(2026, 11, 2), 1, source=MANUAL)
    before = real_rows(account)

    save_calculated_balances(owner, account.pk)

    assert calculated(account) == CHECKING_EXPECTED
    assert real_rows(account) == before


def test_a_calculated_row_never_anchors_a_calculation():
    owner = make_person("owner")
    account = checking_with_history(owner)
    save_calculated_balances(owner, account.pk)
    BalanceSnapshot.objects.filter(account=account).exclude(source=CALCULATED).delete()

    with pytest.raises(CalculationError) as raised:
        calculate(account)

    assert str(raised.value) == NO_ANCHOR


def test_a_simplefin_or_manual_row_on_the_same_date_wins_over_a_calculated_one():
    owner = make_person("owner")
    account = checking_with_history(owner)
    snapshot(account, date(2026, 8, 31), 7_777, source=MANUAL)
    # Saved after the manual row, so a higher pk: the rank, not the pk, must decide.
    BalanceSnapshot.objects.create(account=account, snapshot_date=date(2026, 8, 31), amount_minor=1, source=CALCULATED)

    august = net_worth_report(owner, date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)).periods[0]
    goal_amount = _goal_amount(owner, account, date(2026, 8, 31))

    assert [(row.source, row.amount_minor) for row in august.accounts] == [(MANUAL, 7_777)]
    assert goal_amount == 7_777
    snapshot(account, date(2026, 8, 31), 8_888)
    august = net_worth_report(owner, date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)).periods[0]
    assert [(row.source, row.amount_minor) for row in august.accounts] == [(SIMPLEFIN, 8_888)]


def _goal_amount(owner, account, as_of):
    from finance.models import SavingsGoal

    goal = SavingsGoal.objects.create(owner=owner, name="Synthetic goal", target_amount_minor=1, linked_account=account)
    return _current_amount(owner, goal, as_of=as_of)[0]


def test_saving_again_replaces_only_calculated_rows():
    owner = make_person("owner")
    account = checking_with_history(owner)
    first = save_calculated_balances(owner, account.pk)
    real = real_rows(account)
    kept_pk = BalanceSnapshot.objects.get(account=account, source=CALCULATED, snapshot_date=date(2026, 9, 30)).pk
    # The May row's import is undone: May and June drop out, July through August change.
    undone_at = timezone.now()
    ImportBatch.objects.filter(account=account, date_range_start=date(2026, 5, 1)).update(
        status=ImportBatch.Status.ARCHIVED, archived_at=undone_at
    )
    account.transactions.filter(transaction_date=date(2026, 5, 20)).update(status="archived", archived_at=undone_at)
    account.transactions.filter(transaction_date=date(2026, 8, 5)).update(amount_minor=-30_000)

    second = save_calculated_balances(owner, account.pk)

    assert (first.created, first.updated, first.deleted) == (5, 0, 0)
    assert (second.saved, second.created, second.updated, second.deleted) == (3, 0, 1, 2)
    assert calculated(account) == [(date(2026, 7, 31), 533_000), (date(2026, 8, 31), 503_000),
                                   (date(2026, 9, 30), 498_000)]
    assert BalanceSnapshot.objects.get(account=account, source=CALCULATED, snapshot_date=date(2026, 9, 30)).pk == kept_pk
    assert real_rows(account) == real


def test_saving_and_deleting_write_balance_audit_events_without_amounts():
    owner = make_person("owner")
    card = card_with_history(owner)

    save_calculated_balances(owner, card.pk)
    save_calculated_balances(owner, card.pk)  # unchanged: records nothing
    created = list(events_for(owner).filter(action=AuditEvent.Action.RECORD_CREATED))
    deleted_count = delete_calculated_balances(owner, card.pk)
    deleted = list(events_for(owner).filter(action=AuditEvent.Action.RECORD_DELETED))

    assert len(created) == 2 and all(row.target_type == AuditEvent.TargetType.BALANCE for row in created)
    assert deleted_count == 2 and len(deleted) == 2
    assert {row.target_id for row in created} == {row.target_id for row in deleted}
    assert all(row.metadata == {} for row in created + deleted)


def test_balances_page_saves_lists_and_deletes_calculated_balances():
    owner = make_person("owner")
    account = checking_with_history(owner)
    snapshot(account, date(2026, 11, 1), 1, source=MANUAL)
    real = real_rows(account)
    client = signed_in(owner)

    page = client.get(reverse("account-balances", args=[account.pk])).content.decode()
    preview = client.get(reverse("account-balances-calculate", args=[account.pk]))
    assert BalanceSnapshot.objects.filter(account=account, source=CALCULATED).count() == 0
    saved = client.post(reverse("account-balances-calculate", args=[account.pk]), follow=True).content.decode()
    deleted = client.post(reverse("account-calculated-delete", args=[account.pk]), follow=True).content.decode()

    assert "Calculate past balances" in page and "Delete calculated balances" not in page
    preview_text = preview.content.decode()
    assert preview.status_code == 200 and "Save calculated balances" in preview_text
    assert "2026-05-31" in preview_text and "$4,230.00" in preview_text and "$5,230.00" in preview_text
    assert "Saved 5 calculated balances." in saved and "Delete calculated balances" in saved
    assert saved.count(">calculated</span>") == 5
    assert "Deleted 5 calculated balances." in deleted
    assert calculated(account) == [] and real_rows(account) == real


def test_preview_explains_why_nothing_can_be_calculated():
    owner = make_person("owner")
    no_rows = make_account(owner, name="Synthetic Empty")
    snapshot(no_rows, OCT_31, 1)
    no_anchor = make_account(owner, name="Synthetic Unanchored")
    add_rows(add_batch(no_anchor, date(2026, 9, 1), OCT_31), (date(2026, 9, 5), -1_000))
    client = signed_in(owner)

    empty = client.get(reverse("account-balances-calculate", args=[no_rows.pk])).content.decode()
    unanchored = client.get(reverse("account-balances-calculate", args=[no_anchor.pk])).content.decode()
    post = client.post(reverse("account-balances-calculate", args=[no_anchor.pk]), follow=True).content.decode()

    assert NO_TRANSACTIONS in empty and "Save calculated balances" not in empty
    assert NO_ANCHOR in unanchored and "Save calculated balances" not in unanchored
    assert "Record a balance first" in post
    assert BalanceSnapshot.objects.filter(source=CALCULATED).count() == 0


def test_investment_accounts_are_refused():
    owner = make_person("owner")
    investment = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    add_rows(add_batch(investment, date(2026, 9, 1), OCT_31), (date(2026, 9, 5), -1_000))
    snapshot(investment, OCT_31, 1_000_000, source=MANUAL)
    client = signed_in(owner)

    page = client.get(reverse("account-balances", args=[investment.pk])).content.decode()
    get = client.get(reverse("account-balances-calculate", args=[investment.pk]))
    post = client.post(reverse("account-balances-calculate", args=[investment.pk]))

    assert "Calculate past balances" not in page
    assert get.status_code == post.status_code == 404
    with pytest.raises(CalculationError) as raised:
        save_calculated_balances(owner, investment.pk)
    assert str(raised.value) == NOT_ELIGIBLE
    assert BalanceSnapshot.objects.filter(source=CALCULATED).count() == 0


@pytest.mark.parametrize("account_type", [Account.Type.LOAN, Account.Type.REAL_ESTATE])
def test_loans_and_physical_assets_are_refused(account_type):
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Other", account_type=account_type)

    response = signed_in(owner).get(reverse("account-balances-calculate", args=[account.pk]))

    assert response.status_code == 404


def test_another_members_private_account_is_a_404_and_unchanged():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = checking_with_history(owner, name="Owner Secret Vault")
    save_calculated_balances(owner, private.pk)
    before = calculated(private)
    client = signed_in(member)
    missing = private.pk + 999

    responses = [
        client.get(reverse("account-balances-calculate", args=[private.pk])),
        client.post(reverse("account-balances-calculate", args=[private.pk])),
        client.post(reverse("account-calculated-delete", args=[private.pk])),
    ]
    missing_response = client.get(reverse("account-balances-calculate", args=[missing]))

    for response in responses:
        assert response.status_code == 404
        assert response.content == missing_response.content
    assert calculated(private) == before


def test_a_household_member_can_calculate_a_shared_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = checking_with_history(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)

    response = signed_in(member).post(reverse("account-balances-calculate", args=[shared.pk]))

    assert response.status_code == 302
    assert calculated(shared) == CHECKING_EXPECTED


def test_reconciliation_ignores_calculated_rows():
    owner = make_person("owner")
    single = checking_with_history(owner, name="Synthetic Single")  # one real balance: not reconciled
    double = checking_with_history(owner, name="Synthetic Double")
    snapshot(double, date(2026, 7, 31), 523_000)
    before_eligible = list(eligible_accounts().values_list("pk", flat=True))
    before = reconcile(eligible_accounts())

    save_calculated_balances(owner, single.pk)
    save_calculated_balances(owner, double.pk)

    assert BalanceSnapshot.objects.filter(source=CALCULATED).count() == 5 + 2
    assert list(eligible_accounts().values_list("pk", flat=True)) == before_eligible == [double.pk]
    assert reconcile(eligible_accounts()) == before


def test_net_worth_monthly_series_uses_calculated_rows_with_a_badge():
    owner = make_person("owner")
    account = checking_with_history(owner)
    card = card_with_history(owner)
    save_calculated_balances(owner, account.pk)
    save_calculated_balances(owner, card.pk)

    report = net_worth_report(owner, date_from=date(2026, 5, 1), date_to=OCT_31, today=date(2026, 11, 3))
    chart = net_worth_chart_data(report)
    page = signed_in(owner).get(reverse("net-worth"), {"date_from": "2026-05-01", "date_to": "2026-10-31"})
    content = page.content.decode()

    assert [period.net_minor for period in report.periods] == [
        423_000, 423_000, 523_000, 503_000 - 15_000, 498_000 - 36_000, 500_000 - 30_000,
    ]
    assert [period.omitted_untracked for period in report.periods] == [1, 1, 1, 0, 0, 0]
    assert [period.calculated for period in report.periods] == [1, 1, 1, 2, 2, 0]
    assert chart["periods"][3]["calculated"] == 2
    assert {row["source_badge"] for row in chart["periods"][3]["accounts"]} == {CALCULATED}
    assert all(row["is_calculated"] for row in chart["periods"][3]["accounts"])
    assert not any(row["is_calculated"] for row in chart["periods"][5]["accounts"])
    assert page.status_code == 200
    assert "2 calculated" in content and 'badge-sm">calculated</span>' in content


def test_calculated_rows_carry_forward_like_any_other_balance():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, date(2026, 5, 1), OCT_31), (date(2026, 5, 20), 50_000))
    snapshot(account, OCT_31, 50_000)
    save_calculated_balances(owner, account.pk)

    report = net_worth_report(owner, date_from=date(2026, 6, 1), date_to=date(2026, 6, 30), today=date(2026, 11, 3))
    row = report.periods[0].accounts[0]

    # Every month-end got its own row, so June is not carried forward.
    assert (row.snapshot_date, row.carried_forward, row.is_calculated) == (date(2026, 6, 30), False, True)
    BalanceSnapshot.objects.filter(account=account, snapshot_date=date(2026, 6, 30)).delete()
    report = net_worth_report(owner, date_from=date(2026, 6, 1), date_to=date(2026, 6, 30), today=date(2026, 11, 3))
    row = report.periods[0].accounts[0]
    assert (row.snapshot_date, row.carried_forward, row.source_badge) == (date(2026, 5, 31), True, "carried forward")


def test_the_check_constraint_accepts_calculated_and_still_rejects_unknown_sources():
    account = make_account(make_person("owner"))
    BalanceSnapshot.objects.create(account=account, snapshot_date=OCT_31, amount_minor=1, source=CALCULATED)

    with pytest.raises(IntegrityError), transaction.atomic():
        BalanceSnapshot.objects.create(account=account, snapshot_date=OCT_31, amount_minor=1, source="estimated")


@pytest.mark.django_db(transaction=True)
def test_rolling_back_0078_drops_calculated_rows_and_keeps_the_rest():
    account = make_account(make_person("owner"))
    real = snapshot(account, OCT_31, 1_000)
    BalanceSnapshot.objects.create(account=account, snapshot_date=date(2026, 9, 30), amount_minor=900, source=CALCULATED)
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("finance", "0077_refund_link_drop")])
        old = executor.loader.project_state([("finance", "0077_refund_link_drop")]).apps
        rows = list(old.get_model("finance", "BalanceSnapshot").objects.values_list("pk", "source"))
        assert rows == [(real.pk, SIMPLEFIN)]
    finally:
        MigrationExecutor(connection).migrate(head)
