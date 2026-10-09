from datetime import date
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.csv_import.ofx import OFX_HEADERS, looks_like_ofx, read_ofx
from finance.csv_import.parser import MAX_FILE_BYTES, CsvInputError, preview_csv
from finance.csv_import.profiles import OFX_MAPPING
from finance.csv_import.staging import SESSION_KEY
from finance.models import Account, ImportBatch, Transaction
from tests.test_csv_import_views import make_person, mapping_data

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
    (b"20260927120000", b"99990615120000", "more than a year in the future"),
    (b"20260927120000", b"18991231120000", "on or after January 1, 1900"),
    (b"<CURDEF>USD", b"<CURDEF>EUR", "Currency must be USD"),
    (b"<TRNAMT>-12.34", b"<CURRENCY><CURSYM>CAD</CURRENCY><TRNAMT>-12.34", "Currency must be USD"),
    (b"<FITID>synthetic-1", b"<FITID>" + b"a" * 256, "FITID exceeds"),
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
    (b"OFXHEADER:100\n<OFX><", "not a valid"),
    (b"OFXHEADER:100\n<OFX></OFX>junk", "not a valid"),
    (b"OFXHEADER:100\n<OFX>", "not a valid"),
    (b"OFXHEADER:100\n<OFX>" + b"<NEST>" * 65, "not a valid"),
    (b"OFXHEADER:100\n<OFX>\x00</OFX>", "not a valid"),
    (b"<OTHER/>", "not a valid"),
    (b"<OFX><INVSTMTMSGSRSV1><INVSTMTRS><STMTTRN/></INVSTMTRS></INVSTMTMSGSRSV1></OFX>", "no bank or card transactions"),
    (b'<!DOCTYPE OFX [<!ENTITY x "secret">]><OFX/>', "entities are not supported"),
    (b"\xff<OFX/>", "encoding"),
    (b"OFXHEADER:100\nCHARSET:1252\n<OFX>\x81</OFX>", "encoding"),
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


def test_sgml_explicit_scalar_end_tags_and_empty_memo():
    content = fixture().replace(b"<CURDEF>USD", b"<CURDEF>USD</CURDEF>")
    content = content.replace(b"<NAME>SYNTHETIC PAY", b"<MEMO></MEMO><NAME>SYNTHETIC PAY</NAME>")
    assert preview_csv(read_ofx(content), OFX_MAPPING).invalid_count == 0


@pytest.mark.parametrize("field", ["CHECKNUM", "REFNUM", "SIC", "DTUSER", "DTAVAIL", "CORRECTFITID", "SRVRTID", "PAYEEID"])
def test_empty_optional_sgml_scalar(field):
    content = fixture().replace(b"<NAME>SYNTHETIC GROCER", f"<{field}><NAME>SYNTHETIC GROCER".encode())
    preview = preview_csv(read_ofx(content), OFX_MAPPING)
    assert preview.valid_count == 2
    assert preview.rows[0].amount_minor == -1234
    assert preview.rows[0].description == "SYNTHETIC GROCER - Weekly & fresh"


@pytest.mark.parametrize("payee_tag", ["PAYEE", "PAYEE2"])
def test_nested_payee_with_empty_sgml_address(payee_tag):
    content = fixture().replace(
        b"<NAME>SYNTHETIC GROCER",
        f"<{payee_tag}><NAME>SYNTHETIC GROCER\n<ADDR1>SYNTHETIC STREET\n<ADDR2><CITY>SYNTHETIC CITY\n"
        f"<STATE>ZZ\n<POSTALCODE>00000\n<PHONE>0000000000\n</{payee_tag}>".encode(),
    )
    preview = preview_csv(read_ofx(content), OFX_MAPPING)
    assert preview.valid_count == 2
    assert preview.rows[0].description == "SYNTHETIC GROCER - Weekly & fresh"


def test_sgml_investment_fields_do_not_affect_bank_transactions():
    content = fixture().replace(
        b"</OFX>",
        b"<INVSTMTMSGSRSV1><INVSTMTRS><SECID><UNIQUEID><UNIQUEIDTYPE>CUSIP\n"
        b"</SECID></INVSTMTRS></INVSTMTMSGSRSV1></OFX>",
    )
    assert preview_csv(read_ofx(content), OFX_MAPPING).valid_count == 2


def test_sgml_extension_fields_are_ignored():
    content = fixture().replace(b"<OFX>", b"<OFX><SYNTHETIC.BID>999\n")
    assert preview_csv(read_ofx(content), OFX_MAPPING).valid_count == 2


def test_long_unclosed_sgml_tags_are_rejected():
    with pytest.raises(CsvInputError, match="not a valid"):
        read_ofx(b"OFXHEADER:100\n<OFX>" + b"<" * 100_000)


def test_mixed_currencies_are_row_errors():
    content = fixture("synthetic_card.qfx").replace(
        b"<TRNAMT>-45.67", b"<CURRENCY><CURSYM>EUR</CURSYM></CURRENCY><TRNAMT>-45.67"
    )
    preview = preview_csv(read_ofx(content), OFX_MAPPING)
    assert preview.invalid_count == 1
    assert preview.valid_count == 1


@pytest.mark.parametrize("prefix", ["", "ofx:"])
def test_namespaced_xml_statements(prefix):
    content = fixture("synthetic_card.qfx").decode()
    if prefix:
        import re

        content = re.sub(r"<(/?)([A-Z][A-Z0-9]*)", rf"<\1{prefix}\2", content)
    declaration = f'xmlns{":ofx" if prefix else ""}="http://ofx.net/types/2003/04"'
    content = content.replace(f"<{prefix}OFX>", f"<{prefix}OFX {declaration}>")
    # A foreign namespace is not a statement transaction in the document's namespace.
    content = content.replace(f"</{prefix}OFX>", f'<foreign:STMTRS xmlns:foreign="urn:synthetic"/></{prefix}OFX>')
    preview = preview_csv(read_ofx(content.encode()), OFX_MAPPING)
    assert preview.valid_count == 2
    assert preview.rows[0].amount_minor == -4567


@pytest.mark.parametrize("encoding", [
    "windows-1252", "iso-8859-1", "utf-16", "utf-32", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be",
])
def test_xml_declared_encodings(encoding):
    content = fixture("synthetic_card.qfx").decode().replace('encoding="UTF-8"', f'encoding="{encoding}"')
    content = content.replace("Supplies", "Synthetic café")
    preview = preview_csv(read_ofx(content.encode(encoding)), OFX_MAPPING)
    assert preview.valid_count == 2
    assert preview.rows[0].description == "SYNTHETIC SHOP - Synthetic café"


def test_xml_unknown_encoding_and_encoded_entity_declarations_are_safe():
    content = fixture("synthetic_card.qfx").replace(b'encoding="UTF-8"', b'encoding="unknown-synthetic"')
    with pytest.raises(CsvInputError, match="encoding"):
        read_ofx(content)
    content = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE OFX [<!ENTITY x "bad">]><OFX/>'
    with pytest.raises(CsvInputError, match="entities are not supported"):
        read_ofx(content.encode("utf-16"))


@pytest.mark.parametrize("content,expected", [
    (fixture(), True),
    (fixture("synthetic_card.qfx"), True),
    (b'<?xml version="1.0"?><!-- synthetic --><x:OFX xmlns:x="urn:synthetic"/>', True),
    (b"\n<!-- synthetic --><?OFX version='200'?><OFX/>", True),
    (b"<?unterminated", False),
    (b"<!-- unterminated", False),
    (b"", False),
    (b"  ", False),
    (b"\xff", False),
    (b"OFXHEADER:100,Memo,Amount\nexample,<OFX>,-1.00", False),
    (b"Date,Memo,Amount\n2026-09-27,<OFX>,-1.00", False),
])
def test_format_detection_uses_only_the_file_preamble(content, expected):
    assert looks_like_ofx(content) is expected


def test_foreign_purchase_posted_in_usd_ignores_original_currency():
    content = fixture("synthetic_card.qfx").replace(
        b"<TRNAMT>-45.67",
        b"<ORIGCURRENCY><CURSYM>EUR</CURSYM></ORIGCURRENCY>"
        b"<CURRENCY><CURSYM>USD</CURSYM></CURRENCY><TRNAMT>-45.67",
    )
    preview = preview_csv(read_ofx(content), OFX_MAPPING)
    assert preview.invalid_count == 0
    assert preview.rows[0].amount_minor == -4567
    assert preview.rows[0].currency == "USD"


@pytest.mark.parametrize("action", ["REPLACE", "DELETE"])
@pytest.mark.parametrize("name", ["synthetic_checking.ofx", "synthetic_card.qfx"])
def test_provider_corrections_are_rejected(name, action):
    marker = f"<CORRECTFITID>synthetic-prior</CORRECTFITID><CORRECTACTION>{action}</CORRECTACTION>".encode()
    content = fixture(name).replace(b"<TRNAMT>", marker + b"<TRNAMT>", 1)
    with pytest.raises(CsvInputError, match="transaction corrections are not supported"):
        read_ofx(content)


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
    expected = preview_csv(read_ofx(content), OFX_MAPPING)
    assert list(Transaction.objects.order_by("source_row_number").values_list(
        "transaction_date", "amount_minor", "description", "currency"
    )) == [(row.transaction_date, row.amount_minor, row.description, row.currency) for row in expected.rows]
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
def test_replacement_does_not_add_to_an_earlier_import(import_client):
    client, account, staging = import_client
    response = upload(client, account, fixture())
    assert commit(client, account, response).status_code == 302
    amounts = list(Transaction.objects.order_by("pk").values_list("amount_minor", flat=True))
    correction = fixture().replace(
        b"<TRNAMT>-12.34",
        b"<CORRECTFITID>synthetic-1\n<CORRECTACTION>REPLACE\n<TRNAMT>-10.00",
    ).replace(b"<FITID>synthetic-1", b"<FITID>synthetic-correction")
    response = upload(client, account, correction)
    assert "transaction corrections are not supported" in str(response.context["upload_form"].errors)
    assert not list(staging.iterdir())
    assert not client.session.get(SESSION_KEY)
    assert ImportBatch.objects.count() == 1
    assert list(Transaction.objects.order_by("pk").values_list("amount_minor", flat=True)) == amounts


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


@pytest.mark.django_db
def test_csv_description_containing_ofx_imports_normally(import_client):
    client, account, staging = import_client
    content = b"When,Memo,Amount,Currency\n09/27/2026,<OFX> SYNTHETIC,-12.34,USD\n"
    response = upload(client, account, content, profile="generic", name="synthetic.csv")
    assert not response.context["upload_form"].errors
    token = response.context["mapping_form"].initial["token"]
    data = mapping_data(token, action="commit", source="huntington",
                        date_range_start="2026-09-01", date_range_end="2026-09-30")
    response = client.post(reverse("csv-import-preview", args=[account.pk]), data)
    assert response.status_code == 302
    transaction = Transaction.objects.get()
    assert transaction.description == "<OFX> SYNTHETIC"
    assert transaction.amount_minor == -1234
    assert not list(staging.iterdir())


@pytest.mark.django_db
def test_encoded_xml_chosen_as_csv_has_a_profile_error(import_client):
    client, account, staging = import_client
    content = fixture("synthetic_card.qfx").decode().replace('encoding="UTF-8"', 'encoding="utf-16"')
    response = upload(client, account, content.encode("utf-16"), profile="generic")
    assert "Choose the OFX / QFX profile" in str(response.context["upload_form"].errors)
    assert not list(staging.iterdir())
