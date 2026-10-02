import logging
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.category_services import refresh_transfer_pairs
from finance.csv_import.parser import CsvInputError, preview_csv, read_csv
from finance.csv_import.profiles import (
    CAPITAL_ONE_HEADER_ERROR,
    CAPITAL_ONE_MAPPING,
    require_capital_one_headers,
)
from finance.csv_import.services import commit_csv_import
from finance.models import Account, ImportBatch, Person, Transaction, TransferPair
from tests.capital_one_fixtures import NATIVE_CSV, OVERLAP_CSV, capital_one_csv


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def commit(user, account, content):
    document = read_csv(content)
    require_capital_one_headers(document.headers)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=document,
        mapping=CAPITAL_ONE_MAPPING,
        source=ImportBatch.Source.CAPITAL_ONE,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )


def test_native_fixture_uses_crlf_and_expected_headers():
    assert NATIVE_CSV.startswith(
        b"Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\r\n"
    )


def test_capital_one_preview_parses_debits_and_credits_and_rejects_malformed_row():
    preview = preview_csv(read_csv(NATIVE_CSV), CAPITAL_ONE_MAPPING)
    rows = preview.rows

    assert preview.valid_count == 5
    assert preview.invalid_count == 1
    assert [row.transaction_date.isoformat() for row in rows[:5]] == [
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-18",
    ]
    assert [row.amount_minor for row in rows[:5]] == [-4215, 50000, 1500, -525, -525]
    assert [row.currency for row in rows[:5]] == ["USD"] * 5
    assert not rows[5].is_valid


@pytest.mark.django_db
def test_capital_one_import_drops_card_number_but_keeps_other_original_fields():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)

    commit(user, account, NATIVE_CSV)

    purchase = Transaction.objects.get(description="SYNTHETIC GROCER")
    assert "Card No." not in purchase.original_fields
    assert purchase.original_fields["Posted Date"] == "2026-09-16"
    assert purchase.original_fields["Category"] == "Merchandise"
    assert purchase.category is None
    assert purchase.kind == Transaction.Kind.CASH_FLOW
    assert purchase.amount_minor == -4215

    payment = Transaction.objects.get(description="CAPITAL ONE MOBILE PAYMENT")
    assert payment.amount_minor == 50000
    assert "Card No." not in payment.original_fields

    refund = Transaction.objects.get(description="SYNTHETIC REFUND")
    assert refund.amount_minor == 1500


@pytest.mark.django_db
def test_two_identical_same_day_purchases_both_import_and_reimport_adds_nothing():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)

    first = commit(user, account, NATIVE_CSV)
    same = commit(user, account, NATIVE_CSV)
    overlap = commit(user, account, OVERLAP_CSV)

    assert first.new_count == 5
    assert same.new_count == 0
    assert same.duplicate_count == 5
    assert overlap.new_count == 1
    assert overlap.duplicate_count == 2
    assert Transaction.objects.filter(account=account, status="active").count() == 6
    assert Transaction.objects.filter(account=account, description="SYNTHETIC CAFE").count() == 2


def test_missing_or_renamed_capital_one_headers_are_rejected_without_echoing_them():
    secret_header = "PRIVATE-RENAMED-DATE"
    content = (
        f"{secret_header},Posted Date,Card No.,Description,Category,Debit,Credit\r\n"
        "2026-09-15,2026-09-16,1234,SYNTHETIC GROCER,Merchandise,1.00,\r\n"
    ).encode()

    document = read_csv(content)
    with pytest.raises(CsvInputError, match="does not match the Capital One credit card export") as caught:
        require_capital_one_headers(document.headers)

    assert secret_header not in str(caught.value)
    assert CAPITAL_ONE_HEADER_ERROR == str(caught.value)


@pytest.mark.django_db
def test_capital_one_payment_pairs_with_checking_withdrawal_as_a_card_payment():
    user, person = make_person("owner")
    checking = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    card = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
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
        description="Synthetic payment to card",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint="synthetic-checking-leg".ljust(64, "a"),
        original_fields={},
    )

    commit(user, card, NATIVE_CSV)
    refresh_transfer_pairs(user)

    pair = TransferPair.objects.get()
    assert pair.kind == TransferPair.Kind.CARD_PAYMENT


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def upload_capital_one(client, account, content=NATIVE_CSV):
    return client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {
            "action": "upload",
            "import_profile": "capital_one",
            "csv_file": SimpleUploadedFile("synthetic.csv", content, "text/csv"),
        },
    )


@pytest.mark.django_db
def test_capital_one_upload_previews_without_column_mapping_or_card_number(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    client = Client()
    client.force_login(user)

    response = upload_capital_one(client, account)

    assert response.status_code == 200
    preview = response.context["preview"]
    assert preview.valid_count == 5
    assert b"Capital One columns are mapped automatically" in response.content
    assert b"date_column" not in response.content
    assert b"1234" not in response.content
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_capital_one_commit_requires_a_visible_account_and_imports(staging_settings):
    owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=owner)
    owner_client = Client()
    owner_client.force_login(owner_user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))

    viewer = Client()
    viewer.force_login(viewer_user)
    hidden = upload_capital_one(viewer, account)
    missing = viewer.get(reverse("csv-import-preview", args=(account.pk + 999,)))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content

    staged = upload_capital_one(owner_client, account)
    token = staged.context["mapping_form"].data["token"]
    imported = owner_client.post(
        preview_url,
        {
            "action": "commit",
            "token": token,
            "source": "capital_one",
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
        follow=True,
    )

    assert imported.status_code == 200
    assert Transaction.objects.filter(account=account).count() == 5
    batch = ImportBatch.objects.get()
    assert batch.source == ImportBatch.Source.CAPITAL_ONE
    assert not Transaction.objects.filter(account=account, original_fields__has_key="Card No.").exists()


@pytest.mark.django_db
def test_capital_one_wrong_headers_are_not_staged_or_logged(caplog, staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    client = Client()
    client.force_login(user)
    secret = "PRIVATE-SOURCE-ROW-SECRET"
    content = f"When,Memo,Amount\n2026-09-27,{secret},-1.00\n".encode()

    with caplog.at_level(logging.DEBUG):
        response = upload_capital_one(client, account, content)

    assert response.status_code == 200
    assert b"does not match the Capital One credit card export" in response.content
    assert secret.encode() not in response.content
    assert secret not in caplog.text
    assert not list(staging_settings.glob("*.csvstage"))
    assert response.context.get("preview") is None
