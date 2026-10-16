"""Ledger invariants checked against generated statements, not hand-picked examples (#533).

Each property states a rule from AGENTS.md "Data and correctness" and lets
Hypothesis search synthetic exports (docs/csv-shapes.md) and SimpleFIN feeds for
a counterexample. Expected values come from the generated real-world
transactions, never from the code under test. A property that an open bug
still breaks is marked xfail(strict=True) with that bug's number, so it turns
red as soon as the bug is fixed and the marker is left behind.
"""

import csv
import io
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from hypothesis import example, given
from hypothesis import strategies as st

from finance.cash_flow import selected_accounts
from finance.category_services import confirm_transfer_pair, link_refund
from finance.csv_import.parser import Mapping
from finance.export import minor_from_decimal_string, money_decimal
from finance.models import Account, ImportBatch, TransferPair
from finance.transaction_export import iter_transaction_csv
from finance.transaction_filters import visible_transaction_queryset

from . import harness as ledger
from .strategies import (
    CAPITAL_ONE,
    HUNTINGTON,
    MAX_MINOR,
    Row,
    overlapping_files,
    payee_memo,
    rows,
    shapes,
    shifted,
)

pytestmark = [pytest.mark.property, pytest.mark.django_db]


def _key(shape, row):
    """What the ledger should hold for one source row."""
    return (row.day, row.amount_minor, "USD", shape.stored_description(row))


def _new_member_with_account(shape, username="owner"):
    owner = ledger.make_member(username)
    ledger.make_household(owner)
    return owner, ledger.make_account(owner, shape.account_type)


# 1. Reimport is idempotent.

@st.composite
def reimport_cases(draw):
    shape = draw(shapes)
    pool = draw(rows(max_size=8))
    earlier = draw(overlapping_files(pool, min_files=0, max_files=2))
    target = draw(overlapping_files(pool, min_files=1, max_files=1))[0]
    return shape, earlier, target


@given(reimport_cases())
def test_importing_the_same_file_twice_changes_nothing(case):
    shape, earlier, target = case
    with ledger.isolated():
        owner, account = _new_member_with_account(shape)
        for file in earlier:
            ledger.import_rows(owner, account, shape, file)
        ledger.import_rows(owner, account, shape, target)
        before = ledger.ledger_state(owner, [account])

        again = ledger.import_rows(owner, account, shape, target)

        assert again.new_count == 0
        assert ledger.ledger_state(owner, [account]) == before
        # The first import kept every row of the file, including identical ones.
        assert not Counter(_key(shape, row) for row in target) - before["rows"]


# 2. Order doesn't matter.

@st.composite
def order_cases(draw):
    shape = draw(shapes)
    pool = draw(rows(max_size=6))
    files = draw(overlapping_files(pool))
    return shape, files, draw(st.permutations(files))


@given(order_cases())
def test_overlapping_files_give_the_same_ledger_in_any_order(case):
    shape, files, reordered = case
    with ledger.isolated():
        # Separate members, so a transfer can never pair one ledger's row with the other's.
        first_owner, first = _new_member_with_account(shape, "first")
        second_owner, second = _new_member_with_account(shape, "second")
        for file in files:
            ledger.import_rows(first_owner, first, shape, file)
        for file in reordered:
            ledger.import_rows(second_owner, second, shape, file)

        # A row repeated k times in one file is k transactions; the same row in
        # another file is the same transactions again, never extra ones.
        expected = Counter()
        for file in files:
            expected |= Counter(_key(shape, row) for row in file)
        assert ledger.content_counter([first]) == ledger.content_counter([second]) == expected
        assert ledger.per_account_sums([first])[first.pk] == ledger.per_account_sums([second])[second.pk]


# 3. No double counting across a SimpleFIN cut-over.

@dataclass(frozen=True)
class CutoverCase:
    cutover: date
    world: tuple
    csv_files: tuple
    feeds: tuple
    steps: tuple


def _feed_row(index, row):
    # SimpleFIN describes a transaction differently from the bank's CSV, which
    # is why the cut-over, not description matching, keeps them apart.
    return replace(row, payee=f"SYN FEED N{index}", memo="")


@st.composite
def cutover_cases(draw):
    cutover = draw(st.dates(date(2025, 3, 1), date(2026, 8, 1)))
    world = draw(rows(min_size=1, max_size=10, unique=True,
                      day_strategy=st.integers(-45, 45).map(lambda offset: shifted(cutover, offset))))
    file_count = draw(st.integers(1, 3))
    feed_count = draw(st.integers(1, 3))
    csv_files = [[] for _ in range(file_count)]
    feeds = [[] for _ in range(feed_count)]
    for index, row in enumerate(world):
        before = row.day < cutover
        # The side that owns a date always has the transaction; the other side may repeat it.
        if before or draw(st.booleans()):
            for file in draw(st.sets(st.integers(0, file_count - 1), min_size=1)):
                csv_files[file].append(row)
        if not before or draw(st.booleans()):
            for feed in draw(st.sets(st.integers(0, feed_count - 1), min_size=1)):
                feeds[feed].append((index, _feed_row(index, row)))
    operations = [("csv", i) for i in range(file_count)] + [("sync", i) for i in range(feed_count)]
    repeats = draw(st.lists(st.sampled_from(operations), max_size=2))
    steps = draw(st.permutations(operations + repeats))
    return CutoverCase(cutover, tuple(world), tuple(map(tuple, csv_files)), tuple(map(tuple, feeds)), tuple(steps))


@given(cutover_cases())
def test_a_cutover_counts_each_transaction_once_in_any_mix_of_csv_and_simplefin(case):
    with ledger.isolated():
        owner = ledger.make_member()
        ledger.make_household(owner)
        account = ledger.make_account(owner, Account.Type.CHECKING)
        connection = ledger.link_simplefin(owner, account, case.cutover)
        for kind, index in case.steps:
            if kind == "csv":
                ledger.import_rows(owner, account, HUNTINGTON, list(case.csv_files[index]))
            else:
                ledger.sync(owner, connection, case.feeds[index])

        expected_rows = Counter(
            (row.day, row.amount_minor, "USD",
             payee_memo(row) if row.day < case.cutover else _feed_row(index, row).payee)
            for index, row in enumerate(case.world)
        )
        assert ledger.content_counter([account]) == expected_rows
        expected = ledger.expected_totals([row.amount_minor for row in case.world])
        assert ledger.totals(owner, [account]) == expected
        assert ledger.report_income_and_spending(owner, [account]) == (expected.income, expected.spending)


# 4. Transfers net to zero; a refund never reduces spending that was never counted.

ACCOUNT_SHAPES = {"checking": HUNTINGTON, "savings": HUNTINGTON, "card": CAPITAL_ONE}
ACCOUNT_TYPES = {"checking": Account.Type.CHECKING, "savings": Account.Type.SAVINGS, "card": Account.Type.CREDIT_CARD}


@dataclass(frozen=True)
class Transfer:
    source: str
    target: str
    amount_minor: int
    day: date
    lag: int

    def legs(self):
        out_name = "SYN CARD PAYMENT" if self.target == "card" else f"SYN TRANSFER TO {self.target.upper()}"
        return (
            (self.source, Row(self.day, -self.amount_minor, out_name, "", "")),
            (self.target, Row(shifted(self.day, self.lag), self.amount_minor,
                              f"SYN TRANSFER FROM {self.source.upper()}", "", "")),
        )


@st.composite
def transfer_cases(draw):
    sizes = draw(st.lists(st.integers(1, MAX_MINOR), min_size=1, max_size=4, unique=True))
    transfers = []
    for size in sizes:
        source, target = draw(st.permutations(tuple(ACCOUNT_SHAPES)))[:2]
        day = draw(st.dates(date(2026, 1, 1), date(2026, 8, 31)))
        # Within the default five-day matching window.
        transfers.append(Transfer(source, target, size, day, draw(st.integers(0, 3))))
    # Ordinary activity never shares an absolute amount with a transfer or with
    # other ordinary activity, so the intended pairs are the only possible matches.
    background = draw(rows(min_size=0, max_size=6))
    distinct = draw(st.lists(st.integers(1, MAX_MINOR).filter(lambda size: size not in sizes),
                             min_size=len(background), max_size=len(background), unique=True))
    background = [replace(row, amount_minor=size if row.amount_minor > 0 else -size)
                  for row, size in zip(background, distinct)]
    placed = [(draw(st.sampled_from(tuple(ACCOUNT_SHAPES))), row) for row in background]
    return tuple(transfers), tuple(placed)


def _household_ledger(owner, placed_rows):
    """Household accounts for checking, savings and card, each imported from its own export."""
    household = ledger.make_household(owner)
    accounts = {
        key: ledger.make_account(owner, ACCOUNT_TYPES[key], name=f"Synthetic {key.title()}", household=household)
        for key in ACCOUNT_SHAPES
    }
    for key, shape in ACCOUNT_SHAPES.items():
        file = [replace(row, source_id=f"SYN-{i:05d}") for i, (where, row) in enumerate(placed_rows) if where == key]
        ledger.import_rows(owner, accounts[key], shape, file)
    for pair in TransferPair.objects.filter(status=TransferPair.Status.SUGGESTED):
        confirm_transfer_pair(owner, pair.pk)
    return accounts


@given(transfer_cases())
def test_matched_transfers_and_card_payments_add_nothing_to_household_cash_flow(case):
    transfers, background = case
    legs = [leg for transfer in transfers for leg in transfer.legs()]
    with ledger.isolated():
        owner = ledger.make_member()
        _household_ledger(owner, list(background) + legs)

        assert TransferPair.objects.excluding_income_and_spending().count() == len(transfers)
        expected = ledger.expected_totals([row.amount_minor for _where, row in background])
        household = selected_accounts(owner, scope=Account.Scope.HOUSEHOLD, cash_flow_only=True)
        assert ledger.totals(owner, household) == expected
        assert ledger.totals(owner) == expected
        assert ledger.report_income_and_spending(owner, household) == (expected.income, expected.spending)


@st.composite
def refund_cases(draw, transfer_leg=True):
    transfer = draw(st.integers(1, MAX_MINOR))
    other = st.integers(1, MAX_MINOR).filter(lambda size: size != transfer)
    purchases = draw(st.lists(other, min_size=1, max_size=4))
    refunds = draw(st.lists(other, min_size=1, max_size=3))
    # Index len(purchases) picks the outgoing transfer leg, which totals already exclude.
    last = len(purchases) if transfer_leg else len(purchases) - 1
    originals = draw(st.lists(st.integers(0, last), min_size=len(refunds), max_size=len(refunds)))
    return transfer, tuple(purchases), tuple(zip(refunds, originals))


def _check_refunds(case):
    transfer, purchases, refunds = case
    day = date(2026, 8, 10)
    checking = [Row(day, -transfer, "SYN TRANSFER TO SAVINGS", "", "")]
    checking += [Row(day, -size, f"SYN PURCHASE N{i}", "", "") for i, size in enumerate(purchases)]
    checking += [Row(day, size, f"SYN REFUND N{i}", "", "") for i, (size, _original) in enumerate(refunds)]
    savings = [Row(day, transfer, "SYN TRANSFER FROM CHECKING", "", "")]
    with ledger.isolated():
        owner = ledger.make_member()
        accounts = _household_ledger(owner, [("checking", row) for row in checking] + [("savings", row) for row in savings])
        assert TransferPair.objects.excluding_income_and_spending().count() == 1

        by_description = dict(ledger.active_rows([accounts["checking"]]).values_list("description", "pk"))
        originals = [by_description[f"SYN PURCHASE N{i}"] for i in range(len(purchases))]
        originals.append(by_description["SYN TRANSFER TO SAVINGS"])
        for i, (_size, original) in enumerate(refunds):
            try:
                link_refund(owner, by_description[f"SYN REFUND N{i}"], originals[original])
            except (PermissionDenied, ValidationError):
                pass

        # A refund of a counted purchase reduces spending, below zero when
        # refunds exceed purchases (a net refund). One aimed at the excluded
        # transfer leg must be refused, so it stays income.
        income = sum(size for size, original in refunds if original == len(purchases))
        spending = sum(purchases) - sum(size for size, original in refunds if original < len(purchases))
        household = selected_accounts(owner, scope=Account.Scope.HOUSEHOLD, cash_flow_only=True)
        assert ledger.totals(owner, household) == ledger.Totals(income, spending, income - spending)


@given(refund_cases(transfer_leg=False))
def test_a_refund_of_a_counted_purchase_reduces_spending_by_its_amount(case):
    _check_refunds(case)


# The failing explicit example ends this test before Hypothesis generates any
# cases, so the property above carries the generated purchase refunds until
# #506 is fixed. Then remove the marker and let this one search both kinds.
@pytest.mark.xfail(strict=True, reason="#506")
@given(refund_cases())
# Hypothesis's shrunk counterexample: a 0.01 refund linked to the excluded
# 0.02 transfer leg erases 0.01 of counted spending.
@example((2, (1,), ((1, 1),)))
def test_a_refund_reduces_spending_only_when_its_original_was_counted(case):
    _check_refunds(case)


# 5. Exact money.

GENERIC_EXPORT_MAPPING = Mapping(
    date_column="date", description_column="description", date_format="iso", number_format="dot_none",
    amount_mode="signed", amount_column="amount_decimal", currency_column="currency",
)


@given(shapes, rows(max_size=8))
def test_amounts_survive_import_export_and_reimport_exactly(shape, source_rows):
    with ledger.isolated():
        owner, account = _new_member_with_account(shape)
        ledger.import_rows(owner, account, shape, source_rows)
        # Each source's sign convention lands as the ledger's: negative is money out.
        imported = Counter(ledger.active_rows([account]).values_list("transaction_date", "amount_minor", "currency"))
        assert imported == Counter((row.day, row.amount_minor, "USD") for row in source_rows)

        exported = b"".join(iter_transaction_csv(visible_transaction_queryset(owner).filter(account=account), owner))
        records = list(csv.DictReader(io.StringIO(exported.decode("utf-8"))))
        for record in records:
            assert Decimal(record["amount_decimal"]) * 100 == int(record["amount_minor"])
        assert Counter((date.fromisoformat(r["date"]), int(r["amount_minor"]), r["currency"]) for r in records) == imported

        copy = ledger.make_account(owner, Account.Type.CHECKING, name="Synthetic Reimport")
        ledger.import_file(owner, copy, exported, mapping=GENERIC_EXPORT_MAPPING, source=ImportBatch.Source.HUNTINGTON)
        reimported = Counter(ledger.active_rows([copy]).values_list("transaction_date", "amount_minor", "currency"))
        assert reimported == imported


@given(st.integers(-(2**63 - 1), 2**63 - 1))
def test_full_export_money_columns_round_trip_every_storable_amount(amount_minor):
    assert minor_from_decimal_string(money_decimal(amount_minor)) == amount_minor
    assert minor_from_decimal_string(format(Decimal(amount_minor) / Decimal(100), ".2f")) == amount_minor
