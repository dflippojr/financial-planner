from datetime import date
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.csv_import.ofx import OFX_HEADERS, read_ofx
from finance.csv_import.parser import MAX_FILE_BYTES, CsvInputError, preview_csv
from finance.csv_import.profiles import OFX_MAPPING
from finance.csv_import.staging import SESSION_KEY
from finance.models import Account, ImportBatch, Transaction
from tests.test_csv_import_views import make_person

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name="synthetic_checking.ofx"):
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize("name,amounts,descriptions", [
    ("synthetic_checking.ofx", [-1234, 10000], ["SYNTHETIC GROCER - Weekly & fresh", "SYNTHETIC PAY"]),
    ("synthetic_card.qfx", [-4567, 500], ["SYNTHETIC SHOP - Supplies", "SYNTHETIC REFUND"]),
])
def test_statement_preview(name, amounts, descriptions):
    document = read_ofx(fixture(name))
    assert document.headers == OFX_HEADERS
    preview = preview_csv(document, OFX_MAPPING)
    assert preview.invalid_count == 0
    assert [row.transaction_date for row in preview.rows] == [date(2026, 9, 27), date(2026, 9, 28)]
    assert [row.amount_minor for row in preview.rows] == amounts
    assert [row.description for row in preview.rows] == descriptions
    assert all(row.currency == "USD" for row in preview.rows)


@pytest.mark.parametrize("old,new,error", [
    (b"-12.34", b"-12.345", "Amount is not valid"),
    (b"20260927120000", b"20260230120000", "Date does not match"),
    (b"20260927120000", b"bad-date", "Date does not match"),
    (b"<CURDEF>USD", b"<CURDEF>EUR", "Currency must be USD"),
    (b"<TRNAMT>-12.34", b"<CURRENCY><CURSYM>CAD</CURRENCY><TRNAMT>-12.34", "Currency must be USD"),
])
def test_invalid_row_fields(old, new, error):
    preview = preview_csv(read_ofx(fixture().replace(old, new)), OFX_MAPPING)
    assert any(error in message for message in preview.rows[0].errors)


@pytest.mark.parametrize("content,message", [
    (b"", "not a valid"),
    (b"Date,Amount,Name\n2026-09-27,-12.34,Example", "not a valid"),
    (b"<OFX><STMTRS></OFX>", "not a valid"),
    (b"OFXHEADER:100\n<OFX><STMTRS></OFX>", "not a valid"),
    (b"OFXHEADER:100\nno root", "not a valid"),
    (b"OFXHEADER:100\n<OFX><!bad></OFX>", "not a valid"),
    (b"<OTHER/>", "not a valid"),
    (b"<OFX><INVSTMTMSGSRSV1><INVSTMTRS><STMTTRN/></INVSTMTRS></INVSTMTMSGSRSV1></OFX>", "no bank or card transactions"),
    (b'<!DOCTYPE OFX [<!ENTITY x "secret">]><OFX/>', "entities are not supported"),
    (b"\xff<OFX/>", "encoding"),
])
def test_bad_files(content, message):
    with pytest.raises(CsvInputError, match=message):
        read_ofx(content)


def test_row_limit_and_legacy_encoding():
    with pytest.raises(CsvInputError, match="5 MB limit"):
        read_ofx(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(CsvInputError, match="row limit"):
        read_ofx(fixture(), max_rows=1)
    document = read_ofx(fixture().replace(b"Weekly &amp; fresh", b"Synthetic caf\xe9"))
    assert document.rows[0].cells[3] == "Synthetic café"


@pytest.fixture
def import_client(tmp_path):
    user, person = make_person("ofx-owner")
    account = Account.objects.create(name="Synthetic checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path):
        yield client, account, tmp_path


def upload(client, account, content, profile="ofx", name="synthetic.ofx"):
    return client.post(reverse("csv-import-preview", args=[account.pk]), {
        "action": "upload", "import_profile": profile,
        "csv_file": SimpleUploadedFile(name, content),
    })


def commit(client, account, response):
    form = response.context["mapping_form"]
    return client.post(reverse("csv-import-preview", args=[account.pk]), {
        "action": "commit", "token": form.data["token"], "source": "vanguard",
        "date_range_start": form.data["date_range_start"],
        "date_range_end": form.data["date_range_end"],
    })


@pytest.mark.django_db
@pytest.mark.parametrize("name,account_type", [("synthetic_checking.ofx", "checking"), ("synthetic_card.qfx", "credit_card")])
def test_import_reimport_and_undo(import_client, name, account_type):
    client, account, staging = import_client
    account.account_type = account_type
    account.save()
    content = fixture(name)
    response = upload(client, account, content, name=name)
    assert response.context["preview"].new_count == 2
    assert not ImportBatch.objects.exists()
    restored = client.get(reverse("csv-import-preview", args=[account.pk]))
    assert restored.context["preview"].new_count == 2
    assert b'Save mapping</button>' not in restored.content
    assert commit(client, account, restored).status_code == 302
    batch = ImportBatch.objects.get()
    assert batch.source == "ofx"
    assert batch.saved_csv_mapping_id is None
    assert not list(staging.iterdir())
    stored = Transaction.objects.order_by("source_row_number").first()
    assert set(stored.original_fields) == set(OFX_HEADERS)
    assert stored.source_transaction_id.startswith("synthetic-")
    assert stored.kind == "cash_flow"
    response = upload(client, account, content)
    assert response.context["preview"].duplicate_count == 2
    assert commit(client, account, response).status_code == 302
    assert ImportBatch.objects.count() == 1
    assert Transaction.objects.count() == 2
    assert not list(staging.iterdir())
    # A changed FITID still matches by fingerprint; only the extra date is new.
    overlap = content.replace(b"20260927", b"20260929").replace(b"synthetic-2", b"synthetic-changed-id")
    response = upload(client, account, overlap)
    assert response.context["preview"].duplicate_count == 1
    assert response.context["preview"].new_count == 1
    assert commit(client, account, response).status_code == 302
    second = ImportBatch.objects.exclude(pk=batch.pk).get()
    assert client.post(reverse("csv-import-undo", args=[account.pk, second.pk])).status_code == 302
    assert Transaction.objects.filter(status="active").count() == 2
    assert Transaction.objects.get(import_batch=second).status == "archived"


@pytest.mark.django_db
@pytest.mark.parametrize("content,profile,message", [
    (b"broken", "ofx", "not a valid"),
    (b"<OFX><INVSTMTRS/></OFX>", "ofx", "no bank or card transactions"),
    (fixture().replace(b"<CURDEF>USD", b"<CURDEF>EUR"), "ofx", "Currency must be USD"),
    (b"Date,Amount,Name\n2026-09-27,-12.34,Example", "ofx", "not a valid"),
    (fixture(), "generic", "Choose the OFX / QFX profile"),
    (fixture(), "huntington", "Choose the OFX / QFX profile"),
])
def test_upload_errors_leave_nothing_staged(import_client, content, profile, message):
    client, account, staging = import_client
    response = upload(client, account, content, profile)
    assert message in str(response.context["upload_form"].errors)
    assert not list(staging.iterdir())
    assert not client.session.get(SESSION_KEY)
    assert not ImportBatch.objects.exists()
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_stage_cannot_be_used_for_another_account_or_session(import_client):
    client, account, _staging = import_client
    response = upload(client, account, fixture())
    token = response.context["mapping_form"].data["token"]
    other = Account.objects.create(name="Synthetic other", account_type="checking", owner=account.owner)
    assert client.post(reverse("csv-import-preview", args=[other.pk]), {"action": "commit", "token": token}).status_code == 404
    second = Client()
    second.force_login(account.owner.user)
    assert second.post(reverse("csv-import-preview", args=[account.pk]), {"action": "commit", "token": token}).status_code == 404
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_hub_upload_and_cancel(import_client):
    client, account, staging = import_client
    response = client.post(reverse("csv-import"), {
        "account_id": account.pk, "import_profile": "ofx",
        "csv_file": SimpleUploadedFile("synthetic.qbo", fixture()),
    })
    assert response.status_code == 302
    preview = client.get(response.url)
    token = preview.context["mapping_form"].data["token"]
    assert client.post(response.url, {"action": "cancel", "token": token}).status_code == 302
    assert not list(staging.iterdir())
