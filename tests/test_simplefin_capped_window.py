"""A sync window SimpleFIN capped at 90 days claims only the days it fetched (#640).

SimpleFIN Bridge returns at most 90 days, counted back from the end of the
request, and says so with a `gen.api` notice (#629). A first sync for a link
whose cut-over is older than that used to record a batch from the cut-over, so
the days SimpleFIN never sent were refused by the CSV importer and counted as
covered: a silent gap. The fake Bridge below caps the window the same way.
Dates are relative to today because the cap is. Synthetic data only.
"""

from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from finance.audit_models import AuditEvent
from finance.calculated_balances import calculate
from finance.cash_flow import GROUPING_WEEK, cash_flow_report
from finance.models import AccountLink, ImportBatch, Transaction
from finance.recurring_review import missing_import_on
from finance.simplefin_services import save_account_links, sync_connection
from tests.properties import harness as ledger
from tests.test_simplefin import account_payload, connect_owner, make_account, make_household, make_person

CAPPED_NOTICE = {"code": "gen.api", "msg": "Requested date range exceeds limit of 90 days and was capped."}
RECOMMENDED_NOTICE = {
    "code": "gen.api",
    "msg": "Requested date range exceeds recommended range of 45 days. In the future, this may be capped.",
}
LIMIT_SECONDS = 90 * 86400


def _today():
    return timezone.localdate()


def _stamp(day):
    return int(timezone.make_aware(datetime.combine(day, time(12))).timestamp())


def _txn(txn_id, day, amount="-25.00"):
    return {"id": txn_id, "posted": _stamp(day), "amount": amount, "description": "Synthetic Stream"}


class FakeBridge:
    """Answers like SimpleFIN Bridge: a window over 90 days is cut to the last 90, with the notice."""

    def __init__(self, transactions, *, balance_day=None):
        self.transactions = transactions
        self.balance_day = balance_day or _today()
        self.requests = []
        self.served_from = None

    def __call__(self, access_url, *, start_date=None, end_date=None, balances_only=False):
        self.requests.append((start_date, end_date))
        payload = account_payload(transactions=[])
        payload["accounts"][0]["balance-date"] = _stamp(self.balance_day)
        if start_date is None:
            return payload
        start = start_date
        if end_date - start_date > LIMIT_SECONDS:
            start = end_date - LIMIT_SECONDS
            payload["errlist"].append(dict(CAPPED_NOTICE))
        self.served_from = start
        payload["accounts"][0]["transactions"] = [
            item for item in self.transactions if start <= item["posted"] < end_date
        ]
        return payload

    def first_whole_day(self):
        """The first local day the last answer served in full: the day it started in was only partly sent."""
        started = timezone.localtime(datetime.fromtimestamp(self.served_from, tz=UTC))
        return started.date() + timedelta(days=1)


def _huntington(*rows):
    lines = [b"Date,Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number"]
    for index, (day, amount) in enumerate(rows, start=1):
        lines.append(f"{day:%m/%d/%Y},R{index},SYNTHETIC GROCER,Purchase,{amount},Food,T-{index}".encode())
    return b"\n".join(lines) + b"\n"


def _link(owner, connection, account, cutover):
    save_account_links(owner, connection.pk, [{
        "simplefin_account_id": "CON-1:sf-checking", "action": "link",
        "account_id": account.pk, "cutover_date": cutover,
    }])


def _sync(owner, connection, monkeypatch, bridge):
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", bridge)
    return sync_connection(owner, connection.pk, ignore_rate_limit=True)


def _sync_batches(account):
    return ImportBatch.objects.filter(
        account=account, source=ImportBatch.Source.SIMPLEFIN, status=ImportBatch.Status.ACTIVE,
    ).order_by("date_range_start")


def _first_synced_day(account):
    return _sync_batches(account).values_list("date_range_start", flat=True).first()


def _cutover(account):
    return AccountLink.objects.filter(account=account).values_list("cutover_date", flat=True).get()


def _active(account):
    return Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE)


def _owner_with_checking():
    owner = make_person("owner")
    make_household(owner)
    return owner, make_account(owner)


def _linked_120_days_back(monkeypatch, *, csv_before=None):
    """An account linked with a cut-over 120 days back, optionally after a CSV import before it."""
    owner, checking = _owner_with_checking()
    if csv_before is not None:
        ledger.import_file(owner, checking, _huntington(*csv_before), profile="huntington")
    connection = connect_owner(owner, monkeypatch)
    _link(owner, connection, checking, _today() - timedelta(days=120))
    return owner, checking, connection


# Acceptance 1: the batch starts at most 90 days back, the days before it are
# uncovered, and a CSV import may supply them.

@pytest.mark.django_db
def test_a_capped_first_sync_records_a_batch_that_starts_at_most_90_days_back(monkeypatch):
    today = _today()
    owner, checking, connection = _linked_120_days_back(monkeypatch)
    requested = _cutover(checking)
    bridge = FakeBridge([_txn("old", today - timedelta(days=115)), _txn("recent", today - timedelta(days=10))])

    _sync(owner, connection, monkeypatch, bridge)

    first = _first_synced_day(checking)
    assert first == bridge.first_whole_day()
    assert first >= today - timedelta(days=90)
    # The batch keeps the day the link asked from.
    assert _sync_batches(checking).get().requested_from == requested
    assert not _sync_batches(checking).filter(date_range_start__lt=first).exists()
    # Only what SimpleFIN sent was imported.
    assert list(_active(checking).values_list("source_transaction_id", flat=True)) == ["recent"]


@pytest.mark.django_db
def test_the_days_before_a_capped_first_sync_show_as_uncovered(monkeypatch):
    today = _today()
    gap_day = today - timedelta(days=100)
    owner, checking, connection = _linked_120_days_back(monkeypatch)

    _sync(owner, connection, monkeypatch, FakeBridge([]))

    assert missing_import_on(owner, [checking], gap_day)
    assert not missing_import_on(owner, [checking], today - timedelta(days=10))


@pytest.mark.django_db
def test_the_cash_flow_report_flags_the_weeks_a_capped_first_sync_did_not_fetch(monkeypatch):
    today = _today()
    owner, checking, connection = _linked_120_days_back(
        monkeypatch, csv_before=[(today - timedelta(days=121), "-10.00")],
    )

    _sync(owner, connection, monkeypatch, FakeBridge([]))

    first = _first_synced_day(checking)
    report = cash_flow_report(
        owner, date_from=today - timedelta(days=112), date_to=today - timedelta(days=7),
        grouping=GROUPING_WEEK, account=checking, today=today,
    )
    gap_weeks = [period for period in report.periods if period.end < first]
    synced_weeks = [period for period in report.periods if period.start >= first]
    assert gap_weeks and all(period.missing_import for period in gap_weeks)
    assert synced_weeks and not any(period.missing_import for period in synced_weeks)


@pytest.mark.django_db
def test_cash_flow_flags_the_unfetched_weeks_even_when_the_capped_sync_is_the_first_import(monkeypatch):
    """Nothing was imported before the sync, but the link asked for history from its cut-over (#713, #640)."""
    today = _today()
    owner, checking, connection = _linked_120_days_back(monkeypatch)
    requested = _cutover(checking)

    _sync(owner, connection, monkeypatch, FakeBridge([]))

    first = _first_synced_day(checking)
    report = cash_flow_report(
        owner, date_from=requested, date_to=today - timedelta(days=7),
        grouping=GROUPING_WEEK, account=checking, today=today,
    )
    gap_weeks = [period for period in report.periods if requested <= period.start and period.end < first]
    synced_weeks = [period for period in report.periods if period.start >= first]
    assert gap_weeks and all(period.missing_import for period in gap_weeks)
    assert synced_weeks and not any(period.missing_import for period in synced_weeks)
    # Weeks before the requested history start were never asked for.
    earlier = cash_flow_report(
        owner, date_from=requested - timedelta(days=60), date_to=requested - timedelta(days=8),
        grouping=GROUPING_WEEK, account=checking, today=today,
    )
    assert earlier.periods and not any(period.missing_import for period in earlier.periods)


@pytest.mark.django_db
def test_a_csv_import_can_supply_the_days_a_capped_first_sync_did_not_fetch(monkeypatch):
    today = _today()
    gap_day = today - timedelta(days=100)
    synced_day = today - timedelta(days=10)
    owner, checking, connection = _linked_120_days_back(monkeypatch)
    _sync(owner, connection, monkeypatch, FakeBridge([_txn("recent", synced_day)]))

    result = ledger.import_file(owner, checking, _huntington((gap_day, "-30.00"), (synced_day, "-25.00")),
                                profile="huntington")

    assert result.new_count == 1
    assert _active(checking).filter(transaction_date=gap_day, import_batch__source=ImportBatch.Source.HUNTINGTON)
    # The synced day is still SimpleFIN's alone: it counts once.
    assert _active(checking).filter(transaction_date=synced_day).count() == 1


@pytest.mark.django_db
def test_a_capped_first_sync_moves_the_cutover_to_the_first_day_it_fetched_and_audits_it(monkeypatch):
    owner, checking, connection = _linked_120_days_back(monkeypatch)
    before = AuditEvent.objects.filter(
        action=AuditEvent.Action.SIMPLEFIN_LINKS_CHANGED, target_id=connection.pk,
    ).count()

    result = _sync(owner, connection, monkeypatch, FakeBridge([]))

    first = _first_synced_day(checking)
    assert _cutover(checking) == first
    events = AuditEvent.objects.filter(action=AuditEvent.Action.SIMPLEFIN_LINKS_CHANGED, target_id=connection.pk)
    assert events.count() == before + 1
    assert events.filter(changed_fields=["cutover"]).count() == 1
    assert checking.name in result["result"]
    assert "90 days" in result["result"]


# Acceptance 2: calculated month-ends whose following days fall in the gap are incomplete.

@pytest.mark.django_db
def test_calculated_month_ends_before_a_capped_first_sync_are_incomplete(monkeypatch):
    today = _today()
    # A CSV row the day before the cut-over: before #640 the CSV and sync
    # batches together claimed every day from there through today.
    owner, checking, connection = _linked_120_days_back(
        monkeypatch, csv_before=[(today - timedelta(days=150), "-10.00"), (today - timedelta(days=121), "-10.00")],
    )

    bridge = FakeBridge([_txn("recent", today - timedelta(days=10))])
    _sync(owner, connection, monkeypatch, bridge)

    # SimpleFIN sent every day from fetched_from on, and none in full before it.
    fetched_from = bridge.first_whole_day()
    never_fetched = fetched_from - timedelta(days=1)
    rows = calculate(checking).rows
    in_gap = [row for row in rows if row.month_end + timedelta(days=1) <= never_fetched]
    assert in_gap, "the gap should hold at least one month-end"
    for row in in_gap:
        assert row.incomplete, row.month_end
    for row in rows:
        if row.month_end + timedelta(days=1) >= fetched_from:
            assert not row.incomplete, row.month_end


# Links that already synced keep their cut-over and batches.

@pytest.mark.django_db
def test_a_capped_sync_leaves_a_link_whose_batches_cover_its_cutover_alone(monkeypatch):
    today = _today()
    owner, checking, connection = _linked_120_days_back(monkeypatch)
    cutover = _cutover(checking)
    # Synced earlier, when the cut-over was still inside the window.
    earlier = ImportBatch.objects.create(
        account=checking, imported_by=owner, source=ImportBatch.Source.SIMPLEFIN,
        source_file_sha256="a" * 64, simplefin_account_id="CON-1:sf-checking",
        date_range_start=cutover, date_range_end=today - timedelta(days=30),
    )

    _sync(owner, connection, monkeypatch, FakeBridge([]))

    assert _cutover(checking) == cutover
    earlier.refresh_from_db()
    assert (earlier.date_range_start, earlier.date_range_end) == (cutover, today - timedelta(days=30))
    later = _sync_batches(checking).exclude(pk=earlier.pk).get()
    assert later.date_range_start == today - timedelta(days=29)


@pytest.mark.django_db
def test_a_capped_sync_after_a_long_pause_does_not_claim_the_days_it_could_not_fetch(monkeypatch):
    today = _today()
    owner, checking, connection = _linked_120_days_back(monkeypatch)
    cutover = _cutover(checking)
    ImportBatch.objects.create(
        account=checking, imported_by=owner, source=ImportBatch.Source.SIMPLEFIN,
        source_file_sha256="b" * 64, simplefin_account_id="CON-1:sf-checking",
        date_range_start=cutover, date_range_end=cutover + timedelta(days=5),
    )

    bridge = FakeBridge([])
    _sync(owner, connection, monkeypatch, bridge)

    later = _sync_batches(checking).exclude(date_range_start=cutover).get()
    assert later.date_range_start == bridge.first_whole_day()
    assert later.requested_from == cutover + timedelta(days=6)
    assert later.date_range_start >= today - timedelta(days=90)
    assert missing_import_on(owner, [checking], cutover + timedelta(days=10))


@pytest.mark.django_db
def test_an_uncapped_sync_still_covers_from_the_cutover(monkeypatch):
    today = _today()
    owner, checking = _owner_with_checking()
    connection = connect_owner(owner, monkeypatch)
    cutover = today - timedelta(days=60)
    _link(owner, connection, checking, cutover)

    _sync(owner, connection, monkeypatch, FakeBridge([_txn("recent", today - timedelta(days=10))]))

    assert _first_synced_day(checking) == cutover
    assert _cutover(checking) == cutover
    assert _sync_batches(checking).get().requested_from is None


@pytest.mark.django_db
def test_a_cap_that_crosses_a_daylight_saving_change_starts_at_the_first_whole_local_day(monkeypatch):
    """90 x 24 hours back from 23:30 EST on Nov 12 is 00:30 EDT on Aug 15, so Aug 16 is the first whole day."""
    with timezone.override("America/New_York"), \
            patch("django.utils.timezone.now", return_value=datetime(2026, 11, 13, 4, 30, tzinfo=UTC)):
        owner, checking, connection = _linked_120_days_back(monkeypatch)
        _sync(owner, connection, monkeypatch, FakeBridge([]))

    assert _first_synced_day(checking) == date(2026, 8, 16)
    assert _cutover(checking) == date(2026, 8, 16)


# Reading the notice.

def test_capped_window_days_reads_only_the_capped_notice():
    from finance.simplefin_errors import capped_window_days

    assert capped_window_days({"errlist": [dict(CAPPED_NOTICE)]}) == 90
    assert capped_window_days({"errlist": [dict(RECOMMENDED_NOTICE)]}) is None
    assert capped_window_days({"errlist": [{**CAPPED_NOTICE, "account_id": "sf-checking"}]}) is None
    assert capped_window_days({"errlist": [{**CAPPED_NOTICE, "code": "gen.conn"}]}) is None
    assert capped_window_days({"errlist": ["Requested date range exceeds limit of 90 days and was capped."]}) is None
    assert capped_window_days({}) is None
