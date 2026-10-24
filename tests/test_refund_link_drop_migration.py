"""Rolling 0077 back never shows one member's history to another (#570).

At 0076 `TransactionCorrectionHistory` has no `only_for`, so every viewer of the
refund would see a "linked" -> "unlinked" row written for one member alone. The
reverse deletes those rows instead.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from finance.category_services import link_refund, refresh_transfer_pairs
from finance.models import Account, RefundLinkDrop, TransactionCorrectionHistory
from tests.test_categorization import make_account, make_household, make_person, make_transaction

BEFORE = [("finance", "0076_ai_conversation_backfill_quoted_rows")]


@pytest.fixture
def executor():
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        yield executor
    finally:
        MigrationExecutor(connection).migrate(head)
        if connection.vendor == "postgresql":
            # The rollback recreated the table; keep autovacuum off as
            # tests/conftest.py does for every table (#555).
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE {RefundLinkDrop._meta.db_table} SET (autovacuum_enabled = false)")


@pytest.mark.django_db(transaction=True)
def test_rolling_back_deletes_member_only_history(executor):
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    joint = make_account(alice, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    savings = make_account(alice, name="Synthetic Private Savings", account_type=Account.Type.SAVINGS)
    purchase = make_transaction(alice, joint, amount_minor=-5000, description="Synthetic transfer to savings")
    refund = make_transaction(alice, joint, amount_minor=2000, description="Synthetic return credit")
    link_refund(bob, refund.pk, purchase.pk)
    make_transaction(alice, savings, amount_minor=5000, description="Synthetic transfer from checking")
    refresh_transfer_pairs(alice)
    assert RefundLinkDrop.objects.filter(person=alice).exists()
    assert TransactionCorrectionHistory.objects.filter(transaction=refund, only_for=alice).exists()
    shared = set(TransactionCorrectionHistory.objects.filter(only_for__isnull=True).values_list("pk", flat=True))

    executor.migrate(BEFORE)

    history = executor.loader.project_state(BEFORE).apps.get_model("finance", "TransactionCorrectionHistory")
    assert set(history.objects.values_list("pk", flat=True)) == shared
    rows = history.objects.filter(transaction_id=refund.pk, field_name="refund_link")
    assert list(rows.values_list("previous_description", "new_description")) == [("unlinked", "linked")]
