"""Saved calculated balances follow every change at or before their anchor (#641, #676).

Owner decision (2026-11-12): an import, edit or deletion that changes a
balance or a transaction at or before the anchor recalculates the account's
saved calculated rows under the account lock, and deletes them when no real
balance remains. SimpleFIN and manual rows are never changed. Synthetic data only.
"""

from datetime import date
from unittest.mock import patch

import pytest
from django.urls import reverse

from finance.calculated_balances import CalculationError, calculate, save_calculated_balances
from finance.csv_import.parser import read_csv
from finance.csv_import.services import commit_csv_import, undo_import_batch
from finance.manual_entry_services import add_manual_transaction, delete_manual_transaction
from finance.models import Account, AuditEvent, BalanceSnapshot, ImportBatch, Transaction
from finance.net_worth import net_worth_report
from finance.simplefin_services import sync_connection
from finance.snapshot_services import delete_manual_snapshot, record_manual_snapshot, update_manual_snapshot
from tests.properties import harness
from tests.properties.strategies import simplefin_epoch, simplefin_payload
from tests.test_alerts import make_account, make_person, signed_in
from tests.test_calculated_balances import (
    CALCULATED,
    MANUAL,
    OCT_31,
    calculated,
    card_with_history,
    checking_with_history,
    real_rows,
)
from tests.test_manual_transactions import mapping

pytestmark = pytest.mark.django_db


def expected(account):
    """What a manual Calculate would save now: calculate()'s rows, or none when it refuses."""
    try:
        rows = calculate(account).rows
    except CalculationError:
        return []
    return [(row.month_end, row.balance_minor) for row in rows]


def balance_events(account):
    return AuditEvent.objects.filter(target_type=AuditEvent.TargetType.BALANCE, account_id=account.pk).count()


def import_csv(owner, account, *rows):
    """Commit a CSV of (day, amount_minor) rows through the import service."""
    lines = ["When,Memo,Amount,Currency"]
    lines += [f"{day:%m/%d/%Y},SYNTHETIC LATE ROW {n},{amount / 100:.2f},USD" for n, (day, amount) in enumerate(rows)]
    content = ("\n".join(lines) + "\n").encode()
    days = [day for day, _ in rows]
    return commit_csv_import(
        owner.user, account.pk, content=content, document=read_csv(content), mapping=mapping(),
        source=ImportBatch.Source.HUNTINGTON, date_range_start=min(days), date_range_end=max(days),
    )


def _owed_on(person, card, day):
    report = net_worth_report(person, date_from=day.replace(day=1), date_to=day, today=day)
    rows = [row for row in report.periods[-1].accounts if row.account_id == card.pk]
    # With no balance of any kind left, the card has no net worth row at all.
    return (rows[0].liabilities_minor, rows[0].source) if rows else None


# --- #676: the repro from the issue body ---------------------------------------------------


def test_editing_the_anchor_balance_leaves_saved_calculated_rows_wrong():
    owner = make_person("calc-owner")
    card = card_with_history(owner, anchor_source=MANUAL)  # 300.00 owed on Oct 31
    save_calculated_balances(owner, card.pk)
    anchor = BalanceSnapshot.objects.get(account=card, source=MANUAL)
    update_manual_snapshot(owner, card.pk, anchor.pk, snapshot_date=anchor.snapshot_date, amount_minor=35_000)
    wanted = [(row.month_end, row.balance_minor) for row in calculate(card).rows]
    assert calculated(card) == wanted == [(date(2026, 8, 31), -20_000), (date(2026, 9, 30), -41_000)]


def test_deleting_the_anchor_balance_leaves_calculated_rows_with_no_anchor():
    owner = make_person("calc-owner-2")
    card = card_with_history(owner, anchor_source=MANUAL)
    save_calculated_balances(owner, card.pk)
    anchor = BalanceSnapshot.objects.get(account=card, source=MANUAL)
    delete_manual_snapshot(owner, card.pk, anchor.pk)
    assert _owed_on(owner, card, date(2026, 11, 5)) != (36_000, CALCULATED)
    assert _owed_on(owner, card, date(2026, 11, 5)) is None
    assert calculated(card) == []


# --- #641: late-posted rows dated before the anchor ----------------------------------------


def test_a_csv_import_of_a_row_before_the_anchor_recalculates_saved_rows():
    owner = make_person("late-csv")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    before = calculated(checking)
    snapshots = real_rows(checking)

    import_csv(owner, checking, (date(2026, 10, 30), -403_773))

    assert calculated(checking) == expected(checking) != before
    assert real_rows(checking) == snapshots


def _sync_payload(rows, *, balance, balance_day):
    payload = simplefin_payload([])
    account = payload["accounts"][0]
    account["balance"] = f"{balance / 100:.2f}"
    account["balance-date"] = simplefin_epoch(balance_day)
    account["transactions"] = [
        {"id": f"SYN-LATE-{n}", "posted": simplefin_epoch(day), "amount": f"{amount / 100:.2f}",
         "description": f"SYNTHETIC SF ROW {n}"}
        for n, (day, amount) in rows
    ]
    return payload


def _sync(owner, connection, payload):
    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        return sync_connection(owner, connection.pk, ignore_rate_limit=True)


def test_a_simplefin_sync_that_brings_late_rows_before_the_anchor_recalculates_saved_rows():
    """The production case: the 11-03 balance already held Nov 2 debits that arrived on 11-04."""
    owner = make_person("late-sync")
    checking = make_account(owner)
    connection = harness.link_simplefin(owner, checking, date(2026, 8, 1))
    first = [(1, (date(2026, 8, 5), -10_000)), (2, (date(2026, 9, 10), 200_000))]
    _sync(owner, connection, _sync_payload(first, balance=100_000, balance_day=date(2026, 11, 3)))
    save_calculated_balances(owner, checking.pk)
    assert calculated(checking) == [(date(2026, 8, 31), -100_000), (date(2026, 9, 30), 100_000),
                                    (date(2026, 10, 31), 100_000)]
    anchor = real_rows(checking)[0]

    late = [*first, (3, (date(2026, 11, 2), -403_773))]
    _sync(owner, connection, _sync_payload(late, balance=100_000, balance_day=date(2026, 11, 4)))

    assert calculated(checking) == expected(checking) == [
        (date(2026, 8, 31), 303_773), (date(2026, 9, 30), 503_773), (date(2026, 10, 31), 503_773),
    ]
    assert real_rows(checking)[0] == anchor


def test_an_import_that_only_adds_rows_after_the_anchor_changes_nothing():
    owner = make_person("after-anchor")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    rows = list(BalanceSnapshot.objects.filter(account=checking).order_by("pk").values())
    events = balance_events(checking)

    import_csv(owner, checking, (date(2026, 11, 3), -1_234), (date(2026, 11, 4), 5_000))

    assert list(BalanceSnapshot.objects.filter(account=checking).order_by("pk").values()) == rows
    assert balance_events(checking) == events


# --- every other path that changes a balance or a row at or before the anchor ---------------


def test_undoing_an_import_recalculates_saved_rows():
    owner = make_person("undo-import")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    before = calculated(checking)
    batch = import_csv(owner, checking, (date(2026, 9, 2), -7_000)).batch
    assert calculated(checking) == expected(checking) != before

    undo_import_batch(owner, checking.pk, batch.pk)

    assert calculated(checking) == expected(checking) == before


def test_undoing_the_import_that_holds_every_row_deletes_the_calculated_rows():
    owner = make_person("undo-all")
    card = card_with_history(owner, anchor_source=MANUAL)
    save_calculated_balances(owner, card.pk)
    batch = ImportBatch.objects.get(account=card)

    undo_import_batch(owner, card.pk, batch.pk)

    assert calculated(card) == [] == expected(card)
    assert BalanceSnapshot.objects.filter(account=card, source=MANUAL).count() == 1


def test_adding_and_deleting_a_manual_transaction_before_the_anchor_recalculates():
    owner = make_person("manual-entry")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    before = calculated(checking)

    entry = add_manual_transaction(owner, checking.pk, transaction_date=date(2026, 8, 15), amount_minor=-2_500,
                                   description="SYNTHETIC CASH")
    assert calculated(checking) == expected(checking) != before

    delete_manual_transaction(owner, entry.pk)
    assert calculated(checking) == expected(checking) == before


def test_editing_a_transaction_amount_or_date_recalculates_saved_rows():
    owner = make_person("edit-row")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    before = calculated(checking)
    row = checking.transactions.get(transaction_date=date(2026, 8, 5))
    client = signed_in(owner)

    response = client.post(reverse("transaction-edit", args=[row.pk]),
                           {"transaction_date": "2026-08-05", "description": row.description, "amount": "-250.00"})
    assert response.status_code == 302
    assert calculated(checking) == expected(checking) != before

    moved = calculated(checking)
    response = client.post(reverse("transaction-edit", args=[row.pk]),
                           {"transaction_date": "2026-10-05", "description": row.description, "amount": "-250.00"})
    assert response.status_code == 302
    assert calculated(checking) == expected(checking) != moved


def test_recording_an_earlier_manual_balance_moves_the_anchor_and_recalculates():
    owner = make_person("earlier-manual")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    simplefin = real_rows(checking)

    record_manual_snapshot(owner, checking.pk, snapshot_date=date(2026, 9, 15), amount_minor=490_000)

    assert calculated(checking) == expected(checking)
    assert max(day for day, _ in calculated(checking)) < date(2026, 9, 15)
    assert real_rows(checking)[0] == simplefin[0]


def test_a_later_manual_balance_changes_nothing():
    owner = make_person("later-manual")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    before = list(BalanceSnapshot.objects.filter(account=checking, source=CALCULATED).values())
    events = balance_events(checking)

    record_manual_snapshot(owner, checking.pk, snapshot_date=date(2026, 11, 10), amount_minor=1)

    assert list(BalanceSnapshot.objects.filter(account=checking, source=CALCULATED).values()) == before
    assert balance_events(checking) == events + 1  # the manual balance itself


def test_recalculation_records_balance_audit_events_without_amounts():
    owner = make_person("recalc-audit")
    card = card_with_history(owner, anchor_source=MANUAL)
    save_calculated_balances(owner, card.pk)
    anchor = BalanceSnapshot.objects.get(account=card, source=MANUAL)
    events = balance_events(card)

    update_manual_snapshot(owner, card.pk, anchor.pk, snapshot_date=OCT_31, amount_minor=35_000)

    # The anchor edit, then one edit per changed calculated row.
    assert balance_events(card) == events + 3
    for event in AuditEvent.objects.filter(account_id=card.pk).order_by("-pk")[:3]:
        assert "35000" not in str(event.metadata) and "-20000" not in str(event.metadata)


def test_accounts_without_calculated_rows_are_left_alone():
    owner = make_person("no-calc")
    checking = checking_with_history(owner)
    import_csv(owner, checking, (date(2026, 10, 1), -1_000))
    assert calculated(checking) == []
    assert Account.objects.get(pk=checking.pk).transactions.filter(status=Transaction.Status.ACTIVE).count() == 7



def test_the_balances_page_says_calculated_rows_update_on_their_own():
    owner = make_person("page-copy")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    page = signed_in(owner).get(reverse("account-balances", args=[checking.pk])).content.decode()
    assert "They update on their own when its transactions or balances change." in page
    assert "calculate again" not in page


# --- concurrency: an import that waited for a Save sees the rows it saved (review of #733) ---


def _wait_until_blocked_on_a_lock(timeout=10):
    import time

    from django.db import connection

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if cursor.fetchone()[0]:
                return True
        time.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("in_household", [False, True], ids=["no-household", "household"])
def test_an_import_waiting_on_a_concurrent_save_still_recalculates(in_household):
    import threading

    from django.db import connection, connections, transaction

    from django.utils import timezone

    if connection.vendor != "postgresql":
        pytest.skip("row locks can only be exercised on PostgreSQL")
    from finance.lifecycle_services import lock_actor_household
    from finance.models import Membership

    owner = make_person("race-owner")
    checking = checking_with_history(owner)  # creates a household for its split categories
    if not in_household:
        Membership.objects.filter(person=owner).update(ended_at=timezone.now())
    # The precondition each case is about: whether a household lock serializes Save and import.
    with transaction.atomic():
        assert (lock_actor_household(owner)[0] is not None) is in_household
    saved, release = threading.Event(), threading.Event()
    errors = []

    def save():
        try:
            with transaction.atomic():
                save_calculated_balances(owner, checking.pk)
                saved.set()
                release.wait(timeout=20)  # hold the locks, rows still uncommitted
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    def late_import():
        try:
            import_csv(owner, checking, (date(2026, 10, 30), -403_773))
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    saver = threading.Thread(target=save)
    saver.start()
    importer = None
    try:
        assert saved.wait(timeout=10)
        importer = threading.Thread(target=late_import)
        importer.start()
        assert _wait_until_blocked_on_a_lock()
    finally:
        release.set()
        saver.join(timeout=30)
        if importer is not None:
            importer.join(timeout=30)
    assert errors == []
    assert calculated(checking) == expected(checking)
    assert checking.transactions.filter(transaction_date=date(2026, 10, 30)).exists()


def test_a_change_to_an_archived_account_keeps_its_calculated_history():
    from finance.calculated_balances import refresh_calculated_balances
    from finance.lifecycle_services import archive_account
    from django.db import transaction

    owner = make_person("archived-history")
    checking = checking_with_history(owner)
    save_calculated_balances(owner, checking.pk)
    before = calculated(checking)
    archive_account(owner, checking.pk)

    with transaction.atomic():
        assert refresh_calculated_balances(owner, checking.pk) == (0, 0, 0)
    assert calculated(checking) == before


@pytest.mark.django_db(transaction=True)
def test_a_sync_holds_no_account_lock_and_refreshes_each_account_after_it_commits():
    """Review of #733: the sync must not hold account locks while transfer matching
    locks counterpart accounts in pk order, or a sync can deadlock with undo, archive
    or another member's transfer refresh. The refresh runs after the sync commits,
    one account per transaction, and still corrects the late rows."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    if connection.vendor != "postgresql":
        pytest.skip("SELECT ... FOR UPDATE is only emitted on PostgreSQL")
    owner = make_person("lock-order")
    checking = make_account(owner)
    sf_connection = harness.link_simplefin(owner, checking, date(2026, 8, 1))
    first = [(1, (date(2026, 8, 5), -10_000))]
    _sync(owner, sf_connection, _sync_payload(first, balance=100_000, balance_day=date(2026, 11, 3)))
    save_calculated_balances(owner, checking.pk)
    late = [*first, (2, (date(2026, 11, 2), -5_000))]
    with CaptureQueriesContext(connection) as queries:
        _sync(owner, sf_connection, _sync_payload(late, balance=100_000, balance_day=date(2026, 11, 4)))
    sql = [query["sql"] for query in queries]
    sync_commit = next(n for n, text in enumerate(sql)
                       if text == "COMMIT" and any(t.startswith('INSERT INTO "finance_transaction"') for t in sql[:n]))
    account_locks = [n for n, text in enumerate(sql)
                     if text.startswith('SELECT "finance_account"') and 'FOR UPDATE OF "finance_account"' in text]
    assert account_locks and min(account_locks) > sync_commit
    assert calculated(checking) == expected(checking) == [(date(2026, 8, 31), 105_000), (date(2026, 9, 30), 105_000),
                                                          (date(2026, 10, 31), 105_000)]
