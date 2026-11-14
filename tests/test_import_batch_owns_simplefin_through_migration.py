"""Synthetic upgrade through 0092: a batch that existed before it owns no SimpleFIN days (#743).

Migration 0092 only adds the nullable `ImportBatch.owns_simplefin_through`.
A file batch imported before it, even one whose range and rows reach past a
cut-over (which the superseded ledger-derived rule would have counted), must
come through NULL, so neither ownership reader gives it any day and a later
sync still supplies those days. A file imported after the upgrade while the
link is failing stores its boundary. Rolling back to 0091 keeps every batch
and transaction. All data is synthetic.
"""

from datetime import date
from hashlib import sha256

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Sum

from finance.models import Account, ImportBatch, Person
from finance.simplefin_file_days import file_owned_days, file_owned_through
from tests.properties import harness as ledger
from tests.properties.strategies import Row

BEFORE = [("finance", "0091_import_batch_requested_from")]
AFTER = [("finance", "0092_import_batch_owns_simplefin_through")]
CUTOVER = date(2026, 10, 1)
# The link made after the upgrade: after the legacy file row, which now counts
# as an ordinary file row for the cut-over check because it owns nothing.
NEW_CUTOVER = date(2026, 10, 7)


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _seed_legacy_batch(apps):
    """A Huntington batch confirmed 10-06 through 10-09 with one active row on 10-06."""
    user = apps.get_model("auth", "User").objects.create(username="synthetic-0092-owner")
    owner = apps.get_model("finance", "Person").objects.create(user=user, display_name="Synthetic Owner")
    account = apps.get_model("finance", "Account").objects.create(
        name="Synthetic Checking", account_type="checking", owner=owner, scope="private", share_mode="",
    )
    batch = apps.get_model("finance", "ImportBatch").objects.create(
        account=account, imported_by=owner, source="huntington", source_file_sha256=_digest("legacy file"),
        date_range_start=date(2026, 10, 6), date_range_end=date(2026, 10, 9),
    )
    apps.get_model("finance", "Transaction").objects.create(
        account=account, import_batch=batch, transaction_date=date(2026, 10, 6), amount_minor=-300,
        currency="USD", description="SYN LEGACY GAP", kind="cash_flow", source_row_number=2,
        fingerprint=_digest("legacy row"), original_fields={"Date": "2026-10-06"},
    )
    return owner.pk, account.pk, batch.pk


def _snapshot(apps, account_id):
    batches = apps.get_model("finance", "ImportBatch").objects.filter(account_id=account_id)
    rows = apps.get_model("finance", "Transaction").objects.filter(account_id=account_id)
    return (
        sorted(batches.values_list("pk", "source", "status", "date_range_start", "date_range_end")),
        sorted(rows.values_list("pk", "import_batch_id", "transaction_date", "amount_minor", "status")),
        rows.aggregate(total=Sum("amount_minor"))["total"],
    )


@pytest.mark.django_db(transaction=True)
def test_a_batch_from_before_0092_owns_no_days_and_rollback_keeps_every_row():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(BEFORE)
        owner_id, account_id, legacy_id = _seed_legacy_batch(executor.loader.project_state(BEFORE).apps)

        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        after = executor.loader.project_state(AFTER).apps
        assert after.get_model("finance", "ImportBatch").objects.get(pk=legacy_id).owns_simplefin_through is None
        # Both readers ignore it, at the old cut-over and the one linked below.
        for cutover in (CUTOVER, NEW_CUTOVER):
            assert file_owned_through(account_id, cutover) is None
            assert file_owned_days(account_id, cutover).through is None

        # A file imported after the upgrade while the link is failing stores its date.
        owner = Person.objects.get(pk=owner_id)
        account = Account.objects.get(pk=account_id)
        sf_connection = ledger.link_simplefin(owner, account, NEW_CUTOVER)
        ledger.sync(owner, sf_connection, [("a", Row(date(2026, 10, 8), -1100, "SYN SF COFFEE", "", ""))])
        from tests.test_simplefin_file_owned_days import _fail, _import_file

        _fail(owner, sf_connection)
        gap = _import_file(owner, account, (
            b"Date,Description,Amount\n"
            b"2026-10-10,SYN CSV GAP ONE,-3.00\n"
            b"2026-10-12,SYN CSV GAP TWO,-4.00\n"
        ))
        assert gap.new_count == 2
        assert gap.batch.owns_simplefin_through == date(2026, 10, 12)
        assert ImportBatch.objects.get(pk=legacy_id).owns_simplefin_through is None
        assert file_owned_through(account_id, NEW_CUTOVER) == date(2026, 10, 12)
        assert file_owned_days(account_id, NEW_CUTOVER).ranges == ((date(2026, 10, 10), date(2026, 10, 12)),)
        upgraded = _snapshot(after, account_id)
        assert len(upgraded[0]) == 3 and upgraded[2] == -300 - 1100 - 300 - 400

        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        restored = executor.loader.project_state(BEFORE).apps
        assert _snapshot(restored, account_id) == upgraded
        field_names = {field.name for field in restored.get_model("finance", "ImportBatch")._meta.get_fields()}
        assert "owns_simplefin_through" not in field_names
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
