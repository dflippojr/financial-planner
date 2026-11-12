"""While an account's SimpleFIN link is failing, a file import may fill the days after its last synced row (#735).

A link counts as failing when the latest stored read lists an error for the
account's bank, when that read no longer lists the account, when the
connection is disabled, or when no read has succeeded for
LINK_FAILING_AFTER. Rows from the cut-over through the last SimpleFIN row
stay refused; a healthy link keeps refusing everything from the cut-over.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from finance.csv_import.parser import Mapping, preview_csv, read_csv
from finance.csv_import.services import classify_overlap
from finance.models import Account, ImportBatch, SimpleFinConnection, Transaction
from finance.simplefin_services import sync_connection
from tests.properties import harness as ledger
from tests.properties.strategies import Row, simplefin_payload
from tests.test_simplefin import signed_in

CUTOVER = date(2026, 10, 1)
LAST_SYNCED = date(2026, 10, 5)
SYNCED = [
    ("a", Row(date(2026, 10, 2), -1100, "SYN SF COFFEE", "", "")),
    ("b", Row(LAST_SYNCED, -2200, "SYN SF GROCER", "", "")),
]
CSV = (
    b"Date,Description,Amount\n"
    b"2026-09-30,SYN CSV BEFORE CUTOVER,-1.00\n"
    b"2026-10-05,SYN CSV GROCER,-22.00\n"
    b"2026-10-06,SYN CSV GAP ONE,-3.00\n"
    b"2026-10-08,SYN CSV GAP TWO,-4.00\n"
)
MAPPING = Mapping(date_column="Date", description_column="Description", date_format="iso",
                  number_format="dot_none", amount_mode="signed", amount_column="Amount")
# The N the issue asks the worker to propose: three days without a successful read.
N_DAYS = timedelta(days=3)
THROUGH = "Covered by SimpleFIN through 2026-10-05."
FROM_CUTOVER = "Covered by SimpleFIN from 2026-10-01."


def _synced_checking():
    owner = ledger.make_member()
    ledger.make_household(owner)
    checking = ledger.make_account(owner, Account.Type.CHECKING)
    connection = ledger.link_simplefin(owner, checking, CUTOVER)
    ledger.sync(owner, connection, SYNCED)
    return owner, checking, connection


def _sync_payload(owner, connection, payload):
    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        return sync_connection(owner, connection.pk, ignore_rate_limit=True)


def _read_with_bank_error(conn_id="CON-1"):
    payload = simplefin_payload([])
    payload["errlist"].append({"code": "con.auth", "conn_id": conn_id, "msg": "Synthetic sign-in required."})
    return payload


def _read_without_the_account():
    payload = simplefin_payload([])
    payload["accounts"][0]["id"] = "sf-other"
    return payload


def _preview(account):
    return classify_overlap(account, preview_csv(read_csv(CSV), MAPPING))


def _statuses(preview):
    return [row.overlap_status for row in preview.rows]


@pytest.mark.django_db
def test_a_link_listed_with_an_error_accepts_rows_after_its_last_synced_day():
    owner, checking, connection = _synced_checking()
    _sync_payload(owner, connection, _read_with_bank_error())

    preview = _preview(checking)

    assert _statuses(preview) == ["new", "invalid", "new", "new"]
    assert preview.rows[1].errors == (THROUGH,)
    window = preview.simplefin_window
    assert (window.failing, window.accepted_from) == (True, date(2026, 10, 6))


@pytest.mark.django_db
def test_a_link_absent_from_the_latest_read_accepts_rows_after_its_last_synced_day():
    owner, checking, connection = _synced_checking()
    _sync_payload(owner, connection, _read_without_the_account())

    preview = _preview(checking)

    assert _statuses(preview) == ["new", "invalid", "new", "new"]
    assert preview.rows[1].errors == (THROUGH,)


@pytest.mark.django_db
def test_a_healthy_link_still_refuses_every_row_from_the_cutover():
    owner, checking, connection = _synced_checking()
    ledger.sync(owner, connection, SYNCED)

    preview = _preview(checking)

    assert _statuses(preview) == ["new", "invalid", "invalid", "invalid"]
    assert {row.errors for row in preview.rows[1:]} == {(FROM_CUTOVER,)}
    assert preview.simplefin_window.failing is False


@pytest.mark.django_db
def test_an_error_on_another_bank_leaves_the_link_healthy():
    owner, checking, connection = _synced_checking()
    payload = _read_with_bank_error("CON-2")
    payload["connections"].append({"conn_id": "CON-2", "name": "Other Synthetic Bank", "org_id": "ORG-2"})
    _sync_payload(owner, connection, payload)

    assert _statuses(_preview(checking)) == ["new", "invalid", "invalid", "invalid"]


@pytest.mark.django_db
@pytest.mark.parametrize(("age", "failing"), [
    (N_DAYS - timedelta(hours=1), False),
    (N_DAYS + timedelta(hours=1), True),
], ids=["recent-read", "no-read-for-n-days"])
def test_a_link_with_no_successful_read_for_n_days_is_failing(age, failing):
    from finance.simplefin_health import LINK_FAILING_AFTER

    assert LINK_FAILING_AFTER == N_DAYS
    _owner, checking, connection = _synced_checking()
    read = SimpleFinConnection.objects.get(pk=connection.pk).last_read
    read["at"] = (timezone.now() - age).isoformat()
    SimpleFinConnection.objects.filter(pk=connection.pk).update(last_read=read)

    preview = _preview(checking)

    assert preview.simplefin_window.failing is failing
    assert _statuses(preview)[2] == ("new" if failing else "invalid")


@pytest.mark.django_db
def test_a_disabled_connection_is_failing():
    _owner, checking, connection = _synced_checking()
    SimpleFinConnection.objects.filter(pk=connection.pk).update(disabled=True)

    assert _statuses(_preview(checking)) == ["new", "invalid", "new", "new"]


@pytest.mark.django_db
def test_a_failing_link_with_no_synced_rows_accepts_rows_from_the_cutover():
    owner = ledger.make_member()
    ledger.make_household(owner)
    checking = ledger.make_account(owner, Account.Type.CHECKING)
    connection = ledger.link_simplefin(owner, checking, CUTOVER)
    _sync_payload(owner, connection, _read_with_bank_error())

    preview = _preview(checking)

    assert _statuses(preview) == ["new", "new", "new", "new"]
    assert preview.simplefin_window.accepted_from == CUTOVER


@pytest.mark.django_db
def test_gap_rows_import_once_and_a_reimport_finds_them_duplicate():
    owner, checking, connection = _synced_checking()
    _sync_payload(owner, connection, _read_with_bank_error())

    first = ledger.import_file(owner, checking, CSV, mapping=MAPPING, source=ImportBatch.Source.HUNTINGTON)
    again = ledger.import_file(owner, checking, CSV, mapping=MAPPING, source=ImportBatch.Source.HUNTINGTON)

    assert (first.new_count, first.invalid_count) == (3, 1)
    assert (again.new_count, again.duplicate_count, again.invalid_count) == (0, 3, 1)
    active = Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE)
    assert sorted(active.values_list("transaction_date", "amount_minor")) == [
        (date(2026, 9, 30), -100), (date(2026, 10, 2), -1100), (date(2026, 10, 5), -2200),
        (date(2026, 10, 6), -300), (date(2026, 10, 8), -400),
    ]
    assert ledger.totals(owner, [checking]).spending == 100 + 1100 + 2200 + 300 + 400


@pytest.mark.django_db
def test_the_window_reads_only_the_account_asked_about():
    owner, checking, connection = _synced_checking()
    _sync_payload(owner, connection, _read_with_bank_error())
    unlinked = ledger.make_account(owner, Account.Type.CHECKING, name="Synthetic Unlinked")
    from finance.csv_import.services import simplefin_window

    assert simplefin_window(unlinked) is None


# The preview page -------------------------------------------------------------


@pytest.fixture
def staging(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def _preview_page(owner, account):
    client = signed_in(owner)
    url = reverse("csv-import-preview", args=(account.pk,))
    token = client.post(url, {
        "action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
    }).context["mapping_form"].initial["token"]
    return client.post(url, {
        "token": token, "date_column": "Date", "description_column": "Description",
        "date_format": "iso", "number_format": "dot_none", "amount_mode": "signed",
        "amount_column": "Amount", "debit_column": "", "credit_column": "", "currency_column": "",
        "action": "preview",
    })


@pytest.mark.django_db
def test_the_preview_states_the_day_rows_are_accepted_from_while_the_link_is_failing(staging):
    owner, checking, connection = _synced_checking()
    _sync_payload(owner, connection, _read_with_bank_error())

    page = _preview_page(owner, checking).content.decode()

    assert "Rows dated 2026-10-06 or later are accepted" in page
    assert "2026-10-01 through 2026-10-05" in page


@pytest.mark.django_db
def test_the_preview_for_a_healthy_link_states_no_accepted_day(staging):
    owner, checking, _connection = _synced_checking()

    page = _preview_page(owner, checking).content.decode()

    assert "are accepted" not in page
    assert FROM_CUTOVER in page
