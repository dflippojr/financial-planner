"""Days a file import supplied after a SimpleFIN cut-over belong to the file (#743).

While a link is failing (#735), a file may fill the days after its last synced
row. The link then has a second boundary derived from the ledger: the latest
`date_range_end` of an active file batch that reaches past the cut-over. A
sync skips SimpleFIN rows posted on or before it, as it skips rows before the
cut-over, so a recovered link never supplies a day the file already did.
Undoing the gap file removes the boundary. Dates only, never amounts (#671,
#732). All data is synthetic.
"""

from collections import defaultdict
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from finance.csv_import.parser import preview_csv, read_csv
from finance.csv_import.services import commit_csv_import, undo_import_batch
from finance.models import AccountLink, ImportBatch, Transaction
from tests.properties import harness as ledger
from tests.properties.strategies import Row
from tests.test_csv_failing_link import (
    CUTOVER,
    MAPPING,
    SYNCED,
    _read_with_bank_error,
    _sync_payload,
    _synced_checking,
    staging,  # noqa: F401 - a pytest fixture used by the preview test below
)
from tests.test_simplefin import signed_in

SF = ImportBatch.Source.SIMPLEFIN
FILE = ImportBatch.Source.HUNTINGTON

GAP_CSV = (
    b"Date,Description,Amount\n"
    b"2026-10-06,SYN CSV GAP ONE,-3.00\n"
    b"2026-10-08,SYN CSV GAP TWO,-4.00\n"
)
# What SimpleFIN returns for the same purchases once the link recovers.
BACKFILL = [
    ("g1", Row(date(2026, 10, 6), -300, "SYN SF GAP ONE", "", "")),
    ("g2", Row(date(2026, 10, 8), -400, "SYN SF GAP TWO", "", "")),
]
LATER = [("c", Row(date(2026, 10, 10), -500, "SYN SF LATER", "", ""))]


def _import_file(owner, account, content, *, end=None):
    """Commit a file the way the import page does, with the date range the member confirmed."""
    document = read_csv(content)
    dates = [row.transaction_date for row in preview_csv(document, MAPPING).rows if row.transaction_date]
    return commit_csv_import(
        owner, account.pk, content=content, document=document, mapping=MAPPING, source=FILE,
        date_range_start=min(dates), date_range_end=end or max(dates),
    )


def _fail(owner, connection):
    _sync_payload(owner, connection, _read_with_bank_error())


def _ledger(account):
    rows = Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE)
    return sorted(rows.values_list("transaction_date", "amount_minor", "import_batch__source"))


def _assert_no_day_from_both_sources(account):
    sources = defaultdict(set)
    for day, _amount, source in _ledger(account):
        sources[day].add(source)
    assert {day: found for day, found in sources.items() if len(found) > 1} == {}


@pytest.mark.django_db
def test_a_recovered_sync_skips_the_days_a_gap_file_supplied():
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    _import_file(owner, checking, GAP_CSV)

    ledger.sync(owner, connection, SYNCED + BACKFILL + LATER)

    assert _ledger(checking) == [
        (date(2026, 10, 2), -1100, SF), (date(2026, 10, 5), -2200, SF),
        (date(2026, 10, 6), -300, FILE), (date(2026, 10, 8), -400, FILE),
        (date(2026, 10, 10), -500, SF),
    ]
    _assert_no_day_from_both_sources(checking)
    assert ledger.totals(owner, [checking]).spending == 1100 + 2200 + 300 + 400 + 500


@pytest.mark.django_db
def test_undoing_the_gap_file_lets_a_later_sync_supply_its_days():
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    gap = _import_file(owner, checking, GAP_CSV)
    ledger.sync(owner, connection, SYNCED + BACKFILL + LATER)

    undo_import_batch(owner, checking.pk, gap.batch.pk)
    ledger.sync(owner, connection, SYNCED + BACKFILL + LATER)

    assert _ledger(checking) == [
        (date(2026, 10, 2), -1100, SF), (date(2026, 10, 5), -2200, SF),
        (date(2026, 10, 6), -300, SF), (date(2026, 10, 8), -400, SF),
        (date(2026, 10, 10), -500, SF),
    ]


@pytest.mark.django_db
def test_a_file_exported_partway_through_its_last_day_owns_that_day():
    """The boundary is the range the member confirmed, not the latest row date."""
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    # Exported on the morning of 10-09, before that day's purchase posted.
    _import_file(owner, checking, GAP_CSV, end=date(2026, 10, 9))
    evening = [("e", Row(date(2026, 10, 9), -900, "SYN SF EVENING", "", ""))]

    ledger.sync(owner, connection, SYNCED + BACKFILL + evening + LATER)

    assert _ledger(checking) == [
        (date(2026, 10, 2), -1100, SF), (date(2026, 10, 5), -2200, SF),
        (date(2026, 10, 6), -300, FILE), (date(2026, 10, 8), -400, FILE),
        (date(2026, 10, 10), -500, SF),
    ]


@pytest.mark.django_db
def test_two_gap_files_own_their_days_and_undoing_the_later_hands_back_only_its_days():
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    _import_file(owner, checking, GAP_CSV)
    between = LATER + [("d", Row(date(2026, 10, 12), -600, "SYN SF BETWEEN", "", ""))]
    ledger.sync(owner, connection, SYNCED + BACKFILL + between)
    _fail(owner, connection)
    second = _import_file(owner, checking, (
        b"Date,Description,Amount\n"
        b"2026-10-12,SYN CSV BETWEEN,-6.00\n"
        b"2026-10-14,SYN CSV GAP THREE,-7.00\n"
        b"2026-10-15,SYN CSV GAP FOUR,-8.00\n"
    ))
    assert (second.new_count, second.invalid_count) == (2, 1)
    backfill_two = [
        ("g3", Row(date(2026, 10, 14), -700, "SYN SF GAP THREE", "", "")),
        ("g4", Row(date(2026, 10, 15), -800, "SYN SF GAP FOUR", "", "")),
    ]
    after = [("f", Row(date(2026, 10, 17), -1000, "SYN SF AFTER", "", ""))]
    feed = SYNCED + BACKFILL + between + backfill_two + after

    ledger.sync(owner, connection, feed)

    assert _ledger(checking) == [
        (date(2026, 10, 2), -1100, SF), (date(2026, 10, 5), -2200, SF),
        (date(2026, 10, 6), -300, FILE), (date(2026, 10, 8), -400, FILE),
        (date(2026, 10, 10), -500, SF), (date(2026, 10, 12), -600, SF),
        (date(2026, 10, 14), -700, FILE), (date(2026, 10, 15), -800, FILE),
        (date(2026, 10, 17), -1000, SF),
    ]
    _assert_no_day_from_both_sources(checking)

    undo_import_batch(owner, checking.pk, second.batch.pk)
    ledger.sync(owner, connection, feed)

    assert _ledger(checking) == [
        (date(2026, 10, 2), -1100, SF), (date(2026, 10, 5), -2200, SF),
        (date(2026, 10, 6), -300, FILE), (date(2026, 10, 8), -400, FILE),
        (date(2026, 10, 10), -500, SF), (date(2026, 10, 12), -600, SF),
        (date(2026, 10, 14), -700, SF), (date(2026, 10, 15), -800, SF),
        (date(2026, 10, 17), -1000, SF),
    ]


@pytest.mark.django_db
def test_a_gap_file_does_not_trap_the_link_when_its_cutover_is_changed():
    """check_cutover measures "after every active file row" only up to the cut-over; the gap is owned."""
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    _import_file(owner, checking, GAP_CSV)

    assert ledger.save_link(owner, connection, checking, date(2026, 9, 28)) is True
    assert AccountLink.objects.get(account=checking).cutover_date == date(2026, 9, 28)
    # The earliest synced row still bounds it from above.
    assert ledger.save_link(owner, connection, checking, date(2026, 10, 3)) is False
    assert ledger.save_link(owner, connection, checking, CUTOVER) is True


@pytest.mark.django_db
def test_the_preview_says_which_days_the_file_will_own(staging):  # noqa: F811 - the imported fixture
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    client = signed_in(owner)
    url = reverse("csv-import-preview", args=(checking.pk,))
    token = client.post(url, {
        "action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", GAP_CSV, "text/csv"),
    }).context["mapping_form"].initial["token"]

    page = client.post(url, {
        "token": token, "date_column": "Date", "description_column": "Description",
        "date_format": "iso", "number_format": "dot_none", "amount_mode": "signed",
        "amount_column": "Amount", "debit_column": "", "credit_column": "", "currency_column": "",
        "action": "preview",
    }).content.decode()

    assert "This file will own 2026-10-06 through 2026-10-08" in page
    assert "skips SimpleFIN transactions posted on or before 2026-10-08" in page


@pytest.mark.django_db
def test_connections_says_which_days_file_imports_own():
    owner, checking, connection = _synced_checking()
    _fail(owner, connection)
    _import_file(owner, checking, GAP_CSV)

    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()

    assert "File imports own 2026-10-06 through 2026-10-08" in page
    assert "skips SimpleFIN transactions posted on or before 2026-10-08" in page


@pytest.mark.django_db
def test_connections_names_no_owned_days_without_a_gap_file():
    owner, _checking, _connection = _synced_checking()

    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()

    assert "File imports own" not in page


@pytest.mark.django_db
def test_a_file_imported_while_the_link_is_healthy_owns_no_days():
    """Every row on or after the cut-over was refused, so however far its range runs it supplied no day."""
    owner, checking, connection = _synced_checking()
    refused = _import_file(owner, checking, GAP_CSV, end=date(2026, 10, 9))
    assert (refused.new_count, refused.invalid_count) == (0, 2)

    ledger.sync(owner, connection, SYNCED + BACKFILL + LATER)

    assert [source for _day, _amount, source in _ledger(checking)] == [SF] * 5
    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()
    assert "skips SimpleFIN transactions" not in page
