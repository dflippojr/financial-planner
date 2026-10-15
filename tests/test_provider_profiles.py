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
from finance.category_services import income_and_spending_totals
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
from finance.models import Account, ImportBatch, Person, Transaction, TransferPair
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
    purchase = Transaction.objects.get(description="SYNTHETIC GROCER")
    assert purchase.original_fields["Posted Date"] == "2026-09-16"
    assert purchase.original_fields["Category"] == "Merchandise"
    assert purchase.category is None
    assert purchase.kind == Transaction.Kind.CASH_FLOW
    assert purchase.amount_minor == -4215
    assert Transaction.objects.get(description="CAPITAL ONE MOBILE PAYMENT").amount_minor == 50000
    assert Transaction.objects.get(description="SYNTHETIC REFUND").amount_minor == 1500


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


def upload(p, client, account, content=None):
    return client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {
            "action": "upload",
            "import_profile": p.profile,
            "csv_file": SimpleUploadedFile("synthetic.csv", content or p.native, "text/csv"),
        },
    )


def import_through_views(p, client, account, content):
    """Upload then commit one file through the import views; return the preview shown before the commit."""
    staged = upload(p, client, account, content)
    assert staged.status_code == 200
    committed = client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {
            "action": "commit",
            "token": staged.context["mapping_form"].data["token"],
            **p.commit_post_extra,
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
    )
    assert committed.status_code == 302
    return staged.context["preview"]


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
def test_reimporting_the_same_and_overlapping_files_adds_no_duplicates(p, staging_settings):
    user, person = make_person("owner")
    account = make_account(p, person)
    client = Client()
    client.force_login(user)
    same_duplicates, overlap_new, overlap_duplicates, active_total = p.reimport

    first = import_through_views(p, client, account, p.native)
    assert (first.new_count, first.duplicate_count, first.invalid_count) == (p.valid_count, 0, p.invalid_count)
    assert Transaction.objects.filter(account=account).count() == p.valid_count
    # commit_post_extra spoofs a different source; the profile decides the batch source.
    assert ImportBatch.objects.get().source == p.source
    p.check_commit(account)

    same = import_through_views(p, client, account, p.native)
    assert (same.new_count, same.duplicate_count) == (0, same_duplicates)

    overlap = import_through_views(p, client, account, p.overlap)
    assert (overlap.new_count, overlap.duplicate_count) == (overlap_new, overlap_duplicates)

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
def test_import_requires_a_visible_account(p, staging_settings):
    _owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    account = make_account(p, owner)

    viewer = Client()
    viewer.force_login(viewer_user)
    hidden = upload(p, viewer, account)
    missing = viewer.get(reverse("csv-import-preview", args=(account.pk + 999,)))

    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content
    assert not Transaction.objects.exists()


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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("p", "income_minor", "spending_minor"),
    [(PROVIDERS["capital_one"], 1500, 5265), (PROVIDERS["apple_card"], 1500, 16145)],
    ids=["capital_one", "apple_card"],
)
def test_imported_card_payment_pairs_with_the_checking_withdrawal(p, income_minor, spending_minor, staging_settings):
    user, person = make_person("owner")
    checking = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    card = make_account(p, person)
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
    client = Client()
    client.force_login(user)

    import_through_views(p, client, card, p.native)

    pair = TransferPair.objects.get()
    assert pair.kind == TransferPair.Kind.CARD_PAYMENT
    # Both 50000 legs stay out of the totals; only the refund and the purchases remain.
    totals = income_and_spending_totals(person)
    assert (totals.income_minor, totals.spending_minor) == (income_minor, spending_minor)
