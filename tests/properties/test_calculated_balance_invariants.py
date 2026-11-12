"""Saved calculated balances always equal calculate() after any ledger change (#641, #676).

Owner decision (2026-11-12): every import, edit or deletion that changes a
balance or a transaction at or before the anchor recalculates the saved
calculated rows under the account lock, and deletes them when no real balance
remains. This drives random sequences of the real write paths (CSV import,
SimpleFIN sync with late rows, import undo, manual entries, transaction edits,
manual balance record, edit and delete) against an account whose calculated
rows were saved, and after every step checks:

- the stored calculated rows are exactly what a manual Calculate would save
  now, or none when it would refuse; an account with none saved gets none;
- SimpleFIN and manual balances are only ever changed by the step that names them;
- a step that only adds rows after the anchor leaves every calculated row as it was.

Synthetic data only.
"""

from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st

from finance.calculated_balances import CalculationError, _anchor, calculate, save_calculated_balances
from finance.manual_entry_services import add_manual_transaction, delete_manual_transaction
from finance.models import Account, BalanceSnapshot, ImportBatch, Transaction
from finance.simplefin_services import sync_connection
from finance.snapshot_services import (
    SnapshotError,
    delete_manual_snapshot,
    record_manual_snapshot,
    update_manual_snapshot,
)
from finance.csv_import.services import undo_import_batch

from . import harness as ledger
from .strategies import HUNTINGTON, Row, decimal_text, simplefin_epoch, simplefin_payload

pytestmark = [pytest.mark.property, pytest.mark.django_db]

CALCULATED = BalanceSnapshot.Source.CALCULATED
MANUAL = BalanceSnapshot.Source.MANUAL
CUTOVER = date(2026, 6, 1)
days = st.dates(CUTOVER, date(2026, 10, 31))
money = st.integers(-500_000, 500_000).filter(bool)
balances = st.integers(-2_000_000, 2_000_000)


@dataclass(frozen=True)
class Case:
    feed: str  # "csv" or "simplefin"
    rows: tuple  # (day, amount_minor)
    anchor: tuple  # (day, amount_minor)
    steps: tuple


def _step():
    return st.one_of(
        st.tuples(st.just("import"), st.lists(st.tuples(days, money), min_size=1, max_size=3)),
        st.tuples(st.just("manual_entry"), days, money),
        st.tuples(st.just("delete_entry"), st.integers(0, 9)),
        st.tuples(st.just("undo"), st.integers(0, 9)),
        st.tuples(st.just("edit"), st.integers(0, 99), days, money),
        st.tuples(st.just("record_balance"), days, balances),
        st.tuples(st.just("update_balance"), st.integers(0, 9), days, balances),
        st.tuples(st.just("delete_balance"), st.integers(0, 9)),
    )


@st.composite
def cases(draw):
    return Case(
        feed=draw(st.sampled_from(("csv", "simplefin"))),
        rows=tuple(draw(st.lists(st.tuples(days, money), min_size=1, max_size=5))),
        anchor=(draw(st.dates(date(2026, 8, 1), date(2026, 10, 31))), draw(balances)),
        steps=tuple(draw(st.lists(_step(), min_size=1, max_size=5))),
    )


def expected(account):
    try:
        return [(row.month_end, row.balance_minor) for row in calculate(account).rows]
    except CalculationError:
        return []


def stored(account):
    return list(
        BalanceSnapshot.objects.filter(account=account, source=CALCULATED)
        .order_by("snapshot_date").values_list("snapshot_date", "amount_minor")
    )


def stored_with_ids(account):
    return list(
        BalanceSnapshot.objects.filter(account=account, source=CALCULATED)
        .order_by("snapshot_date").values_list("pk", "snapshot_date", "amount_minor")
    )


def real_balances(account):
    return set(
        BalanceSnapshot.objects.filter(account=account).exclude(source=CALCULATED)
        .values_list("pk", "source", "snapshot_date", "amount_minor")
    )


class World:
    """One account with saved calculated rows, and the steps that change its ledger."""

    def __init__(self, case):
        self.case = case
        self.owner = ledger.make_member("owner")
        self.account = ledger.make_account(self.owner, Account.Type.CHECKING)
        self.client = Client()
        self.client.force_login(self.owner.user)
        self.feed = []  # the SimpleFIN provider's cumulative transaction list
        self.serial = 0
        if case.feed == "simplefin":
            self.connection = ledger.link_simplefin(self.owner, self.account, CUTOVER)
            self.sync(case.rows, case.anchor)
        else:
            self.import_csv(case.rows)
            record_manual_snapshot(self.owner, self.account.pk, snapshot_date=case.anchor[0],
                                   amount_minor=case.anchor[1])

    def next_id(self):
        self.serial += 1
        return self.serial

    def import_csv(self, rows):
        csv_rows = [Row(day, amount, f"SYN CALC N{self.next_id()}", "", "") for day, amount in rows]
        ledger.import_rows(self.owner, self.account, HUNTINGTON, csv_rows)

    def sync(self, rows, balance):
        for day, amount in rows:
            self.feed.append({"id": f"SYN-CALC-{self.next_id()}", "posted": simplefin_epoch(day),
                              "amount": decimal_text(amount), "description": f"SYN CALC N{self.serial}"})
        payload = simplefin_payload([])
        remote = payload["accounts"][0]
        remote["transactions"] = list(self.feed)
        remote["balance"] = decimal_text(balance[1])
        remote["balance-date"] = simplefin_epoch(balance[0])
        with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
            sync_connection(self.owner, self.connection.pk, ignore_rate_limit=True)

    def nth(self, queryset, index):
        rows = list(queryset.order_by("pk"))
        return rows[index % len(rows)] if rows else None

    def active(self):
        return Transaction.objects.filter(account=self.account, status=Transaction.Status.ACTIVE)

    def manual_balances(self):
        return BalanceSnapshot.objects.filter(account=self.account, source=MANUAL)

    def apply(self, step):
        """Run one step; returns the real balance rows it may change, or None for any."""
        kind, *args = step
        if kind == "import":
            if self.case.feed == "simplefin":
                last = max(_anchor(self.account).snapshot_date if _anchor(self.account) else CUTOVER, CUTOVER)
                self.sync(args[0], (last, 0))  # a later sync: late rows, and a balance on a known day
                return None
            self.import_csv(args[0])
        elif kind == "manual_entry":
            add_manual_transaction(self.owner, self.account.pk, transaction_date=args[0], amount_minor=args[1],
                                   description=f"SYN CASH N{self.next_id()}")
        elif kind == "delete_entry":
            entry = self.nth(self.active().filter(import_batch__source=ImportBatch.Source.MANUAL), args[0])
            if entry is not None:
                delete_manual_transaction(self.owner, entry.pk)
        elif kind == "undo":
            batch = self.nth(ImportBatch.objects.filter(account=self.account, status=ImportBatch.Status.ACTIVE)
                             .exclude(source=ImportBatch.Source.MANUAL), args[0])
            if batch is not None:
                undo_import_batch(self.owner, self.account.pk, batch.pk)
                return None  # a SimpleFIN batch takes the balances it recorded with it
        elif kind == "edit":
            # A split's amount is fixed by its parts, so the edit page refuses to change it.
            row = self.nth(self.active().exclude(category_source=Transaction.CategorySource.SPLIT), args[0])
            if row is not None:
                response = self.client.post(reverse("transaction-edit", args=[row.pk]), {
                    "transaction_date": args[1].isoformat(), "description": row.description,
                    "amount": decimal_text(args[2]),
                })
                assert response.status_code == 302
        elif kind == "record_balance":
            record_manual_snapshot(self.owner, self.account.pk, snapshot_date=args[0], amount_minor=args[1])
            return {MANUAL}
        elif kind == "update_balance":
            target = self.nth(self.manual_balances(), args[0])
            if target is not None:
                try:
                    update_manual_snapshot(self.owner, self.account.pk, target.pk, snapshot_date=args[1],
                                           amount_minor=args[2])
                except SnapshotError:
                    pass  # another manual balance already holds that date; nothing changed
                return {MANUAL}
        elif kind == "delete_balance":
            target = self.nth(self.manual_balances(), args[0])
            if target is not None:
                delete_manual_snapshot(self.owner, self.account.pk, target.pk)
                return {MANUAL}
        return set()


def _only_after_anchor(world, step):
    anchor = _anchor(world.account)
    return (
        anchor is not None
        and world.case.feed == "csv"
        and step[0] in ("import", "manual_entry")
        and all(day > anchor.snapshot_date for day, _ in (step[1] if step[0] == "import" else [step[1:]]))
    )


@given(cases())
def test_saved_calculated_balances_always_equal_a_fresh_calculation(case):
    with ledger.isolated():
        world = World(case)
        try:
            save_calculated_balances(world.owner, world.account.pk)
        except CalculationError:
            pass  # nothing saved, so nothing may appear later either
        assert stored(world.account) == expected(world.account)
        for step in case.steps:
            before_real = real_balances(world.account)
            before_calculated = stored_with_ids(world.account)
            after_anchor_only = _only_after_anchor(world, step)

            may_change = world.apply(step)

            if before_calculated:
                assert stored(world.account) == expected(world.account), step
            else:
                # Calculating is the member's choice: no saved rows, none appear on their own.
                assert stored(world.account) == [], step
            if after_anchor_only:
                assert stored_with_ids(world.account) == before_calculated, step
            if may_change is not None:
                untouched = {row for row in before_real if row[1] not in may_change}
                assert untouched <= real_balances(world.account), step
