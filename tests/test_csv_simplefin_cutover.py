"""CSV rows on or after a SimpleFIN cut-over are refused, so a linked purchase counts once (#498)."""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from finance.category_services import income_and_spending_totals
from finance.csv_import.parser import Mapping, preview_csv, read_csv
from finance.csv_import.profiles import APPLE_CARD_MAPPING, CAPITAL_ONE_MAPPING
from finance.csv_import.services import classify_overlap
from finance.models import Account, AccountLink, ImportBatch, Transaction
from finance.simplefin_services import default_cutover_date, sync_connection
from tests.properties import harness as ledger
from tests.properties.strategies import Row
from tests.test_simplefin import (
    _linked_owner_and_checking,
    account_payload,
    make_account,
    make_household,
    make_person,
    posted_txn,
    signed_in,
)

CUTOVER = date(2026, 3, 11)
COVERED = "Covered by SimpleFIN from 2026-03-11."
CSV = (
    b"When,Memo,Amount,Currency\n"
    b"03/10/2026,SYNTHETIC BEFORE CUTOVER,-2.00,USD\n"
    b"03/16/2026,SYNTHETIC STREAM PURCHASE,-3.00,USD\n"
)


@pytest.fixture
def staging(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def _linked(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(transactions=[posted_txn(txn_id="sf-1", day=16, amount="-3.00")])
    _owner, _checking, connection = _linked_owner_and_checking(
        monkeypatch, owner=owner, payload=payload, cutover=CUTOVER, account=checking
    )
    return owner, checking, connection


def _csv_import(owner, account):
    client = signed_in(owner)
    url = reverse("csv-import-preview", args=(account.pk,))
    token = client.post(url, {
        "action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
    }).context["mapping_form"].initial["token"]
    fields = {
        "token": token, "date_column": "When", "description_column": "Memo",
        "date_format": "mdy_slash_4", "number_format": "dot_comma", "amount_mode": "signed",
        "amount_column": "Amount", "debit_column": "", "credit_column": "", "currency_column": "Currency",
    }
    preview = client.post(url, {**fields, "action": "preview"}).context["preview"]
    committed = client.post(url, {
        **fields, "action": "commit", "source": "huntington",
        "date_range_start": "2026-03-01", "date_range_end": "2026-03-31",
    })
    assert committed.status_code == 302
    return preview


def _march_spending(owner):
    return income_and_spending_totals(owner, date_from=date(2026, 3, 1), date_to=date(2026, 3, 31)).spending_minor


@pytest.mark.django_db
@pytest.mark.parametrize("sync_first", [True, False], ids=["sync-then-csv", "csv-then-sync"])
def test_a_linked_purchase_counts_once_in_either_order(staging, monkeypatch, sync_first):
    owner, checking, connection = _linked(monkeypatch)
    if sync_first:
        assert sync_connection(owner, connection.pk, ignore_rate_limit=True)["imported"] == 1

    preview = _csv_import(owner, checking)
    if not sync_first:
        assert sync_connection(owner, connection.pk, ignore_rate_limit=True)["imported"] == 1

    assert [row.overlap_status for row in preview.rows] == ["new", "invalid"]
    assert preview.rows[1].errors == (COVERED,)
    active = Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE)
    assert sorted(active.values_list("import_batch__source", "transaction_date")) == [
        (ImportBatch.Source.HUNTINGTON, date(2026, 3, 10)),
        (ImportBatch.Source.SIMPLEFIN, date(2026, 3, 16)),
    ]
    assert _march_spending(owner) == 500


@pytest.mark.django_db
def test_a_balances_only_link_refuses_no_csv_rows(staging, monkeypatch):
    owner, checking, _connection = _linked(monkeypatch)
    AccountLink.objects.filter(account=checking).update(mode=AccountLink.Mode.BALANCES_ONLY)

    preview = _csv_import(owner, checking)

    assert [row.overlap_status for row in preview.rows] == ["new", "new"]
    assert Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).count() == 2
    assert _march_spending(owner) == 500


# Card rows are dated by Transaction Date but SimpleFIN by posted date, so the
# cut-over compares a card row's posted date when its profile has one (#568).

CAPITAL_ONE_HEADER = "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\r\n"
APPLE_CARD_HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\r\n"
)


def _card(owner):
    ledger.make_household(owner)
    return ledger.make_account(owner, Account.Type.CREDIT_CARD)


def _card_preview(account, content, mapping):
    return classify_overlap(account, preview_csv(read_csv(content), mapping))


def _active_card_rows(account):
    return sorted(
        Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE)
        .values_list("import_batch__source", "transaction_date", "amount_minor")
    )


@pytest.mark.django_db
def test_a_capital_one_purchase_posting_after_a_default_cutover_counts_once():
    owner = ledger.make_member()
    card = _card(owner)
    csv_file = (CAPITAL_ONE_HEADER
                + "2026-08-27,2026-08-30,0000,SYN STORE,Synthetic,42.00,\r\n"
                + "2026-08-28,2026-08-28,0000,SYN CAFE,Synthetic,10.00,\r\n").encode()
    ledger.import_file(owner, card, csv_file, profile="capital_one")

    connection = ledger.link_simplefin(owner, card, None)
    ledger.sync(owner, connection, [("store", Row(date(2026, 8, 30), -4200, "SYN STORE PURCHASE", "", "")),
                                    ("cafe", Row(date(2026, 8, 28), -1000, "SYN CAFE PURCHASE", "", ""))])

    assert ledger.totals(owner, [card]).spending == 5200
    assert _active_card_rows(card) == [
        (ImportBatch.Source.CAPITAL_ONE, date(2026, 8, 27), -4200),
        (ImportBatch.Source.CAPITAL_ONE, date(2026, 8, 28), -1000),
    ]
    assert AccountLink.objects.get(account=card).cutover_date == date(2026, 8, 31)


@pytest.mark.django_db
def test_a_capital_one_row_posted_on_or_after_the_cutover_is_refused_after_a_sync():
    owner = ledger.make_member()
    card = _card(owner)
    connection = ledger.link_simplefin(owner, card, date(2026, 8, 31))
    ledger.sync(owner, connection, [("store", Row(date(2026, 9, 2), -4200, "SYN STORE PURCHASE", "", ""))])
    csv_file = (CAPITAL_ONE_HEADER + "2026-08-30,2026-09-02,0000,SYN STORE,Synthetic,42.00,\r\n").encode()
    ledger.import_file(owner, card, csv_file, profile="capital_one")

    assert ledger.totals(owner, [card]).spending == 4200
    assert _active_card_rows(card) == [(ImportBatch.Source.SIMPLEFIN, date(2026, 9, 2), -4200)]
    preview = _card_preview(card, csv_file, CAPITAL_ONE_MAPPING)
    assert [row.overlap_status for row in preview.rows] == ["invalid"]
    assert preview.rows[0].errors == ("Covered by SimpleFIN from 2026-08-31.",)
    assert preview.rows[0].transaction_date == date(2026, 8, 30)


@pytest.mark.django_db
def test_an_apple_card_row_is_compared_by_clearing_date_or_its_transaction_date_when_blank():
    owner = ledger.make_member()
    card = _card(owner)
    ledger.link_simplefin(owner, card, date(2026, 8, 31))
    csv_file = (APPLE_CARD_HEADER
                + "08/30/2026,09/01/2026,SYNTHETIC,SYN CLEARED LATE,Synthetic,Purchase,42.00,Synthetic Member\r\n"
                + "08/30/2026,,SYNTHETIC,SYN NOT CLEARED,Synthetic,Purchase,10.00,Synthetic Member\r\n"
                + "08/31/2026,,SYNTHETIC,SYN NOT CLEARED ON,Synthetic,Purchase,5.00,Synthetic Member\r\n"
                + "08/29/2026,08/30/2026,SYNTHETIC,SYN CLEARED,Synthetic,Purchase,3.00,Synthetic Member\r\n").encode()

    preview = _card_preview(card, csv_file, APPLE_CARD_MAPPING)

    assert [row.overlap_status for row in preview.rows] == ["invalid", "new", "invalid", "new"]
    assert preview.rows[0].errors == ("Covered by SimpleFIN from 2026-08-31.",)
    assert [row.transaction_date for row in preview.rows] == [
        date(2026, 8, 30), date(2026, 8, 30), date(2026, 8, 31), date(2026, 8, 29),
    ]


@pytest.mark.django_db
def test_the_default_cutover_follows_the_latest_card_posted_date():
    owner = ledger.make_member()
    capital_one, apple_card = _card(owner), ledger.make_account(owner, Account.Type.CREDIT_CARD)
    ledger.import_file(owner, capital_one, (CAPITAL_ONE_HEADER
                                            + "2026-08-20,2026-08-25,0000,SYN STORE,Synthetic,42.00,\r\n"
                                            + "2026-08-22,2026-08-23,0000,SYN CAFE,Synthetic,10.00,\r\n").encode(),
                       profile="capital_one")
    ledger.import_file(owner, apple_card, (APPLE_CARD_HEADER
                                           + "08/20/2026,08/24/2026,SYNTHETIC,SYN A,Synthetic,Purchase,4.00,M\r\n"
                                           + "08/22/2026,,SYNTHETIC,SYN B,Synthetic,Purchase,1.00,M\r\n").encode(),
                       profile="apple_card")

    assert default_cutover_date(capital_one) == date(2026, 8, 26)
    assert default_cutover_date(apple_card) == date(2026, 8, 25)


@pytest.mark.django_db
def test_a_generic_import_keeps_comparing_its_mapped_date():
    owner = ledger.make_member()
    card = _card(owner)
    ledger.link_simplefin(owner, card, date(2026, 8, 31))
    csv_file = (CAPITAL_ONE_HEADER + "2026-08-30,2026-09-02,0000,SYN STORE,Synthetic,42.00,\r\n").encode()
    generic = Mapping(date_column="Transaction Date", description_column="Description", date_format="iso",
                      number_format="dot_none", amount_mode="separate", debit_column="Debit", credit_column="Credit")

    preview = _card_preview(card, csv_file, generic)

    assert [row.overlap_status for row in preview.rows] == ["new"]
