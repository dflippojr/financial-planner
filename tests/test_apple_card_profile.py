import logging
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.category_services import income_and_spending_totals, refresh_transfer_pairs
from finance.csv_import.parser import CsvInputError, preview_csv, read_csv
from finance.csv_import.profiles import APPLE_CARD_HEADER_ERROR, APPLE_CARD_MAPPING, require_apple_card_headers
from finance.csv_import.services import categorize_imported_batch, commit_csv_import
from finance.models import Account, ImportBatch, Person, Transaction, TransferPair
from tests.apple_card_fixtures import NATIVE_CSV, OVERLAP_CSV, apple_card_csv


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def commit(user, account, content):
    document = read_csv(content)
    require_apple_card_headers(document.headers)
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
def test_reimporting_the_same_and_overlapping_apple_card_files_adds_no_duplicates():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)

    first = commit(user, account, NATIVE_CSV)
    same = commit(user, account, NATIVE_CSV)
    overlap = commit(user, account, OVERLAP_CSV)

    assert first.new_count == 7
    assert first.invalid_count == 1
    assert same.new_count == 0
    assert same.duplicate_count == 7
    assert overlap.new_count == 1
    assert overlap.duplicate_count == 1
    assert Transaction.objects.filter(account=account, status="active").count() == 8
    duplicated_purchases = Transaction.objects.filter(description="Synthetic Grocery")
    assert duplicated_purchases.count() == 2
    bookstore = Transaction.objects.get(description="Synthetic Bookstore")
    assert bookstore.amount_minor == -1825


def test_missing_or_renamed_apple_card_headers_are_rejected_without_echoing_them():
    secret_header = "PRIVATE-RENAMED-MERCHANT"
    content = (
        f"Transaction Date,Clearing Date,Description,{secret_header},Category,Type,Amount (USD),Purchased By\r\n"
        "09/15/2026,09/15/2026,SYNTHETIC ROW,Synthetic,Shopping,Purchase,1.00,Dana Example\r\n"
    ).encode()

    document = read_csv(content)
    with pytest.raises(CsvInputError, match="does not match the Apple Card export") as caught:
        require_apple_card_headers(document.headers)

    assert secret_header not in str(caught.value)
    assert APPLE_CARD_HEADER_ERROR == str(caught.value)


def test_apple_card_row_errors_omit_raw_cells(caplog):
    secret_date = "PRIVATE-BAD-DATE"
    secret_amount = "PRIVATE-BAD-AMOUNT"
    secret_extra = "PRIVATE-RAGGED-CELL"
    content = apple_card_csv(
        (
            f"{secret_date},09/15/2026,SYNTHETIC ROW,Synthetic,Shopping,Purchase,1.00,Dana Example",
            f"09/15/2026,09/15/2026,SYNTHETIC ROW,Synthetic,Shopping,Purchase,{secret_amount},Dana Example",
            f"09/15/2026,09/15/2026,{secret_extra}",
        )
    )

    with caplog.at_level(logging.DEBUG):
        preview = preview_csv(read_csv(content), APPLE_CARD_MAPPING)

    messages = " ".join(error for row in preview.rows for error in row.errors)
    assert preview.invalid_count == 3
    assert "Date does not match the selected format." in preview.rows[0].errors
    assert "Amount is not valid" in preview.rows[1].errors[0]
    assert "different number of columns" in preview.rows[2].errors[0]
    assert secret_date not in messages
    assert secret_amount not in messages
    assert secret_extra not in messages
    assert secret_date not in caplog.text
    assert secret_amount not in caplog.text
    assert secret_extra not in caplog.text


@pytest.mark.django_db
def test_card_payment_pairs_with_an_equal_and_opposite_checking_withdrawal():
    user, person = make_person("owner")
    checking = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    card = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)

    payment_only = apple_card_csv(
        ("09/16/2026,09/16/2026,SYNTHETIC CARD PAYMENT THANK YOU,Synthetic Card Payment,Payments,Payment,-500.00,Dana Example",)
    )
    document = read_csv(payment_only)
    require_apple_card_headers(document.headers)
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


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def upload_apple_card(client, account, content=NATIVE_CSV):
    return client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {
            "action": "upload",
            "import_profile": "apple_card",
            "csv_file": SimpleUploadedFile("synthetic.csv", content, "text/csv"),
        },
    )


@pytest.mark.django_db
def test_apple_card_upload_previews_without_column_mapping(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    client = Client()
    client.force_login(user)

    response = upload_apple_card(client, account)

    assert response.status_code == 200
    preview = response.context["preview"]
    assert preview.valid_count == 7
    assert preview.rows[0].description == "Synthetic Coffee"
    assert b"Apple Card columns are mapped automatically" in response.content
    assert b"date_column" not in response.content
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_apple_card_commit_requires_a_visible_account_and_imports(staging_settings):
    owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=owner)
    owner_client = Client()
    owner_client.force_login(owner_user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))

    viewer = Client()
    viewer.force_login(viewer_user)
    hidden = upload_apple_card(viewer, account)
    missing = viewer.get(reverse("csv-import-preview", args=(account.pk + 999,)))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content

    staged = upload_apple_card(owner_client, account)
    token = staged.context["mapping_form"].data["token"]
    imported = owner_client.post(
        preview_url,
        {
            "action": "commit",
            "token": token,
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
        follow=True,
    )

    assert imported.status_code == 200
    assert Transaction.objects.filter(account=account).count() == 7
    batch = ImportBatch.objects.get()
    assert batch.source == ImportBatch.Source.APPLE_CARD
    assert Transaction.objects.get(description="Synthetic Interest").amount_minor == -275


@pytest.mark.django_db
def test_apple_card_wrong_headers_are_not_staged_or_logged(caplog, staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    client = Client()
    client.force_login(user)
    secret = "PRIVATE-SOURCE-ROW-SECRET"
    content = f"When,Who,Amount\n09/27/2026,{secret},-1.00\n".encode()

    with caplog.at_level(logging.DEBUG):
        response = upload_apple_card(client, account, content)

    assert response.status_code == 200
    assert b"does not match the Apple Card export" in response.content
    assert secret.encode() not in response.content
    assert secret not in caplog.text
    assert not list(staging_settings.glob("*.csvstage"))
    assert response.context.get("preview") is None


@pytest.mark.django_db
def test_apple_card_get_restores_preview_and_commit_needs_a_date_range(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    staged = upload_apple_card(client, account)
    token = staged.context["mapping_form"].data["token"]

    restored = client.get(preview_url)
    assert restored.status_code == 200
    assert restored.context["preview"].valid_count == 7
    assert b"Apple Card columns are mapped automatically" in restored.content

    reversed_range = client.post(
        preview_url,
        {
            "action": "commit",
            "token": token,
            "date_range_start": "2026-09-30",
            "date_range_end": "2026-09-01",
        },
    )
    assert reversed_range.status_code == 200
    assert not Transaction.objects.exists()
    assert b"end on or after it starts" in reversed_range.content
