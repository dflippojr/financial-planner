"""Cases every fixed provider profile (Huntington, Capital One, Apple Card) must pass.

Synthetic fixtures only. Provider-specific behavior stays in each provider's own test file.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.csv_import.parser import CsvInputError, preview_csv, read_csv
from finance.csv_import.profiles import (
    APPLE_CARD_HEADER_ERROR,
    APPLE_CARD_HEADERS,
    APPLE_CARD_MAPPING,
    CAPITAL_ONE_HEADER_ERROR,
    CAPITAL_ONE_HEADERS,
    CAPITAL_ONE_MAPPING,
    HUNTINGTON_HEADER_ERROR,
    HUNTINGTON_HEADERS,
    HUNTINGTON_MAPPING,
    require_headers,
)
from finance.csv_import.services import commit_csv_import
from finance.models import Account, ImportBatch, Person, Transaction
from tests import apple_card_fixtures, capital_one_fixtures, huntington_fixtures

PASSWORD = "Synthetic-passphrase-42!"
SECRET_HEADER = "PRIVATE-RENAMED-HEADER"
SECRET_ROW = "PRIVATE-SOURCE-ROW-SECRET"


@dataclass(frozen=True)
class Provider:
    profile: str
    headers: tuple
    header_error: str
    error_match: str
    mapping: object
    source: str
    account_type: str
    native: bytes
    overlap: bytes
    build_csv: Callable
    renamed_index: int  # which expected header the renamed-header case replaces
    sample_row: str  # a valid data row for the renamed-header file
    bad_row: Callable  # (date, amount, extra) -> three bad rows
    wrong_header_file: bytes
    intro: bytes
    valid_count: int
    invalid_count: int
    commit_post_extra: dict
    reimport: tuple  # (same duplicates, overlap new, overlap duplicates, active total)
    check_commit: Callable
    check_upload: Callable
    check_overlap: Callable


def _huntington_check_commit(account):
    fee = Transaction.objects.get(description="ATM WITHDRAWAL FEE")
    assert fee.source_transaction_id == "100000000000000002"


def _capital_one_check_commit(account):
    assert not Transaction.objects.filter(account=account, original_fields__has_key="Card No.").exists()


def _apple_check_commit(account):
    assert Transaction.objects.get(description="Synthetic Interest").amount_minor == -275


def _huntington_check_upload(response):
    assert response.context["preview"].rows[0].description == "SYNTHETIC GROCER - WEEKLY FOOD"


def _capital_one_check_upload(response):
    assert b"1234" not in response.content


def _apple_check_upload(response):
    assert response.context["preview"].rows[0].description == "Synthetic Coffee"


def _huntington_check_overlap(account):
    cafe = Transaction.objects.get(description="SYNTHETIC CAFE - COFFEE")
    assert cafe.amount_minor == -525
    assert cafe.source_transaction_id == "100000000000000004"


def _capital_one_check_overlap(account):
    assert Transaction.objects.filter(account=account, description="SYNTHETIC CAFE").count() == 2


def _apple_check_overlap(account):
    assert Transaction.objects.filter(description="Synthetic Grocery").count() == 2
    assert Transaction.objects.get(description="Synthetic Bookstore").amount_minor == -1825


PROVIDERS = {
    "huntington": Provider(
        profile="huntington",
        headers=HUNTINGTON_HEADERS,
        header_error=HUNTINGTON_HEADER_ERROR,
        error_match="does not match the Huntington checking export",
        mapping=HUNTINGTON_MAPPING,
        source=ImportBatch.Source.HUNTINGTON,
        account_type="checking",
        native=huntington_fixtures.NATIVE_CSV,
        overlap=huntington_fixtures.OVERLAP_CSV,
        build_csv=huntington_fixtures.huntington_csv,
        renamed_index=0,
        sample_row="09/15/2026,1,SYNTHETIC GROCER,WEEKLY FOOD,-1.00,,1",
        bad_row=lambda d, a, x: (
            f"{d},1,SYNTHETIC GROCER,WEEKLY FOOD,-1.00,,1",
            f"09/15/2026,1,SYNTHETIC GROCER,WEEKLY FOOD,{a},,2",
            f"09/15/2026,1,{x}",
        ),
        wrong_header_file=f"When,Memo,Amount\n09/27/2026,{SECRET_ROW},-1.00\n".encode(),
        intro=b"Huntington checking columns are mapped automatically",
        valid_count=3,
        invalid_count=0,
        commit_post_extra={"source": "capital_one"},
        reimport=(3, 1, 1, 4),
        check_commit=_huntington_check_commit,
        check_upload=_huntington_check_upload,
        check_overlap=_huntington_check_overlap,
    ),
    "capital_one": Provider(
        profile="capital_one",
        headers=CAPITAL_ONE_HEADERS,
        header_error=CAPITAL_ONE_HEADER_ERROR,
        error_match="does not match the Capital One credit card export",
        mapping=CAPITAL_ONE_MAPPING,
        source=ImportBatch.Source.CAPITAL_ONE,
        account_type="credit_card",
        native=capital_one_fixtures.NATIVE_CSV,
        overlap=capital_one_fixtures.OVERLAP_CSV,
        build_csv=capital_one_fixtures.capital_one_csv,
        renamed_index=0,
        sample_row="2026-09-15,2026-09-16,1234,SYNTHETIC GROCER,Merchandise,1.00,",
        bad_row=lambda d, a, x: (
            f"{d},2026-09-16,1234,SYNTHETIC GROCER,Merchandise,1.00,",
            f"2026-09-15,2026-09-16,1234,SYNTHETIC GROCER,Merchandise,{a},",
            f"2026-09-15,2026-09-16,{x}",
        ),
        wrong_header_file=f"When,Memo,Amount\n2026-09-27,{SECRET_ROW},-1.00\n".encode(),
        intro=b"Capital One columns are mapped automatically",
        valid_count=5,
        invalid_count=1,
        commit_post_extra={"source": "capital_one"},
        reimport=(5, 1, 2, 6),
        check_commit=_capital_one_check_commit,
        check_upload=_capital_one_check_upload,
        check_overlap=_capital_one_check_overlap,
    ),
    "apple_card": Provider(
        profile="apple_card",
        headers=APPLE_CARD_HEADERS,
        header_error=APPLE_CARD_HEADER_ERROR,
        error_match="does not match the Apple Card export",
        mapping=APPLE_CARD_MAPPING,
        source=ImportBatch.Source.APPLE_CARD,
        account_type="credit_card",
        native=apple_card_fixtures.NATIVE_CSV,
        overlap=apple_card_fixtures.OVERLAP_CSV,
        build_csv=apple_card_fixtures.apple_card_csv,
        renamed_index=3,
        sample_row="09/15/2026,09/15/2026,SYNTHETIC ROW,Synthetic,Shopping,Purchase,1.00,Dana Example",
        bad_row=lambda d, a, x: (
            f"{d},09/15/2026,SYNTHETIC ROW,Synthetic,Shopping,Purchase,1.00,Dana Example",
            f"09/15/2026,09/15/2026,SYNTHETIC ROW,Synthetic,Shopping,Purchase,{a},Dana Example",
            f"09/15/2026,09/15/2026,{x}",
        ),
        wrong_header_file=f"When,Who,Amount\n09/27/2026,{SECRET_ROW},-1.00\n".encode(),
        intro=b"Apple Card columns are mapped automatically",
        valid_count=7,
        invalid_count=1,
        commit_post_extra={},
        reimport=(7, 1, 1, 8),
        check_commit=_apple_check_commit,
        check_upload=_apple_check_upload,
        check_overlap=_apple_check_overlap,
    ),
}

provider = pytest.mark.parametrize("p", PROVIDERS.values(), ids=PROVIDERS.keys())


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_account(p, person):
    return Account.objects.create(name="Synthetic Account", account_type=p.account_type, owner=person)


def commit(p, user, account, content):
    document = read_csv(content)
    require_headers(document.headers, p.headers, p.header_error)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=document,
        mapping=p.mapping,
        source=p.source,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )


def upload(p, client, account, content=None):
    return client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {
            "action": "upload",
            "import_profile": p.profile,
            "csv_file": SimpleUploadedFile("synthetic.csv", content or p.native, "text/csv"),
        },
    )


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


@provider
def test_missing_or_renamed_headers_are_rejected_without_echoing_them(p):
    renamed = list(p.headers)
    renamed[p.renamed_index] = SECRET_HEADER
    content = (",".join(renamed) + "\r\n" + p.sample_row + "\r\n").encode()

    document = read_csv(content)
    with pytest.raises(CsvInputError, match=p.error_match) as caught:
        require_headers(document.headers, p.headers, p.header_error)

    assert SECRET_HEADER not in str(caught.value)
    assert p.header_error == str(caught.value)


@provider
def test_row_errors_omit_raw_cells(p, caplog):
    secret_date, secret_amount, secret_extra = "PRIVATE-BAD-DATE", "PRIVATE-BAD-AMOUNT", "PRIVATE-RAGGED-CELL"
    content = p.build_csv(p.bad_row(secret_date, secret_amount, secret_extra))

    with caplog.at_level(logging.DEBUG):
        preview = preview_csv(read_csv(content), p.mapping)

    messages = " ".join(error for row in preview.rows for error in row.errors)
    assert preview.invalid_count == 3
    assert "Date does not match the selected format." in preview.rows[0].errors
    assert "Amount is not valid" in preview.rows[1].errors[0]
    assert "different number of columns" in preview.rows[2].errors[0]
    for secret in (secret_date, secret_amount, secret_extra):
        assert secret not in messages
        assert secret not in caplog.text


@provider
@pytest.mark.django_db
def test_reimporting_the_same_and_overlapping_files_adds_no_duplicates(p):
    user, person = make_person("owner")
    account = make_account(p, person)
    same_duplicates, overlap_new, overlap_duplicates, active_total = p.reimport

    first = commit(p, user, account, p.native)
    same = commit(p, user, account, p.native)
    overlap = commit(p, user, account, p.overlap)

    assert first.new_count == p.valid_count
    assert first.invalid_count == p.invalid_count
    assert same.new_count == 0
    assert same.duplicate_count == same_duplicates
    assert overlap.new_count == overlap_new
    assert overlap.duplicate_count == overlap_duplicates
    assert Transaction.objects.filter(account=account, status="active").count() == active_total
    p.check_overlap(account)


@provider
@pytest.mark.django_db
def test_upload_previews_without_column_mapping(p, staging_settings):
    user, person = make_person("owner")
    account = make_account(p, person)
    client = Client()
    client.force_login(user)

    response = upload(p, client, account)

    assert response.status_code == 200
    assert response.context["preview"].valid_count == p.valid_count
    assert p.intro in response.content
    assert b"date_column" not in response.content
    assert not Transaction.objects.exists()
    p.check_upload(response)


@provider
@pytest.mark.django_db
def test_commit_requires_a_visible_account_and_imports(p, staging_settings):
    owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    account = make_account(p, owner)
    owner_client = Client()
    owner_client.force_login(owner_user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))

    viewer = Client()
    viewer.force_login(viewer_user)
    hidden = upload(p, viewer, account)
    missing = viewer.get(reverse("csv-import-preview", args=(account.pk + 999,)))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content

    staged = upload(p, owner_client, account)
    token = staged.context["mapping_form"].data["token"]
    imported = owner_client.post(
        preview_url,
        {
            "action": "commit",
            "token": token,
            **p.commit_post_extra,
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
        follow=True,
    )

    assert imported.status_code == 200
    assert Transaction.objects.filter(account=account).count() == p.valid_count
    assert ImportBatch.objects.get().source == p.source
    p.check_commit(account)


@provider
@pytest.mark.django_db
def test_wrong_headers_are_not_staged_or_logged(p, caplog, staging_settings):
    user, person = make_person("owner")
    account = make_account(p, person)
    client = Client()
    client.force_login(user)

    with caplog.at_level(logging.DEBUG):
        response = upload(p, client, account, p.wrong_header_file)

    assert response.status_code == 200
    assert p.error_match.encode() in response.content
    assert SECRET_ROW.encode() not in response.content
    assert SECRET_ROW not in caplog.text
    assert not list(staging_settings.glob("*.csvstage"))
    assert response.context.get("preview") is None


@provider
@pytest.mark.django_db
def test_get_restores_preview_and_commit_needs_a_date_range(p, staging_settings):
    user, person = make_person("owner")
    account = make_account(p, person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    staged = upload(p, client, account)
    token = staged.context["mapping_form"].data["token"]

    restored = client.get(preview_url)
    assert restored.status_code == 200
    assert restored.context["preview"].valid_count == p.valid_count
    assert p.intro in restored.content

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
