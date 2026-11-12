"""A stored monthly review says it may be stale when a refund link to its month is dropped (#675).

A July purchase with a linked August refund stops counting when it is marked as
an untracked transfer (edit page, bulk edit or rule) or its import is undone.
Its refund link is dropped, so the August refund counts as income again and
the stored August review no longer matches cash flow. The page must say so,
and Regenerate must clear the hint. Synthetic rows only.
"""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from finance.bulk_edit_services import ACTION_MARK_UNTRACKED, apply_bulk_edit
from finance.cash_flow import cash_flow_report
from finance.category_services import assign_category, link_refund
from finance.csv_import.parser import read_csv
from finance.csv_import.services import commit_csv_import, undo_import_batch
from finance.models import CategoryRule, ImportBatch, RefundLink, Transaction, TransactionCorrectionHistory
from finance.monthly_review import review_for_viewer
from finance.rule_services import apply_rule, save_category_rule
from tests.test_category_rules import mapping
from tests import test_untracked_transfers as untracked
from tests.test_untracked_transfers import MONTH, TODAY

pytestmark = pytest.mark.django_db

# The owner and a member share a household checking account (fixture from #642's tests).
world = untracked.world

STALE = "may not match cash flow until you regenerate it"
PURCHASE = "SYNTHETIC GADGET"


def _july_purchase_with_august_refund(world, *, purchase=None):
    owner = world["owner"]
    if purchase is None:
        purchase = world["txn"](world["shared"], -5_000, PURCHASE)
        Transaction.objects.filter(pk=purchase.pk).update(transaction_date=date(2026, 7, 20))
    refund = world["txn"](world["shared"], 2_000, f"{PURCHASE} REFUND")
    Transaction.objects.filter(pk=refund.pk).update(transaction_date=date(2026, 8, 5))
    assign_category(owner, purchase.pk, world["shopping"].pk)
    link_refund(owner, refund.pk, purchase.pk)
    for viewer in (owner, world["member"]):
        review_for_viewer(viewer, MONTH[0], today=TODAY)
    return purchase, refund


def _august_page(person):
    client = Client()
    client.force_login(person.user)
    page = client.get(reverse("monthly-review") + "?month=2026-08")
    assert page.status_code == 200
    return page


def _assert_stale_until_regenerated(world, refund):
    owner = world["owner"]
    assert not RefundLink.objects.filter(refund=refund).exists()
    for viewer in (owner, world["member"]):
        page = _august_page(viewer)
        stored = page.context["review"].facts
        live = cash_flow_report(viewer, date_from=MONTH[0], date_to=MONTH[1], today=TODAY).periods[0]
        assert (stored["income_minor"], stored["spending_minor"]) != (live.income_minor, live.spending_minor)
        assert STALE in page.content.decode().lower()

    review_for_viewer(owner, MONTH[0], today=TODAY, force=True)
    page = _august_page(owner)
    stored = page.context["review"].facts
    live = cash_flow_report(owner, date_from=MONTH[0], date_to=MONTH[1], today=TODAY).periods[0]
    assert (stored["income_minor"], stored["spending_minor"]) == (live.income_minor, live.spending_minor)
    assert STALE not in page.content.decode().lower()


def test_marking_the_july_purchase_on_the_edit_page_shows_the_hint_on_the_august_review(world):
    purchase, refund = _july_purchase_with_august_refund(world)
    client = Client()
    client.force_login(world["owner"].user)
    response = client.post(reverse("transaction-untracked-transfer", args=[purchase.pk]), {"action": "mark"})
    assert response.status_code == 302
    _assert_stale_until_regenerated(world, refund)


def test_marking_the_july_purchase_by_bulk_edit_shows_the_hint_on_the_august_review(world):
    purchase, refund = _july_purchase_with_august_refund(world)
    apply_bulk_edit(
        world["member"], matching=Transaction.objects.none(), transaction_ids=[purchase.pk],
        action=ACTION_MARK_UNTRACKED,
    )
    _assert_stale_until_regenerated(world, refund)


def test_marking_the_july_purchase_by_a_rule_shows_the_hint_on_the_august_review(world):
    owner = world["owner"]
    _purchase, refund = _july_purchase_with_august_refund(world)
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="synthetic gadget", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=None, priority=0,
        action=CategoryRule.Action.UNTRACKED_TRANSFER,
    )
    apply_rule(owner, rule.pk)
    _assert_stale_until_regenerated(world, refund)


def test_undoing_the_july_import_shows_the_hint_on_the_august_review(world):
    owner = world["owner"]
    content = f"When,Memo,Amount,Currency\n07/20/2026,{PURCHASE},-50.00,USD\n".encode()
    batch = commit_csv_import(
        owner.user, world["shared"].pk, content=content, document=read_csv(content), mapping=mapping(),
        source=ImportBatch.Source.HUNTINGTON, date_range_start=date(2026, 7, 1), date_range_end=date(2026, 7, 31),
    ).batch
    purchase = Transaction.objects.get(import_batch=batch)
    _purchase, refund = _july_purchase_with_august_refund(world, purchase=purchase)

    undo_import_batch(owner.user, world["shared"].pk, batch.pk)
    _assert_stale_until_regenerated(world, refund)


def test_a_refund_link_dropped_for_another_member_alone_shows_no_hint(world):
    owner, member = world["owner"], world["member"]
    _purchase, refund = _july_purchase_with_august_refund(world)
    # A link dropped for one member alone is listed only to them (#570), so it
    # must not tell anyone else that something changed.
    TransactionCorrectionHistory.objects.create(
        transaction=refund, actor=member, field_name=TransactionCorrectionHistory.Field.REFUND_LINK,
        previous_description="linked", new_description="unlinked", only_for=member,
    )
    assert STALE not in _august_page(owner).content.decode().lower()
    assert STALE in _august_page(member).content.decode().lower()
