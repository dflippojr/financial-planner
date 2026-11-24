"""Rolling back migration 0093 after an undo re-homed rows (#796).

0093 lets `TransactionCorrectionHistory.field_name` be `import_batch`, the
entry an undo writes for each row it moves to another batch. 0092's value-shape
constraint does not admit that name, so the rollback has to drop those entries
before the old constraint returns. The re-homed transactions stay where they
are, active on the batch that took them. All data is synthetic.
"""

from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from finance.csv_import.services import undo_import_batch
from finance.models import Transaction, TransactionCorrectionHistory
from tests.properties import harness as ledger
from tests.properties.strategies import HUNTINGTON, Row

BEFORE = [("finance", "0092_import_batch_owns_simplefin_through")]
COFFEE = Row(date(2026, 9, 28), -450, "SYN COFFEE", "", "SYN-00001")
GROCER = Row(date(2026, 9, 27), -1234, "SYN GROCER", "", "SYN-00002")


@pytest.mark.django_db(transaction=True)
def test_rollback_to_0092_after_a_rehome_drops_only_the_import_batch_history():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        owner = ledger.make_member("synthetic-0093-owner")
        account = ledger.make_account(owner, HUNTINGTON.account_type)
        first = ledger.import_rows(owner, account, HUNTINGTON, [GROCER, COFFEE])
        later = ledger.import_rows(owner, account, HUNTINGTON, [COFFEE])
        assert undo_import_batch(owner, account.pk, first.batch.pk).rehomed_count == 1
        coffee = Transaction.objects.get(account=account, status="active")
        assert coffee.import_batch_id == later.batch.pk
        other = TransactionCorrectionHistory.objects.create(
            transaction=coffee, field_name="note", previous_description="", new_description="synthetic note"
        )
        assert TransactionCorrectionHistory.objects.filter(field_name="import_batch").count() == 1
        rows_before = sorted(Transaction.objects.values_list("pk", "import_batch_id", "status", "amount_minor"))

        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)

        restored = executor.loader.project_state(BEFORE).apps
        history = restored.get_model("finance", "TransactionCorrectionHistory").objects
        assert list(history.values_list("pk", "field_name")) == [(other.pk, "note")]
        rows = restored.get_model("finance", "Transaction").objects
        assert sorted(rows.values_list("pk", "import_batch_id", "status", "amount_minor")) == rows_before
        fields = {field.name for field in restored.get_model("finance", "ImportBatch")._meta.get_fields()}
        assert "skipped_duplicates" not in fields
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
