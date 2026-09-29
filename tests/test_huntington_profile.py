import logging
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.csv_import.parser import CsvInputError, preview_csv, read_csv
from finance.csv_import.profiles import HUNTINGTON_HEADER_ERROR, HUNTINGTON_MAPPING, require_huntington_headers
from finance.csv_import.services import commit_csv_import
from finance.models import Account, ImportBatch, Person, Transaction
from tests.huntington_fixtures import NATIVE_CSV, OVERLAP_CSV, huntington_csv


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def commit(user, account, content):
    document = read_csv(content)
    require_huntington_headers(document.headers)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=document,
        mapping=HUNTINGTON_MAPPING,
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )


def test_native_fixture_uses_crlf_and_expected_headers():
    assert NATIVE_CSV.startswith(
        b"Date,Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number\r\n"
    )


def test_huntington_preview_joins_description_and_keeps_signed_amounts():
    preview = preview_csv(read_csv(NATIVE_CSV), HUNTINGTON_MAPPING)
    rows = preview.rows

    assert preview.valid_count == 3
    assert [row.transaction_date.isoformat() for row in rows] == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert [row.amount_minor for row in rows] == [-4215, -300, 150000]
    assert [row.description for row in rows] == [
        "SYNTHETIC GROCER - WEEKLY FOOD",
        "ATM WITHDRAWAL FEE",
        "SYNTHETIC PAYROLL - DIRECT DEPOSIT",
    ]
    assert [row.source_transaction_id for row in rows] == [
        "100000000000000001",
        "100000000000000002",
        "100000000000000003",
    ]


@pytest.mark.django_db
def test_huntington_import_stores_source_id_and_does_not_match_on_it():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)

    first = commit(user, account, NATIVE_CSV)
    stored = list(Transaction.objects.filter(account=account).order_by("transaction_date", "pk"))
    relabeled = huntington_csv(
        (
            "09/15/2026,9,SYNTHETIC GROCER,WEEKLY FOOD,-42.15,,999999999999999999",
            "09/16/2026,2,,ATM WITHDRAWAL FEE,-3.00,,100000000000000002",
            "09/17/2026,1,SYNTHETIC PAYROLL,DIRECT DEPOSIT,1500.00,,100000000000000003",
        )
    )
    second = commit(user, account, relabeled)

    assert first.new_count == 3
    assert second.new_count == 0
    assert second.duplicate_count == 3
    assert [row.source_transaction_id for row in stored] == [
        "100000000000000001",
        "100000000000000002",
        "100000000000000003",
    ]
    assert Transaction.objects.filter(account=account, status="active").count() == 3


@pytest.mark.django_db
def test_reimporting_the_same_and_overlapping_huntington_files_adds_no_duplicates():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)

    commit(user, account, NATIVE_CSV)
    same = commit(user, account, NATIVE_CSV)
    overlap = commit(user, account, OVERLAP_CSV)

    assert same.new_count == 0
    assert same.duplicate_count == 3
    assert overlap.new_count == 1
    assert overlap.duplicate_count == 1
    assert Transaction.objects.filter(account=account, status="active").count() == 4
    cafe = Transaction.objects.get(description="SYNTHETIC CAFE - COFFEE")
    assert cafe.amount_minor == -525
    assert cafe.source_transaction_id == "100000000000000004"


def test_missing_or_renamed_huntington_headers_are_rejected_without_echoing_them():
    secret_header = "PRIVATE-RENAMED-DATE"
    content = (
        f"{secret_header},Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number\r\n"
        "09/15/2026,1,SYNTHETIC GROCER,WEEKLY FOOD,-1.00,,1\r\n"
    ).encode()

    document = read_csv(content)
    with pytest.raises(CsvInputError, match="does not match the Huntington checking export") as caught:
        require_huntington_headers(document.headers)

    assert secret_header not in str(caught.value)
    assert HUNTINGTON_HEADER_ERROR == str(caught.value)


def test_huntington_row_errors_omit_raw_cells(caplog):
    secret_date = "PRIVATE-BAD-DATE"
    secret_amount = "PRIVATE-BAD-AMOUNT"
    secret_extra = "PRIVATE-RAGGED-CELL"
    content = huntington_csv(
        (
            f"{secret_date},1,SYNTHETIC GROCER,WEEKLY FOOD,-1.00,,1",
            f"09/15/2026,1,SYNTHETIC GROCER,WEEKLY FOOD,{secret_amount},,2",
            f"09/15/2026,1,{secret_extra}",
        )
    )

    with caplog.at_level(logging.DEBUG):
        preview = preview_csv(read_csv(content), HUNTINGTON_MAPPING)

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


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def upload_huntington(client, account, content=NATIVE_CSV):
    return client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {
            "action": "upload",
            "import_profile": "huntington",
            "csv_file": SimpleUploadedFile("synthetic.csv", content, "text/csv"),
        },
    )


@pytest.mark.django_db
def test_huntington_upload_previews_without_column_mapping(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)

    response = upload_huntington(client, account)

    assert response.status_code == 200
    preview = response.context["preview"]
    assert preview.valid_count == 3
    assert preview.rows[0].description == "SYNTHETIC GROCER - WEEKLY FOOD"
    assert b"Huntington checking columns are mapped automatically" in response.content
    assert b"date_column" not in response.content
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_huntington_commit_requires_a_visible_account_and_imports(staging_settings):
    owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=owner)
    owner_client = Client()
    owner_client.force_login(owner_user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))

    viewer = Client()
    viewer.force_login(viewer_user)
    hidden = upload_huntington(viewer, account)
    missing = viewer.get(reverse("csv-import-preview", args=(account.pk + 999,)))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content

    staged = upload_huntington(owner_client, account)
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
    assert Transaction.objects.filter(account=account).count() == 3
    batch = ImportBatch.objects.get()
    assert batch.source == ImportBatch.Source.HUNTINGTON
    assert Transaction.objects.get(description="ATM WITHDRAWAL FEE").source_transaction_id == "100000000000000002"


@pytest.mark.django_db
def test_huntington_wrong_headers_are_not_staged_or_logged(caplog, staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    secret = "PRIVATE-SOURCE-ROW-SECRET"
    content = f"When,Memo,Amount\n09/27/2026,{secret},-1.00\n".encode()

    with caplog.at_level(logging.DEBUG):
        response = upload_huntington(client, account, content)

    assert response.status_code == 200
    assert b"does not match the Huntington checking export" in response.content
    assert secret.encode() not in response.content
    assert secret not in caplog.text
    assert not list(staging_settings.glob("*.csvstage"))
    assert response.context.get("preview") is None


@pytest.mark.django_db
def test_huntington_get_restores_preview_and_commit_needs_a_date_range(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    staged = upload_huntington(client, account)
    token = staged.context["mapping_form"].data["token"]

    restored = client.get(preview_url)
    assert restored.status_code == 200
    assert restored.context["preview"].valid_count == 3
    assert b"Huntington checking columns are mapped automatically" in restored.content

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

