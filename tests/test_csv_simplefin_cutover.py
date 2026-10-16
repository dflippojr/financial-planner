"""CSV rows on or after a SimpleFIN cut-over are refused, so a linked purchase counts once (#498)."""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from finance.category_services import income_and_spending_totals
from finance.models import AccountLink, ImportBatch, Transaction
from finance.simplefin_services import sync_connection
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
