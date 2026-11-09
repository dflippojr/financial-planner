"""Missing import skips windows that end before an account's first import (#713).

Owner decision (2026-11-08): a window that ends before the account's earliest
`ImportBatch.date_range_start` is not flagged; gaps after the first import still
are; an account with no batches keeps being flagged.
"""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance.cash_flow import GROUPING_MONTH, GROUPING_YEAR, cash_flow_report
from finance.category_services import ensure_household_categories
from finance.models import Account, Household, ImportBatch, Membership, Person
from finance.monthly_review import compute_monthly_review_facts
from tests.page_payload import json_script_payload

pytestmark = pytest.mark.django_db

PASSWORD = "Synthetic-passphrase-42!"
FIRST_IMPORT = date(2026, 3, 1)


def make_owner(username="prehistory-owner"):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return person


def make_account(owner, name="Synthetic Checking"):
    return Account.objects.create(name=name, account_type=Account.Type.CHECKING, owner=owner)


def make_batch(owner, account, start, end):
    return ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=f"{account.pk:08d}{start:%Y%m%d}{end:%Y%m%d}".ljust(64, "c"),
        date_range_start=start,
        date_range_end=end,
    )


def missing(owner, account, start, end):
    """The accounts flagged for the one window `start`..`end` (inside one calendar year)."""
    report = cash_flow_report(
        owner, date_from=start, date_to=end, grouping=GROUPING_YEAR, account=account, today=date(2027, 1, 1)
    )
    (period,) = report.periods
    assert (period.start, period.end) == (start, end)
    return period.missing_accounts


def account_with_march_and_may_imports(owner):
    """First import starts 1 March; April has no import; May is imported."""
    account = make_account(owner)
    make_batch(owner, account, date(2026, 5, 1), date(2026, 5, 31))
    make_batch(owner, account, FIRST_IMPORT, date(2026, 3, 31))
    return account


def test_window_ending_before_first_import_is_not_flagged():
    owner = make_owner()
    account = account_with_march_and_may_imports(owner)

    assert missing(owner, account, date(2026, 1, 1), date(2026, 1, 31)) == []
    # The last day before the first import is still pre-history.
    assert missing(owner, account, date(2026, 2, 1), date(2026, 2, 28)) == []


def test_gap_after_first_import_is_still_flagged():
    owner = make_owner()
    account = account_with_march_and_may_imports(owner)

    assert missing(owner, account, date(2026, 4, 1), date(2026, 4, 30)) == [account.name]
    assert missing(owner, account, date(2026, 6, 1), date(2026, 6, 30)) == [account.name]


def test_window_reaching_the_first_import_is_checked_as_before():
    owner = make_owner()
    account = account_with_march_and_may_imports(owner)

    # Ending on the first import day, or straddling it, is not wholly before it:
    # the earliest batch overlaps the window, so the window is covered.
    assert missing(owner, account, date(2026, 2, 1), FIRST_IMPORT) == []
    assert missing(owner, account, date(2026, 2, 15), date(2026, 3, 15)) == []
    # A window straddling a later gap is checked against every batch, as before.
    assert missing(owner, account, date(2026, 3, 15), date(2026, 4, 15)) == []
    assert missing(owner, account, date(2026, 4, 2), date(2026, 4, 29)) == [account.name]


def test_account_with_no_batches_is_flagged_for_every_window():
    owner = make_owner()
    account = make_account(owner)

    for month in (1, 6, 12):
        assert missing(owner, account, date(2026, month, 1), date(2026, month, 28)) == [account.name]


def test_undone_and_hand_entered_batches_also_set_the_first_import_date():
    owner = make_owner()
    account = make_account(owner)
    make_batch(owner, account, FIRST_IMPORT, date(2026, 3, 31))
    # An undone import or a hand-entered row shows the account had history then,
    # so the gap after it is still flagged, though neither covers a window itself.
    manual = make_batch(owner, account, date(2025, 6, 1), date(2025, 6, 1))
    manual.source = ImportBatch.Source.MANUAL
    manual.save(update_fields=["source"])
    undone = make_batch(owner, account, date(2025, 9, 1), date(2025, 9, 30))
    undone.status = ImportBatch.Status.ARCHIVED
    undone.archived_at = timezone.now()
    undone.save(update_fields=["status", "archived_at"])

    assert missing(owner, account, date(2025, 5, 1), date(2025, 5, 31)) == []
    assert missing(owner, account, date(2025, 6, 1), date(2025, 6, 30)) == [account.name]
    assert missing(owner, account, date(2025, 9, 1), date(2025, 9, 30)) == [account.name]
    assert missing(owner, account, date(2026, 4, 1), date(2026, 4, 30)) == [account.name]


def test_home_table_and_chart_skip_months_before_first_import():
    owner = make_owner()
    account_with_march_and_may_imports(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(
        reverse("home"), {"date_from": "2026-01-01", "date_to": "2026-06-30", "grouping": "month"}
    )

    assert response.status_code == 200
    expected = [False, False, False, True, False, True]
    assert [period.missing_import for period in response.context["report"].periods] == expected
    payload = json_script_payload(response.content.decode(), "cash-flow-chart-data")
    actual_rows = [row for row in payload["periods"] if not row["projected"]]
    assert [row["missing_import"] for row in actual_rows] == expected


def test_monthly_review_skips_a_month_before_first_import():
    owner = make_owner()
    account_with_march_and_may_imports(owner)

    before = compute_monthly_review_facts(owner, date(2026, 2, 1), today=date(2026, 7, 3))
    gap = compute_monthly_review_facts(owner, date(2026, 4, 1), today=date(2026, 7, 3))

    assert before["missing_import"] is False
    assert gap["missing_import"] is True


def test_first_import_lookup_does_not_grow_with_the_number_of_windows():
    owner = make_owner()
    for index in range(3):
        account = make_account(owner, name=f"Synthetic Account {index}")
        make_batch(owner, account, FIRST_IMPORT, date(2026, 3, 31))

    def query_count(date_to):
        with CaptureQueriesContext(connection) as queries:
            cash_flow_report(
                owner, date_from=date(2025, 1, 1), date_to=date_to, grouping=GROUPING_MONTH, today=date(2026, 12, 31)
            )
        return len(queries)

    assert query_count(date(2025, 3, 31)) == query_count(date(2026, 12, 31))
