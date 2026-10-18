"""Transactions reconcile against bank-reported balances (#535). Synthetic data only."""

import json
import logging
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import alerts_for, purge_old_read_alerts, run_daily_alert_pass, settings_for
from finance.models import Account, AccountLink, Alert, BalanceSnapshot, ImportBatch, ReconciliationRun, Transaction
from finance.reconciliation import (
    INSUFFICIENT,
    MISMATCH,
    OK,
    OK_LATE,
    eligible_accounts,
    evaluate_reconciliation_alerts,
    reconcile,
)
from finance.simplefin_services import sync_connection
from tests.test_alerts import make_account, make_household, make_person, signed_in
from tests.test_csv_import_page import link_account
from tests.test_simplefin import _linked_owner_and_checking, account_payload, epoch, posted_txn

pytestmark = pytest.mark.django_db

SEPT_1 = date(2026, 9, 1)
SEPT_30 = date(2026, 9, 30)
OCT_31 = date(2026, 10, 31)


def add_batch(account, start, end, *, source=ImportBatch.Source.HUNTINGTON, status=ImportBatch.Status.ACTIVE):
    return ImportBatch.objects.create(
        account=account,
        imported_by=account.owner,
        source=source,
        source_file_sha256=f"{account.pk:x}{start:%Y%m%d}{end:%Y%m%d}{source}".encode().hex().ljust(64, "0")[:64],
        date_range_start=start,
        date_range_end=end,
        status=status,
        archived_at=timezone.now() if status == ImportBatch.Status.ARCHIVED else None,
    )


def add_rows(batch, *rows):
    for number, (day, amount) in enumerate(rows, start=2):
        Transaction.objects.create(
            account=batch.account,
            import_batch=batch,
            transaction_date=day,
            amount_minor=amount,
            description="SYNTHETIC MERCHANT",
            kind=Transaction.Kind.CASH_FLOW,
            source_row_number=number,
            fingerprint=f"{batch.pk:x}{number:x}".ljust(64, "e"),
            original_fields={"Synthetic Amount": str(amount)},
            status=batch.status,
            archived_at=batch.archived_at,
        )


def snapshot(account, day, amount, *, source=BalanceSnapshot.Source.SIMPLEFIN):
    return BalanceSnapshot.objects.create(account=account, snapshot_date=day, amount_minor=amount, source=source)


def states(account):
    return [window.state for window in reconcile(eligible_accounts().filter(pk=account.pk))[account.pk]]


def windows(account):
    return reconcile(eligible_accounts().filter(pk=account.pk))[account.pk]


def clean_month(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, **account_options):
    account = make_account(owner, name=name, account_type=account_type, **account_options)
    batch = add_batch(account, SEPT_1, SEPT_30)
    add_rows(batch, (date(2026, 9, 5), -12_500), (date(2026, 9, 15), 300_000), (SEPT_30, -2_000))
    snapshot(account, SEPT_1, 50_000)
    snapshot(account, SEPT_30, 50_000 - 12_500 + 300_000 - 2_000)
    return account


def run_command(*args):
    out = StringIO()
    err = StringIO()
    try:
        call_command("reconcile_balances", *args, stdout=out, stderr=err)
    except CommandError as exc:
        return out.getvalue(), exc
    return out.getvalue(), None


def test_clean_month_is_ok():
    account = clean_month(make_person("owner"))

    assert states(account) == [OK]


def test_rows_dated_on_the_opening_snapshot_belong_to_the_earlier_window():
    owner = make_person("owner")
    account = clean_month(owner)
    # A row on d1 is already inside the opening balance; one on d2 is inside the closing one.
    add_rows(add_batch(account, date(2026, 8, 1), SEPT_1), (SEPT_1, -9_999))

    assert states(account) == [OK]


def test_duplicated_csv_import_after_cutover_is_a_mismatch_498():
    """#498's shape: a CSV file re-adds rows SimpleFIN already imported after the cut-over."""
    owner = make_person("owner")
    checking = make_account(owner)
    link_account(owner, checking, cutover=SEPT_1)
    synced = add_batch(checking, SEPT_1, OCT_31, source=ImportBatch.Source.SIMPLEFIN)
    add_rows(synced, (date(2026, 9, 3), -4_000), (date(2026, 9, 20), -6_000))
    duplicate_csv = add_batch(checking, SEPT_1, SEPT_30)
    add_rows(duplicate_csv, (date(2026, 9, 3), -4_000), (date(2026, 9, 20), -6_000))
    snapshot(checking, SEPT_1, 100_000)
    snapshot(checking, SEPT_30, 90_000)
    snapshot(checking, OCT_31, 90_000)

    result = windows(checking)

    assert [window.state for window in result] == [MISMATCH, OK]
    # The bank fell by 100.00; the app holds 200.00 of rows: the duplicated amount.
    assert result[0].difference_minor == 10_000

    duplicate_csv.status = ImportBatch.Status.ARCHIVED
    duplicate_csv.archived_at = timezone.now()
    duplicate_csv.save()
    duplicate_csv.transactions.update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())

    assert states(checking) == [OK, OK]


def test_missing_import_is_insufficient_not_mismatch():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, SEPT_1, date(2026, 9, 14)), (date(2026, 9, 5), -1_000))
    add_rows(add_batch(account, date(2026, 9, 20), OCT_31), (date(2026, 10, 5), -2_000))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, SEPT_30, 5_000)
    snapshot(account, OCT_31, 3_000)

    assert states(account) == [INSUFFICIENT, OK]


def test_undone_import_leaves_its_days_uncovered():
    owner = make_person("owner")
    account = clean_month(owner)
    batch = account.import_batches.get()
    batch.status = ImportBatch.Status.ARCHIVED
    batch.archived_at = timezone.now()
    batch.save()

    assert states(account) == [INSUFFICIENT]


def test_manual_entry_batch_is_not_import_coverage():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, date(2026, 9, 5), date(2026, 9, 5), source=ImportBatch.Source.MANUAL),
             (date(2026, 9, 5), -1_000))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, SEPT_30, 9_000)

    assert states(account) == [INSUFFICIENT]


def test_adjacent_batches_cover_a_window_together():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, SEPT_1, date(2026, 9, 15)), (date(2026, 9, 10), -1_000))
    add_rows(add_batch(account, date(2026, 9, 16), SEPT_30), (date(2026, 9, 20), -500))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, SEPT_30, 8_500)

    assert states(account) == [OK]


def test_credit_card_simplefin_balance_owed_is_negative():
    owner = make_person("owner")
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    batch = add_batch(card, SEPT_1, SEPT_30, source=ImportBatch.Source.CAPITAL_ONE)
    add_rows(batch, (date(2026, 9, 4), -7_500), (date(2026, 9, 25), 20_000))
    snapshot(card, SEPT_1, -30_000)
    snapshot(card, SEPT_30, -17_500)

    assert states(card) == [OK]


def test_credit_card_manual_balance_is_typed_as_positive_owed():
    owner = make_person("owner")
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    batch = add_batch(card, SEPT_1, OCT_31, source=ImportBatch.Source.APPLE_CARD)
    add_rows(batch, (date(2026, 9, 4), -7_500), (date(2026, 9, 25), 20_000), (date(2026, 10, 3), -1_000))
    snapshot(card, SEPT_1, 30_000, source=BalanceSnapshot.Source.MANUAL)
    snapshot(card, SEPT_30, 17_500, source=BalanceSnapshot.Source.MANUAL)
    # A SimpleFIN reading after manual statements uses the protocol's negative owed sign.
    snapshot(card, OCT_31, -18_500)

    assert states(card) == [OK, OK]


def test_credit_card_sign_error_is_a_mismatch():
    owner = make_person("owner")
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    # A purchase stored as money in, the sign error an importer could make.
    add_rows(add_batch(card, SEPT_1, SEPT_30, source=ImportBatch.Source.APPLE_CARD), (date(2026, 9, 4), 7_500))
    snapshot(card, SEPT_1, 30_000, source=BalanceSnapshot.Source.MANUAL)
    snapshot(card, SEPT_30, 37_500, source=BalanceSnapshot.Source.MANUAL)

    assert states(card) == [MISMATCH]


def test_simplefin_snapshot_wins_over_manual_on_the_same_day():
    owner = make_person("owner")
    account = clean_month(owner)
    snapshot(account, SEPT_30, 1, source=BalanceSnapshot.Source.MANUAL)

    assert states(account) == [OK]


def test_posting_skew_resolved_in_the_next_window_is_ok_late():
    owner = make_person("owner")
    account = make_account(owner)
    link_account(owner, account, cutover=SEPT_1)
    batch = add_batch(account, SEPT_1, OCT_31, source=ImportBatch.Source.SIMPLEFIN)
    # The bank's Sept 2 balance already held this purchase; it posted dated Sept 3.
    add_rows(batch, (date(2026, 9, 3), -2_500))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, date(2026, 9, 2), 7_500)
    snapshot(account, date(2026, 9, 3), 7_500)
    snapshot(account, date(2026, 9, 4), 7_500)

    assert states(account) == [OK_LATE, OK_LATE, OK]


def test_skew_not_undone_by_the_next_window_is_a_mismatch():
    owner = make_person("owner")
    account = make_account(owner)
    batch = add_batch(account, SEPT_1, OCT_31)
    add_rows(batch, (date(2026, 9, 4), -2_500))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, date(2026, 9, 2), 7_500)
    snapshot(account, date(2026, 9, 3), 7_500)
    snapshot(account, date(2026, 9, 4), 7_500)

    assert states(account) == [MISMATCH, OK, MISMATCH]


def test_skew_does_not_resolve_across_an_uncovered_window():
    owner = make_person("owner")
    account = make_account(owner)
    add_batch(account, SEPT_1, date(2026, 9, 2))
    add_rows(add_batch(account, date(2026, 9, 4), date(2026, 9, 5)), (date(2026, 9, 5), -2_500))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, date(2026, 9, 2), 7_500)
    snapshot(account, date(2026, 9, 3), 7_500)
    snapshot(account, date(2026, 9, 5), 7_500)

    assert states(account) == [MISMATCH, INSUFFICIENT, MISMATCH]


def test_since_keeps_later_windows_and_their_skew_partner():
    owner = make_person("owner")
    account = make_account(owner)
    add_rows(add_batch(account, SEPT_1, OCT_31), (date(2026, 9, 3), -2_500))
    snapshot(account, SEPT_1, 10_000)
    snapshot(account, date(2026, 9, 2), 7_500)
    snapshot(account, date(2026, 9, 3), 7_500)

    result = reconcile(eligible_accounts().filter(pk=account.pk), since=date(2026, 9, 3))[account.pk]

    assert [(window.end, window.state) for window in result] == [(date(2026, 9, 3), OK_LATE)]


def test_eligible_accounts_are_active_cash_accounts_with_a_link_or_two_snapshots():
    owner = make_person("owner")
    linked = make_account(owner, name="Synthetic Linked")
    link_account(owner, linked)
    two = make_account(owner, name="Synthetic Two Snapshots", account_type=Account.Type.SAVINGS)
    snapshot(two, SEPT_1, 1)
    snapshot(two, SEPT_30, 1, source=BalanceSnapshot.Source.MANUAL)
    one_day = make_account(owner, name="Synthetic Same Day")
    snapshot(one_day, SEPT_1, 1)
    snapshot(one_day, SEPT_1, 1, source=BalanceSnapshot.Source.MANUAL)
    investment = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    snapshot(investment, SEPT_1, 1)
    snapshot(investment, SEPT_30, 2)
    house = make_account(owner, name="Synthetic House", account_type=Account.Type.REAL_ESTATE)
    snapshot(house, SEPT_1, 1, source=BalanceSnapshot.Source.MANUAL)
    snapshot(house, SEPT_30, 2, source=BalanceSnapshot.Source.MANUAL)
    archived = make_account(owner, name="Synthetic Closed")
    snapshot(archived, SEPT_1, 1)
    snapshot(archived, SEPT_30, 2)
    Account.objects.filter(pk=archived.pk).update(status=Account.Status.ARCHIVED, archived_at=timezone.now())

    assert list(eligible_accounts().values_list("name", flat=True)) == ["Synthetic Linked", "Synthetic Two Snapshots"]
    assert reconcile(eligible_accounts())[linked.pk] == []


def test_simplefin_syncs_reconcile_through_the_real_sync_path(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    first = account_payload()
    first["accounts"][0].update({"balance": "100.00", "balance-date": epoch(2026, 3, 10)})
    _owner, checking, connection = _linked_owner_and_checking(monkeypatch, owner=owner, payload=first,
                                                              cutover=date(2026, 3, 1))
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    second = account_payload(transactions=[
        posted_txn(txn_id="sf-1", day=12, amount="-30.00"),
        posted_txn(txn_id="sf-2", day=18, amount="5.25"),
        posted_txn(txn_id="sf-pending", day=19, amount="-99.00", pending=True),
    ])
    second["accounts"][0].update({"balance": "75.25", "balance-date": epoch(2026, 3, 20)})
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: second)
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert AccountLink.objects.get(account=checking).mode == AccountLink.Mode.TRANSACTIONS
    assert states(checking) == [OK]


def test_command_text_and_json_carry_no_amounts_or_names(caplog):
    caplog.set_level(logging.DEBUG)
    owner = make_person("owner")
    clean_month(owner, name="Synthetic Secret Name")
    bad = make_account(owner, name="Synthetic Other Name")
    add_rows(add_batch(bad, SEPT_1, SEPT_30), (date(2026, 9, 9), -12_345))
    snapshot(bad, SEPT_1, 50_000)
    snapshot(bad, SEPT_30, 50_000)

    text, error = run_command()
    payload_text, json_error = run_command("--json")

    assert isinstance(error, CommandError) and error.returncode == 1
    assert isinstance(json_error, CommandError) and json_error.returncode == 1
    payload = json.loads(payload_text)
    assert payload["counts"] == {"ok": 1, "ok-late": 0, "mismatch": 1, "insufficient": 0}
    assert payload["accounts"] == 2
    assert {window["state"] for window in payload["windows"]} == {"ok", "mismatch"}
    assert set(payload["windows"][0]) == {"account", "start", "end", "state"}
    assert f"Account {bad.pk}: 1 window(s): 0 ok, 0 ok-late, 1 mismatch, 0 insufficient" in text
    for output in (text, payload_text, str(error), caplog.text):
        for secret in ("Synthetic", "123.45", "12345", "12,345", "500.00", "50000"):
            assert secret not in output


def test_command_succeeds_when_every_window_reconciles():
    account = clean_month(make_person("owner"))

    text, error = run_command("--account", str(account.pk))

    assert error is None
    assert "1 account(s) reconciled: 1 ok, 0 ok-late, 0 mismatch, 0 insufficient." in text


def test_command_rejects_an_ineligible_account_and_a_bad_date():
    owner = make_person("owner")
    house = make_account(owner, name="Synthetic House", account_type=Account.Type.REAL_ESTATE)

    _text, wrong_account = run_command("--account", str(house.pk))
    _text, wrong_date = run_command("--since", "Sept 1")

    assert "not reconciled" in str(wrong_account)
    assert "YYYY-MM-DD" in str(wrong_date)


def test_command_does_not_write():
    clean_month(make_person("owner"))
    before = (Alert.objects.count(), ReconciliationRun.objects.count())

    run_command("--json")

    assert (Alert.objects.count(), ReconciliationRun.objects.count()) == before


def _mismatched(owner, **options):
    account = make_account(owner, **options)
    add_rows(add_batch(account, SEPT_1, SEPT_30), (date(2026, 9, 9), -1_000))
    snapshot(account, SEPT_1, 5_000)
    snapshot(account, SEPT_30, 5_000)
    return account


def _household_with_private_mismatch():
    operator = make_person("operator")
    partner = make_person("partner")
    household = make_household(operator, partner)
    shared = _mismatched(operator, name="Synthetic Shared Checking", scope=Account.Scope.HOUSEHOLD,
                         household=household)
    private = _mismatched(partner, name="Synthetic Partner Private")
    return operator, partner, household, shared, private


@override_settings(OPERATOR_USERNAMES="operator")
def test_alert_reaches_operators_only_for_accounts_they_can_see():
    operator, partner, _household, shared, _private = _household_with_private_mismatch()

    created = evaluate_reconciliation_alerts()

    assert [alert.recipient for alert in created] == [operator]
    alert = Alert.objects.get(kind=Alert.Kind.RECONCILIATION)
    assert alert.title == "Bank balances disagree with transactions in 1 period(s) on 1 account(s)"
    assert list(alert.source_accounts.all()) == [shared]
    assert alert.link == reverse("settings-data")
    assert "Synthetic" not in alert.title and "10.00" not in alert.title
    assert not Alert.objects.filter(recipient=partner).exists()
    assert ReconciliationRun.objects.count() == 1


@override_settings(OPERATOR_USERNAMES="operator")
def test_alert_repeats_only_for_a_new_mismatch_and_clears_when_fixed():
    operator = make_person("operator")
    account = _mismatched(operator)

    assert len(evaluate_reconciliation_alerts()) == 1
    assert evaluate_reconciliation_alerts() == []
    later = _mismatched(operator, name="Synthetic Second Checking")
    assert len(evaluate_reconciliation_alerts()) == 1
    assert Alert.objects.filter(recipient=operator, kind=Alert.Kind.RECONCILIATION).count() == 2

    # One fixed: no alert for a shrinking set.
    BalanceSnapshot.objects.filter(account=account, snapshot_date=SEPT_30).update(amount_minor=4_000)
    assert evaluate_reconciliation_alerts() == []
    BalanceSnapshot.objects.filter(account=later, snapshot_date=SEPT_30).update(amount_minor=4_000)
    assert evaluate_reconciliation_alerts() == []
    assert not Alert.objects.filter(kind=Alert.Kind.RECONCILIATION).exists()
    assert settings_for(operator).reconciliation_alerted_windows == []
    assert ReconciliationRun.objects.count() == 1

    # A window that mismatches again after it was resolved alerts again.
    BalanceSnapshot.objects.filter(account=account, snapshot_date=SEPT_30).update(amount_minor=5_000)
    assert len(evaluate_reconciliation_alerts()) == 1


@override_settings(OPERATOR_USERNAMES="operator")
def test_unresolved_mismatch_does_not_alert_again_after_retention_purges_its_alert():
    operator = make_person("operator")
    account = _mismatched(operator)
    assert len(evaluate_reconciliation_alerts()) == 1
    Alert.objects.filter(kind=Alert.Kind.RECONCILIATION).update(
        created_at=timezone.now() - timedelta(days=200), read_at=timezone.now() - timedelta(days=199)
    )

    purge_old_read_alerts()

    assert not Alert.objects.exists()
    assert evaluate_reconciliation_alerts() == []
    assert settings_for(operator).reconciliation_alerted_windows == [
        f"{account.pk}:{SEPT_1.isoformat()}:{SEPT_30.isoformat()}"
    ]


@override_settings(OPERATOR_USERNAMES="operator")
def test_alert_hides_when_its_account_is_unshared():
    operator, partner, household, shared, _private = _household_with_private_mismatch()
    shared.owner = partner
    shared.save()
    evaluate_reconciliation_alerts()
    assert alerts_for(operator).filter(kind=Alert.Kind.RECONCILIATION).count() == 1

    Account.objects.filter(pk=shared.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")

    assert not alerts_for(operator).filter(kind=Alert.Kind.RECONCILIATION).exists()


@override_settings(OPERATOR_USERNAMES="operator")
def test_daily_alert_pass_runs_reconciliation_after_sync(tmp_path):
    operator = make_person("operator")
    _mismatched(operator)

    with override_settings(BACKUP_STATUS_PATH=str(tmp_path / "missing")):
        run_daily_alert_pass()

    assert Alert.objects.filter(recipient=operator, kind=Alert.Kind.RECONCILIATION).exists()
    assert ReconciliationRun.objects.exists()


@override_settings(OPERATOR_USERNAMES="operator")
def test_settings_data_shows_operator_counts_and_amounts_for_visible_accounts_only():
    operator, partner, _household, _shared, _private = _household_with_private_mismatch()
    clean_month(operator, name="Synthetic Operator Savings", account_type=Account.Type.SAVINGS)
    ReconciliationRun.objects.create(ran_at=timezone.now() - timedelta(hours=2))

    page = signed_in(operator).get(reverse("settings-data"))
    content = page.content.decode()

    assert page.status_code == 200
    assert "Balance reconciliation" in content
    assert page.context["reconciliation"]["counts"] == {"ok": 1, "ok-late": 0, "mismatch": 1, "insufficient": 0}
    assert "Synthetic Shared Checking" in content
    assert "+$10.00" in content
    assert "Synthetic Partner Private" not in content
    assert "Never" not in content.split("Last scheduled check:")[1].split("</p>")[0]

    other = signed_in(partner).get(reverse("settings-data"))
    assert other.context["reconciliation"] is None
    assert "Balance reconciliation" not in other.content.decode()
