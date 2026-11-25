"""Transfers to or from an account the planner doesn't track (#642).

A row marked as an untracked transfer is left out of income and spending
through the one exclusion path every report uses, for every viewer of the row.
These tests drive the services and pages with synthetic rows only.
"""

import csv
import io
import json
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance import ai_tools
from finance.bulk_edit_services import (
    ACTION_MARK_UNTRACKED,
    ACTION_UNMARK_UNTRACKED,
    SKIP_INVESTMENT,
    SKIP_INVESTMENT_ACCOUNT,
    SKIP_LINKED_REFUND,
    SKIP_TRANSFER,
    apply_bulk_edit,
    preview_bulk_edit,
    undo_bulk_edit,
)
from finance.budget_services import progress_snapshot
from finance.cash_flow import cash_flow_report, income_by_category_report, selected_accounts, spending_by_category_report
from finance.category_services import assign_category, assignable_categories
from finance.lifecycle_services import unshare_account
from finance.ledger_counting import excluded_transfer_ids, income_and_spending_totals
from finance.refund_links import link_refund
from finance.transfer_pairing import (
    confirm_transfer_pair,
    dismiss_transfer_pair,
    refresh_transfer_pairs,
    undo_transfer_pair,
)
from finance.export import collect_export_tables
from finance.models import (
    Account,
    AuditEvent,
    Budget,
    Category,
    CategoryRule,
    RefundLink,
    RefundLinkDrop,
    RuleApplicationEntry,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)
from finance.monthly_review import compute_monthly_review_facts, review_for_viewer
from finance.reauth import RECENT_AUTH_SESSION_KEY
from finance.rule_services import apply_rule, preview_rule, reverse_application, save_category_rule, set_rule_enabled
from finance.spending_trends import spending_category_trend_report
from finance.splits import split_transaction
from finance.untracked_transfers import (
    ALREADY_EXCLUDED,
    INVESTMENT_REFUSED,
    LINKED_REFUND_REFUSED,
    mark_untracked_transfer,
    unmark_untracked_transfer,
)
from finance.year_end import year_end_report
from tests.test_budgets import add_budget
from tests.test_categorization import make_account, make_household, make_person, make_transaction
from tests.test_category_rules import import_csv

pytestmark = pytest.mark.django_db

AUGUST = date(2026, 8, 12)
MONTH = (date(2026, 8, 1), date(2026, 8, 31))
TODAY = date(2026, 10, 15)
BROKERAGE_BUY = -100_000
SAVINGS_WITHDRAWAL = 40_000


@pytest.fixture
def world():
    """Owner and member share a household checking account; the owner also has private checking."""
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Owner Private")
    investment = make_account(owner, name="Owner Brokerage", account_type=Account.Type.INVESTMENT)
    shopping = Category.objects.get(household=household, name="Shopping")
    income = Category.objects.get(household=household, name="Income")

    def txn(account, amount_minor, description):
        return make_transaction(owner, account, transaction_date=AUGUST, amount_minor=amount_minor, description=description)

    buy = txn(shared, BROKERAGE_BUY, "SYNTHETIC BROKERAGE BUY")
    withdrawal = txn(shared, SAVINGS_WITHDRAWAL, "SYNTHETIC SAVINGS WITHDRAWAL")
    grocer = txn(shared, -3_000, "SYNTHETIC GROCER")
    payroll = txn(shared, 200_000, "SYNTHETIC PAYROLL")
    assign_category(owner, buy.pk, shopping.pk)
    assign_category(owner, withdrawal.pk, income.pk)
    assign_category(owner, payroll.pk, income.pk)
    return {
        "owner": owner, "member": member, "household": household, "shared": shared, "private": private,
        "investment": investment, "shopping": shopping, "income": income, "buy": buy,
        "withdrawal": withdrawal, "grocer": grocer, "payroll": payroll, "txn": txn,
    }


def _totals(person, scope=""):
    accounts = selected_accounts(person, scope=scope, cash_flow_only=True)
    totals = income_and_spending_totals(person, date_from=MONTH[0], date_to=MONTH[1], accounts=accounts)
    return totals.income_minor, totals.spending_minor


def _views(person, budget):
    """Income and spending as each report sees August, for one viewer."""
    spending = spending_by_category_report(person, date_from=MONTH[0], date_to=MONTH[1])
    income = income_by_category_report(person, date_from=MONTH[0], date_to=MONTH[1])
    cash = cash_flow_report(person, date_from=MONTH[0], date_to=MONTH[1], today=TODAY)
    review = compute_monthly_review_facts(person, MONTH[0], today=TODAY)
    year_end = year_end_report(person, year=2026, today=TODAY)
    trend = spending_category_trend_report(person, date_from=MONTH[0], date_to=MONTH[1], today=TODAY)
    return {
        "totals": _totals(person),
        "household": _totals(person, Account.Scope.HOUSEHOLD),
        "cash_flow": [(period.income_minor, period.spending_minor) for period in cash.periods],
        "spending_by_category": {row.name: row.spending_minor for row in spending.rows if row.spending_minor},
        "spending_total": spending.total_spending_minor,
        "income_total": income.total_income_minor,
        "budget_spent": progress_snapshot(budget, MONTH[0], person).spent_minor,
        "review": (review["income_minor"], review["spending_minor"]),
        "review_large": sorted(item["description"] for item in review["large_transactions"]),
        "year_end": (year_end.cash_flow.summary.income_minor, year_end.cash_flow.summary.spending_minor),
        "trend": [period.total_spending_minor for period in trend.periods],
    }


def test_a_marked_row_is_left_out_of_every_report_for_every_viewer(world):
    owner, member, buy = world["owner"], world["member"], world["buy"]
    budget = add_budget(
        owner, category=world["shopping"], amount_minor=50_000, month=MONTH[0],
        scope=Budget.Scope.HOUSEHOLD, household=world["household"],
    )
    before = {person: _views(person, budget) for person in (owner, member)}
    assert before[owner]["spending_total"] == 103_000
    assert before[owner]["budget_spent"] == 100_000

    mark_untracked_transfer(member, buy.pk)
    mark_untracked_transfer(owner, world["withdrawal"].pk)

    for person in (owner, member):
        after = _views(person, budget)
        assert after["totals"] == after["household"] == (200_000, 3_000)
        assert after["cash_flow"] == [(200_000, 3_000)]
        assert after["spending_by_category"] == {"Uncategorized": 3_000}
        assert after["spending_total"] == 3_000
        assert after["income_total"] == 200_000
        assert after["budget_spent"] == 0
        assert after["review"] == (200_000, 3_000)
        assert "SYNTHETIC BROKERAGE BUY" not in after["review_large"]
        assert "SYNTHETIC BROKERAGE BUY" in before[person]["review_large"]
        assert after["year_end"] == (200_000, 3_000)
        assert after["trend"] == [3_000]
        # Unusual spending and the monthly review read the same id set.
        assert {buy.pk, world["withdrawal"].pk} <= excluded_transfer_ids(person)


def _ai_august(person):
    window = {"date_from": "2026-08-01", "date_to": "2026-08-31"}
    totals = json.loads(ai_tools.cash_flow_totals(person, window).text)
    return totals["spending_minor"], ai_tools.spending_by_category(person, window).text


def test_ai_tools_leave_a_marked_row_out(world):
    # Without household AI consent the tools read the member's private accounts only.
    owner = world["owner"]
    paypal = world["txn"](world["private"], -9_000, "SYNTHETIC PAYPAL INSTANT TRANSFER")
    assign_category(owner, paypal.pk, world["shopping"].pk)
    spending, by_category = _ai_august(owner)
    assert spending == 9_000
    assert "Shopping" in by_category

    mark_untracked_transfer(owner, paypal.pk)

    spending, by_category = _ai_august(owner)
    assert spending == 0
    assert "Shopping" not in by_category


def test_export_and_the_transaction_list_show_the_mark(world):
    owner, member, buy = world["owner"], world["member"], world["buy"]
    mark_untracked_transfer(owner, buy.pk)

    rows = {row["id"]: row for row in collect_export_tables(member)["transactions"]}
    assert rows[buy.pk]["excluded_from_income_and_spending"] is True
    assert rows[buy.pk]["untracked_transfer"] is True
    assert rows[world["grocer"].pk]["excluded_from_income_and_spending"] is False
    assert rows[world["grocer"].pk]["untracked_transfer"] is False

    client = Client()
    client.force_login(member.user)
    page = client.get(reverse("transaction-list"))
    assert "Transfer (untracked account)" in page.content.decode()
    transfers_only = client.get(reverse("transaction-list") + "?category=transfer")
    assert "SYNTHETIC BROKERAGE BUY" in transfers_only.content.decode()

    session_client = Client()
    session_client.force_login(owner.user)
    session = session_client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()
    response = session_client.post(reverse("transaction-export"), {})
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()
    exported = {row["description"]: row for row in csv.DictReader(io.StringIO(body))}
    assert exported["SYNTHETIC BROKERAGE BUY"]["excluded_from_income_and_spending"] == "true"
    assert exported["SYNTHETIC GROCER"]["excluded_from_income_and_spending"] == "false"


def test_unmarking_counts_the_row_again_and_both_changes_are_audited(world):
    owner, member, buy = world["owner"], world["member"], world["buy"]
    before = _totals(member)

    mark_untracked_transfer(owner, buy.pk)
    assert _totals(member) == (before[0], before[1] + BROKERAGE_BUY)
    unmark_untracked_transfer(member, buy.pk)

    assert _totals(member) == before
    buy.refresh_from_db()
    assert buy.untracked_transfer is False
    assert buy.category == world["shopping"]
    history = list(
        TransactionCorrectionHistory.objects.filter(transaction=buy, field_name="untracked").order_by("pk")
    )
    assert [(row.previous_display, row.new_display) for row in history] == [
        ("Counted in income and spending", "Transfer (untracked account)"),
        ("Transfer (untracked account)", "Counted in income and spending"),
    ]
    events = list(
        AuditEvent.objects.filter(
            action=AuditEvent.Action.TRANSACTION_CORRECTED, target_id=buy.pk, changed_fields=["untracked_transfer"]
        ).order_by("occurred_at")
    )
    assert [event.actor_id for event in events] == [owner.pk, member.pk]
    assert [event.metadata["history_id"] for event in events] == [row.pk for row in history]


def test_edit_page_marks_and_unmarks_and_the_list_label_follows(world):
    member, buy = world["member"], world["buy"]
    client = Client()
    client.force_login(member.user)
    edit_url = reverse("transaction-edit", args=[buy.pk])
    url = reverse("transaction-untracked-transfer", args=[buy.pk])

    page = client.get(edit_url).content.decode()
    assert "Mark as transfer to or from an account I don't track" in page

    assert client.post(url, {"action": "mark"}).status_code == 302
    buy.refresh_from_db()
    assert buy.untracked_transfer is True
    page = client.get(edit_url).content.decode()
    assert "Transfer (untracked account)" in page
    assert "Remove transfer mark" in page

    assert client.post(url, {"action": "unmark"}).status_code == 302
    buy.refresh_from_db()
    assert buy.untracked_transfer is False

    refused = client.post(url, {"action": "unmark"}, follow=True).content.decode()
    assert "This row is not marked as a transfer to or from an untracked account." in refused
    client.post(url, {"action": "something-else"})
    buy.refresh_from_db()
    assert buy.untracked_transfer is False


def test_only_a_member_who_may_edit_the_row_can_mark_it(world):
    owner, member = world["owner"], world["member"]
    private_row = world["txn"](world["private"], -5_000, "SYNTHETIC PRIVATE MOVE")
    outsider = make_person("outsider")

    with pytest.raises(PermissionDenied):
        mark_untracked_transfer(member, private_row.pk)
    with pytest.raises(PermissionDenied):
        mark_untracked_transfer(outsider, world["buy"].pk)
    client = Client()
    client.force_login(member.user)
    response = client.post(reverse("transaction-untracked-transfer", args=[private_row.pk]), {"action": "mark"})
    assert response.status_code == 404
    private_row.refresh_from_db()
    assert private_row.untracked_transfer is False

    # The owner's mark on a private row is theirs; the household totals never saw it.
    member_before = _totals(member)
    mark_untracked_transfer(owner, private_row.pk)
    assert _totals(member) == member_before
    with pytest.raises(PermissionDenied):
        unmark_untracked_transfer(member, private_row.pk)


def test_investment_rows_and_accounts_cannot_be_marked(world):
    owner = world["owner"]
    activity = make_transaction(
        owner, world["shared"], transaction_date=AUGUST, amount_minor=-7_000,
        description="SYNTHETIC REINVEST", kind=Transaction.Kind.INVESTMENT_ACTIVITY,
    )
    brokerage_row = world["txn"](world["investment"], 7_000, "SYNTHETIC CASH SWEEP")

    for row in (activity, brokerage_row):
        with pytest.raises(ValidationError) as refused:
            mark_untracked_transfer(owner, row.pk)
        assert refused.value.messages == [INVESTMENT_REFUSED]
        row.refresh_from_db()
        assert row.untracked_transfer is False

    with pytest.raises(IntegrityError), transaction.atomic():
        Transaction.objects.filter(pk=activity.pk).update(untracked_transfer=True)

    preview = preview_bulk_edit(
        owner, matching=Transaction.objects.none(), transaction_ids=[activity.pk, brokerage_row.pk],
        select_matching=False, action=ACTION_MARK_UNTRACKED,
    )
    assert preview.eligible_ids == []
    assert preview.skip_counts == {SKIP_INVESTMENT: 1, SKIP_INVESTMENT_ACCOUNT: 1}


def test_a_marked_row_later_paired_is_left_out_once_and_survives_pair_undo_and_dismiss(world):
    owner = world["owner"]
    out_leg = world["txn"](world["shared"], -25_000, "SYNTHETIC ONLINE TRANSFER TO SAVINGS")
    in_leg = world["txn"](world["private"], 25_000, "SYNTHETIC ONLINE TRANSFER FROM CHECKING")
    TransferPair.objects.all().delete()
    Transaction.objects.filter(pk__in=(out_leg.pk, in_leg.pk)).update(transaction_date=date(2026, 9, 3))
    september = (date(2026, 9, 1), date(2026, 9, 30))

    def september_totals():
        totals = income_and_spending_totals(owner, date_from=september[0], date_to=september[1])
        return totals.income_minor, totals.spending_minor

    mark_untracked_transfer(owner, out_leg.pk)
    assert september_totals() == (25_000, 0)

    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(leg_a=out_leg)
    assert pair.status == TransferPair.Status.AUTO_MARKED
    # Each leg is one row: excluded once, not subtracted twice.
    assert september_totals() == (0, 0)
    report = cash_flow_report(owner, date_from=september[0], date_to=september[1], today=TODAY)
    assert [(period.income_minor, period.spending_minor) for period in report.periods] == [(0, 0)]

    undo_transfer_pair(owner, pair.pk)
    out_leg.refresh_from_db()
    assert out_leg.untracked_transfer is True
    assert september_totals() == (25_000, 0)

    # A suggested pair (no transfer wording) that is dismissed leaves the mark too.
    move_out = world["txn"](world["shared"], -12_000, "SYNTHETIC MOVE OUT")
    move_in = world["txn"](world["private"], 12_000, "SYNTHETIC MOVE IN")
    Transaction.objects.filter(pk__in=(move_out.pk, move_in.pk)).update(transaction_date=date(2026, 9, 20))
    mark_untracked_transfer(owner, move_out.pk)
    refresh_transfer_pairs(owner)
    suggested = TransferPair.objects.get(leg_a=move_out)
    assert suggested.status == TransferPair.Status.SUGGESTED
    assert september_totals() == (25_000 + 12_000, 0)
    dismiss_transfer_pair(owner, suggested.pk)
    move_out.refresh_from_db()
    assert move_out.untracked_transfer is True
    assert september_totals() == (25_000 + 12_000, 0)


def test_a_row_already_left_out_by_a_pair_is_not_marked_again(world):
    owner = world["owner"]
    out_leg = world["txn"](world["shared"], -9_000, "SYNTHETIC SUGGESTED OUT")
    in_leg = world["txn"](world["private"], 9_000, "SYNTHETIC SUGGESTED IN")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(leg_a=out_leg)
    if pair.status == TransferPair.Status.SUGGESTED:
        confirm_transfer_pair(owner, pair.pk)

    with pytest.raises(ValidationError) as refused:
        mark_untracked_transfer(owner, out_leg.pk)
    assert refused.value.messages == [ALREADY_EXCLUDED]
    preview = preview_bulk_edit(
        owner, matching=Transaction.objects.none(), transaction_ids=[in_leg.pk],
        select_matching=False, action=ACTION_MARK_UNTRACKED,
    )
    assert preview.skip_counts == {SKIP_TRANSFER: 1}


def test_bulk_mark_and_unmark_record_one_event_per_account_and_undo_restores(world):
    owner, member = world["owner"], world["member"]
    rows = [world["buy"], world["grocer"]]
    before = _totals(member)

    preview, undo = apply_bulk_edit(
        member, matching=Transaction.objects.none(), transaction_ids=[row.pk for row in rows],
        action=ACTION_MARK_UNTRACKED,
    )
    assert sorted(preview.eligible_ids) == sorted(row.pk for row in rows)
    assert _totals(member) == (before[0], 0)
    assert _totals(owner) == (before[0], 0)
    event = AuditEvent.objects.get(action=AuditEvent.Action.BULK_APPLIED)
    assert event.changed_fields == ["untracked_transfer"]
    assert event.metadata["row_count"] == 2
    assert TransactionCorrectionHistory.objects.filter(field_name="untracked").count() == 2

    undo_bulk_edit(member, undo.pk)
    assert _totals(member) == before
    assert not Transaction.objects.filter(untracked_transfer=True).exists()
    assert AuditEvent.objects.filter(action=AuditEvent.Action.BULK_UNDONE).count() == 1

    mark_untracked_transfer(owner, world["buy"].pk)
    _preview, unmark_undo = apply_bulk_edit(
        owner, matching=Transaction.objects.none(), transaction_ids=[world["buy"].pk, world["grocer"].pk],
        action=ACTION_UNMARK_UNTRACKED,
    )
    assert _totals(owner) == before
    undo_bulk_edit(owner, unmark_undo.pk)
    assert list(Transaction.objects.filter(untracked_transfer=True).values_list("pk", flat=True)) == [world["buy"].pk]


def test_linked_refunds_cannot_be_marked_and_a_marked_purchase_drops_its_refund_links(world):
    owner = world["owner"]
    purchase = world["txn"](world["shared"], -6_000, "SYNTHETIC STORE")
    refund = world["txn"](world["shared"], 1_500, "SYNTHETIC STORE REFUND")
    link_refund(owner, refund.pk, purchase.pk)

    with pytest.raises(ValidationError) as refused:
        mark_untracked_transfer(owner, refund.pk)
    assert refused.value.messages == [LINKED_REFUND_REFUSED]
    preview = preview_bulk_edit(
        owner, matching=Transaction.objects.none(), transaction_ids=[refund.pk],
        select_matching=False, action=ACTION_MARK_UNTRACKED,
    )
    assert preview.skip_counts == {SKIP_LINKED_REFUND: 1}

    before = _totals(owner)
    mark_untracked_transfer(owner, purchase.pk)
    # The purchase no longer counts, so its refund counts as income again (#570).
    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _totals(owner) == (before[0] + 1_500, before[1] - 6_000 + 1_500)


def _untracked_rule(person, contains, *, owner_kind="personal", account=None):
    return save_category_rule(
        person, owner_kind=owner_kind, description_contains=contains, account_id=account,
        min_amount_minor=None, max_amount_minor=None, category_id=None, priority=0,
        action=CategoryRule.Action.UNTRACKED_TRANSFER,
    )


def test_a_rule_can_mark_rows_preview_then_apply_and_reverse(world):
    owner, member, buy = world["owner"], world["member"], world["buy"]
    second = world["txn"](world["private"], -2_500, "SYNTHETIC BROKERAGE BUY 2")
    brokerage_row = world["txn"](world["investment"], -2_500, "SYNTHETIC BROKERAGE BUY INSIDE")
    rule = _untracked_rule(owner, "brokerage buy")
    assert rule.category_id is None
    assert rule.scope_household == world["household"]
    assert rule.confirmed_at is None

    _rule, matches = preview_rule(owner, rule.pk)
    # The hand-categorized buy is reached: marking keeps its category.
    assert {txn.pk for txn in matches} == {buy.pk, second.pk}
    assert not Transaction.objects.filter(untracked_transfer=True).exists()

    application, skipped = apply_rule(owner, rule.pk)
    assert skipped == 0
    assert set(Transaction.objects.filter(untracked_transfer=True).values_list("pk", flat=True)) == {buy.pk, second.pk}
    buy.refresh_from_db()
    assert buy.category == world["shopping"]
    assert buy.category_source == Transaction.CategorySource.MANUAL
    brokerage_row.refresh_from_db()
    assert brokerage_row.untracked_transfer is False
    assert RuleApplicationEntry.objects.filter(application=application, marked_untracked=True).count() == 2
    history = TransactionCorrectionHistory.objects.get(transaction=buy, field_name="untracked")
    assert history.new_display == "Transfer (untracked account) (rule: brokerage buy)"
    assert _totals(member)[1] == 3_000
    assert AuditEvent.objects.filter(action=AuditEvent.Action.RULE_APPLIED, target_id=rule.pk).count() == 1

    result = reverse_application(owner, application.pk)
    assert result.restored == 2
    assert not Transaction.objects.filter(untracked_transfer=True).exists()
    buy.refresh_from_db()
    assert buy.category == world["shopping"]
    assert _totals(member)[1] == 103_000


def test_a_confirmed_untracked_rule_marks_imported_rows_on_private_and_household_accounts(world):
    owner = world["owner"]
    rule = _untracked_rule(owner, "synthetic vanguard")
    content = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC VANGUARD BUY,-500.00,USD\n"
    import_csv(owner, world["private"], content)
    assert not Transaction.objects.filter(untracked_transfer=True).exists()  # not confirmed yet

    apply_rule(owner, rule.pk)
    assert Transaction.objects.filter(untracked_transfer=True).count() == 1
    later = b"When,Memo,Amount,Currency\n09/28/2026,SYNTHETIC VANGUARD BUY,-250.00,USD\n"
    import_csv(owner, world["private"], later)
    import_csv(owner, world["shared"], later)
    imported = Transaction.objects.filter(description="SYNTHETIC VANGUARD BUY", amount_minor=-25_000)
    assert imported.count() == 2
    assert all(row.untracked_transfer for row in imported)
    assert all(row.category_id is None for row in imported)
    assert TransactionCorrectionHistory.objects.filter(
        transaction__in=imported, field_name="untracked"
    ).count() == 2


def test_household_untracked_rule_from_the_rules_page(world):
    member = world["member"]
    client = Client()
    client.force_login(member.user)
    list_url = reverse("category-rule-list")
    missing = client.post(list_url, {
        "owner_kind": "household", "description_contains": "rocket savings", "rule_action": "categorize",
        "priority": "0", "enabled": "on",
    })
    assert missing.status_code == 200
    assert "Choose a category, or mark as untracked transfer." in missing.content.decode()

    created = client.post(list_url, {
        "owner_kind": "household", "description_contains": "savings withdrawal",
        "rule_action": "untracked_transfer", "category": str(world["shopping"].pk), "priority": "0", "enabled": "on",
    })
    assert created.status_code == 302
    rule = CategoryRule.objects.get()
    assert rule.action == CategoryRule.Action.UNTRACKED_TRANSFER
    assert rule.category_id is None
    assert rule.owner_household == world["household"]
    detail = client.get(reverse("category-rule-detail", args=[rule.pk])).content.decode()
    assert "SYNTHETIC SAVINGS WITHDRAWAL" in detail
    listing = client.get(list_url).content.decode()
    assert "Mark as transfer (untracked account)" in listing

    client.post(reverse("category-rule-detail", args=[rule.pk]), {
        "action": "apply", "rule_version": rule.updated_at.isoformat(),
    })
    world["withdrawal"].refresh_from_db()
    assert world["withdrawal"].untracked_transfer is True


def test_rules_still_never_assign_the_transfer_category(world):
    owner, household = world["owner"], world["household"]
    transfer = Category.objects.get(household=household, code=Category.Code.TRANSFER)
    assert not assignable_categories(owner).filter(pk=transfer.pk).exists()
    with pytest.raises(PermissionDenied):
        save_category_rule(
            owner, owner_kind="personal", description_contains="synthetic", account_id=None,
            min_amount_minor=None, max_amount_minor=None, category_id=transfer.pk, priority=0,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        CategoryRule.objects.create(
            owner_person=owner, description_contains="synthetic", action=CategoryRule.Action.UNTRACKED_TRANSFER,
            category=world["shopping"], scope_household=household,
        )


def test_a_review_stored_before_a_mark_says_it_may_disagree_until_regenerated(world):
    owner = world["owner"]
    review_for_viewer(owner, MONTH[0], today=TODAY)
    client = Client()
    client.force_login(owner.user)
    page_url = reverse("monthly-review") + "?month=2026-08"
    hint = "marked or unmarked as transfers to or from an untracked account since this review"
    assert hint not in client.get(page_url).content.decode()

    mark_untracked_transfer(owner, world["buy"].pk)

    page = client.get(page_url)
    cash = cash_flow_report(owner, date_from=MONTH[0], date_to=MONTH[1], today=TODAY).periods[0]
    stored = page.context["review"].facts
    # The stored review stays as it was (#509), so the page says why it differs.
    assert stored["spending_minor"] == cash.spending_minor + 100_000
    assert hint in page.content.decode()

    client.post(reverse("monthly-review-regenerate"), {"month": "2026-08"})
    page = client.get(page_url)
    assert page.context["review"].facts["spending_minor"] == cash.spending_minor
    assert hint not in page.content.decode()


def test_reversing_a_rule_keeps_a_mark_a_member_set_again_after_removing_the_rules(world):
    owner, buy = world["owner"], world["buy"]
    rule = _untracked_rule(owner, "brokerage buy")
    application, _skipped = apply_rule(owner, rule.pk)
    unmark_untracked_transfer(owner, buy.pk)
    mark_untracked_transfer(owner, buy.pk)

    result = reverse_application(owner, application.pk)

    buy.refresh_from_db()
    assert buy.untracked_transfer is True
    assert result.restored == 0
    assert result.skipped_manual == 1


def test_category_and_mark_applications_reverse_independently(world):
    owner, household = world["owner"], world["household"]
    row = world["txn"](world["private"], -4_000, "SYNTHETIC VANGUARD TRANSFER")
    dining = Category.objects.get(household=household, name="Dining")
    category_rule = save_category_rule(
        owner, owner_kind="personal", description_contains="vanguard", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=dining.pk, priority=0,
    )
    category_application, _ = apply_rule(owner, category_rule.pk)
    row.refresh_from_db()
    assert row.category == dining
    set_rule_enabled(owner, category_rule.pk, False)
    mark_rule = _untracked_rule(owner, "vanguard")
    mark_application, _ = apply_rule(owner, mark_rule.pk)
    row.refresh_from_db()
    assert row.untracked_transfer is True

    # The mark did not change the category, so it does not block restoring it.
    assert reverse_application(owner, category_application.pk).restored == 1
    row.refresh_from_db()
    assert row.category_id is None
    assert row.untracked_transfer is True

    assert reverse_application(owner, mark_application.pk).restored == 1
    row.refresh_from_db()
    assert row.category_id is None
    assert row.untracked_transfer is False


# Undoing a bulk untracked-transfer mark restores the refund links it dropped (#797).
# Owner decision on #679: a full undo. Rule reversal and single unmarking leave a dropped link dropped.

def _purchase_with_refund(world, description="SYNTHETIC STORE"):
    owner = world["owner"]
    purchase = world["txn"](world["shared"], -6_000, description)
    refund = world["txn"](world["shared"], 1_500, f"{description} REFUND")
    assign_category(owner, purchase.pk, world["shopping"].pk)
    link_refund(owner, refund.pk, purchase.pk)
    return purchase, refund


def _bulk_mark(person, rows):
    _preview, undo = apply_bulk_edit(
        person, matching=Transaction.objects.none(), transaction_ids=[row.pk for row in rows],
        action=ACTION_MARK_UNTRACKED,
    )
    return undo


def _link_history(refund):
    return list(
        TransactionCorrectionHistory.objects.filter(
            transaction=refund, field_name=TransactionCorrectionHistory.Field.REFUND_LINK
        )
        .order_by("pk")
        .values_list("previous_description", "new_description")
    )


def test_undoing_a_bulk_mark_restores_the_refund_link_and_totals(world):
    owner, member = world["owner"], world["member"]
    purchase, refund = _purchase_with_refund(world)
    before = {person.pk: _totals(person) for person in (owner, member)}

    undo = _bulk_mark(member, [purchase])
    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _totals(owner) != before[owner.pk]

    undo_bulk_edit(member, undo.pk)

    link = RefundLink.objects.get(refund=refund)
    assert link.original_id == purchase.pk
    assert link.original_part_id is None
    refund.refresh_from_db()
    assert refund.category == world["shopping"]
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert {person.pk: _totals(person) for person in (owner, member)} == before
    assert _link_history(refund) == [("unlinked", "linked"), ("linked", "unlinked"), ("unlinked", "linked")]


def test_undo_restores_a_link_to_a_split_part(world):
    owner, household = world["owner"], world["household"]
    dining = Category.objects.get(household=household, name="Dining")
    purchase, refund = _purchase_with_refund(world)
    RefundLink.objects.filter(refund=refund).delete()
    split_transaction(
        owner, purchase.pk,
        [{"amount_minor": -4_000, "category_id": world["shopping"].pk},
         {"amount_minor": -2_000, "category_id": dining.pk}],
    )
    purchase.refresh_from_db()
    dining_part = purchase.splits.get(category=dining)
    link_refund(owner, refund.pk, purchase.pk, dining_part.pk)
    before = _totals(owner)

    undo = _bulk_mark(owner, [purchase])
    assert not RefundLink.objects.filter(refund=refund).exists()
    undo_bulk_edit(owner, undo.pk)

    link = RefundLink.objects.get(refund=refund)
    assert (link.original_id, link.original_part_id) == (purchase.pk, dining_part.pk)
    refund.refresh_from_db()
    assert (refund.category, refund.category_source) == (dining, Transaction.CategorySource.INHERITED)
    assert _totals(owner) == before


def _relink_elsewhere(world, refund):
    other = world["txn"](world["shared"], -9_000, "SYNTHETIC OTHER STORE")
    link_refund(world["owner"], refund.pk, other.pk)


def _recategorize(world, refund):
    assign_category(world["owner"], refund.pk, world["income"].pk)


def _mark_refund(world, refund):
    mark_untracked_transfer(world["owner"], refund.pk)


def _pair_into_a_marked_transfer(world, refund):
    # Pairing never touches the leg's updated_at, so undo must check marked pairs itself.
    out_leg = world["txn"](world["private"], -refund.amount_minor, "SYNTHETIC ONLINE TRANSFER TO CHECKING")
    refresh_transfer_pairs(world["owner"])
    pair = TransferPair.objects.get(Q(leg_a=refund, leg_b=out_leg) | Q(leg_a=out_leg, leg_b=refund))
    if pair.status == TransferPair.Status.SUGGESTED:
        confirm_transfer_pair(world["owner"], pair.pk)


def _archive(_world, refund):
    Transaction.objects.filter(pk=refund.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())


@pytest.mark.parametrize("change", [_relink_elsewhere, _recategorize, _mark_refund, _pair_into_a_marked_transfer, _archive])
def test_undo_skips_a_link_whose_refund_changed_since(world, change):
    owner = world["owner"]
    purchase, refund = _purchase_with_refund(world)
    kept_purchase, kept_refund = _purchase_with_refund(world, "SYNTHETIC BOOKSHOP")
    undo = _bulk_mark(owner, [purchase, kept_purchase])

    change(world, refund)
    refund.refresh_from_db()
    state = (refund.category_id, refund.category_source, refund.untracked_transfer, refund.status)
    link_before = list(RefundLink.objects.filter(refund=refund).values_list("original_id", flat=True))
    history_before = _link_history(refund)

    undo_bulk_edit(owner, undo.pk)

    # The marks are undone and the untouched link comes back; the changed refund is left as it is.
    assert not Transaction.objects.filter(pk__in=(purchase.pk, kept_purchase.pk), untracked_transfer=True).exists()
    assert RefundLink.objects.get(refund=kept_refund).original_id == kept_purchase.pk
    assert list(RefundLink.objects.filter(refund=refund).values_list("original_id", flat=True)) == link_before
    refund.refresh_from_db()
    assert (refund.category_id, refund.category_source, refund.untracked_transfer, refund.status) == state
    assert _link_history(refund) == history_before


def test_single_unmarking_still_leaves_a_dropped_link_dropped(world):
    owner = world["owner"]
    purchase, refund = _purchase_with_refund(world)
    mark_untracked_transfer(owner, purchase.pk)
    unmark_untracked_transfer(owner, purchase.pk)
    assert not RefundLink.objects.filter(refund=refund).exists()


def test_rule_reversal_still_leaves_a_dropped_link_dropped(world):
    owner = world["owner"]
    purchase, refund = _purchase_with_refund(world)
    application, _ = apply_rule(owner, _untracked_rule(owner, "synthetic store").pk)
    purchase.refresh_from_db()
    assert purchase.untracked_transfer is True
    assert reverse_application(owner, application.pk).restored == 1
    purchase.refresh_from_db()
    assert purchase.untracked_transfer is False
    assert not RefundLink.objects.filter(refund=refund).exists()


# Links that stood beside a marked transfer pair before the mark (#797, liaison decision on #809):
# undo restores the stored RefundLinkDrop rows verbatim and never re-derives them under today's rules.


def _purchase_paired_for_owner(world, refund_account=None):
    """A shared purchase paired with the owner's private account, so only the owner sees it left out.

    The member links its refund, so the pair rule drops that link for the owner alone.
    """
    owner, member = world["owner"], world["member"]
    purchase = world["txn"](world["shared"], -6_000, "SYNTHETIC ONLINE TRANSFER TO SAVINGS")
    world["txn"](world["private"], 6_000, "SYNTHETIC ONLINE TRANSFER FROM CHECKING")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(Q(leg_a=purchase) | Q(leg_b=purchase))
    if pair.status == TransferPair.Status.SUGGESTED:
        confirm_transfer_pair(owner, pair.pk)
    refund = world["txn"](refund_account or world["shared"], 1_500, "SYNTHETIC SAVINGS REFUND")
    link_refund(member, refund.pk, purchase.pk)
    assert _drops(refund) == [(owner.pk, pair.pk)]
    return purchase, refund, pair


def _drops(refund):
    return sorted(RefundLinkDrop.objects.filter(link__refund=refund).values_list("person_id", "pair_id"))


def _member_totals(world):
    return {name: _totals(world[name]) for name in ("owner", "member")}


def _shift(totals, income=0, spending=0):
    return (totals[0] + income, totals[1] + spending)


def test_undo_restores_a_link_and_its_drop_beside_a_pair_marked_for_another_member(world):
    purchase, refund, pair = _purchase_paired_for_owner(world)
    before = _member_totals(world)

    undo = _bulk_mark(world["member"], [purchase])
    assert not RefundLink.objects.filter(refund=refund).exists()
    # The owner already left the purchase out and counted the refund as income; the member now does too.
    assert _member_totals(world) == {
        "owner": before["owner"], "member": _shift(before["member"], income=1_500, spending=-4_500),
    }
    undo_bulk_edit(world["member"], undo.pk)

    assert RefundLink.objects.get(refund=refund).original_id == purchase.pk
    assert _drops(refund) == [(world["owner"].pk, pair.pk)]
    assert _member_totals(world) == before


def test_undo_restores_a_link_and_drop_kept_when_the_refund_account_was_unshared(world):
    owner, member = world["owner"], world["member"]
    cards = make_account(owner, name="Owner Card", scope=Account.Scope.HOUSEHOLD, household=world["household"])
    purchase, refund, pair = _purchase_paired_for_owner(world, refund_account=cards)
    # Unsharing keeps the link and the owner's drop (#579), though the owner is now the refund's only viewer.
    unshare_account(owner, cards.pk)
    assert _drops(refund) == [(owner.pk, pair.pk)]
    before = _member_totals(world)

    undo = _bulk_mark(member, [purchase])
    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _member_totals(world) == {"owner": before["owner"], "member": _shift(before["member"], spending=-6_000)}
    undo_bulk_edit(member, undo.pk)

    # Today's pair rule would delete this link; the stored state comes back instead.
    assert RefundLink.objects.get(refund=refund).original_id == purchase.pk
    assert _drops(refund) == [(owner.pk, pair.pk)]
    assert _member_totals(world) == before


def test_undo_restores_the_link_without_the_drop_of_a_pair_undone_since(world):
    owner, member = world["owner"], world["member"]
    purchase, refund, pair = _purchase_paired_for_owner(world)
    before = _member_totals(world)

    undo = _bulk_mark(member, [purchase])
    after_mark = _member_totals(world)
    assert after_mark == {"owner": before["owner"], "member": _shift(before["member"], income=1_500, spending=-4_500)}
    undo_transfer_pair(owner, pair.pk)
    undo_bulk_edit(member, undo.pk)

    assert RefundLink.objects.get(refund=refund).original_id == purchase.pk
    assert _drops(refund) == []
    # With the pair gone the owner counts both legs and the refund offsets the purchase, as the member does.
    assert _member_totals(world) == {
        "owner": _shift(before["owner"], income=6_000 - 1_500, spending=6_000 - 1_500), "member": before["member"],
    }


def test_undo_skips_a_link_whose_purchase_joined_a_marked_pair_since(world):
    owner, member = world["owner"], world["member"]
    purchase, refund = _purchase_with_refund(world, "SYNTHETIC ONLINE TRANSFER TO SAVINGS")
    before = _member_totals(world)

    undo = _bulk_mark(member, [purchase])
    assert _member_totals(world) == {
        name: _shift(totals, income=1_500, spending=-4_500) for name, totals in before.items()
    }
    world["txn"](world["private"], 6_000, "SYNTHETIC ONLINE TRANSFER FROM CHECKING")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(Q(leg_a=purchase) | Q(leg_b=purchase))
    if pair.status == TransferPair.Status.SUGGESTED:
        confirm_transfer_pair(owner, pair.pk)
    history = _link_history(refund)
    undo_bulk_edit(member, undo.pk)

    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _link_history(refund) == history
    # The owner leaves the new pair out; the member, who cannot see its other leg, counts the purchase again.
    assert _member_totals(world) == {
        "owner": _shift(before["owner"], income=1_500, spending=-4_500),
        "member": _shift(before["member"], income=1_500, spending=1_500),
    }
