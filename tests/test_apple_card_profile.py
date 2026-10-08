from datetime import date

import pytest
from django.contrib.auth import get_user_model

from finance.category_services import income_and_spending_totals, refresh_transfer_pairs
from finance.csv_import.parser import preview_csv, read_csv
from finance.csv_import.profiles import (
    APPLE_CARD_HEADER_ERROR,
    APPLE_CARD_HEADERS,
    APPLE_CARD_MAPPING,
    require_headers,
)
from finance.csv_import.services import categorize_imported_batch, commit_csv_import
from finance.models import Account, ImportBatch, Person, Transaction, TransferPair
from tests.apple_card_fixtures import NATIVE_CSV, apple_card_csv

PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def commit(user, account, content):
    document = read_csv(content)
    require_headers(document.headers, APPLE_CARD_HEADERS, APPLE_CARD_HEADER_ERROR)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=document,
        mapping=APPLE_CARD_MAPPING,
        source=ImportBatch.Source.APPLE_CARD,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )


def test_native_fixture_uses_crlf_and_expected_headers():
    assert NATIVE_CSV.startswith(
        b"Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\r\n"
    )


def test_apple_card_preview_maps_merchant_description_and_inverts_signed_amounts():
    preview = preview_csv(read_csv(NATIVE_CSV), APPLE_CARD_MAPPING)
    valid_rows = [row for row in preview.rows if row.is_valid]

    assert preview.valid_count == 7
    assert preview.invalid_count == 1
    assert [row.transaction_date.isoformat() for row in valid_rows] == [
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-19",
        "2026-09-19",
        "2026-09-20",
    ]
    assert [row.amount_minor for row in valid_rows] == [-450, 50000, 1500, -275, -6210, -6210, -3000]
    assert [row.description for row in valid_rows] == [
        "Synthetic Coffee",
        "Synthetic Card Payment",
        "Synthetic Refund",
        "Synthetic Interest",
        "Synthetic Grocery",
        "Synthetic Grocery",
        "Synthetic, Store",
    ]
    assert all(row.currency == "USD" for row in valid_rows)
    assert all(row.source_transaction_id == "" for row in valid_rows)
    malformed = [row for row in preview.rows if not row.is_valid][0]
    assert "different number of columns" in malformed.errors[0]


@pytest.mark.django_db
def test_card_payment_pairs_with_an_equal_and_opposite_checking_withdrawal():
    user, person = make_person("owner")
    checking = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    card = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)

    payment_only = apple_card_csv(
        ("09/16/2026,09/16/2026,SYNTHETIC CARD PAYMENT THANK YOU,Synthetic Card Payment,Payments,Payment,-500.00,Dana Example",)
    )
    document = read_csv(payment_only)
    require_headers(document.headers, APPLE_CARD_HEADERS, APPLE_CARD_HEADER_ERROR)
    result = commit_csv_import(
        user,
        card.pk,
        content=payment_only,
        document=document,
        mapping=APPLE_CARD_MAPPING,
        source=ImportBatch.Source.APPLE_CARD,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )
    categorize_imported_batch(user, result.batch)
    checking_batch = ImportBatch.objects.create(
        account=checking,
        imported_by=person,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )
    Transaction.objects.create(
        account=checking,
        import_batch=checking_batch,
        transaction_date=date(2026, 9, 16),
        amount_minor=-50000,
        currency="USD",
        description="Synthetic card payment outflow",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint="synthetic-checking-leg",
        original_fields={"Synthetic": "row"},
    )

    refresh_transfer_pairs(person)
    pair = TransferPair.objects.get()
    totals = income_and_spending_totals(person)

    assert pair.kind == TransferPair.Kind.CARD_PAYMENT
    assert totals.spending_minor == 0
    assert totals.income_minor == 0
