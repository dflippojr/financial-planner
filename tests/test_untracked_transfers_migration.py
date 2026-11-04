"""Rolling 0079 back removes what the old schema cannot hold (#642).

At 0078 a rule must have a category and "untracked" is not a correction-history
field. The reverse deletes untracked-transfer rules with their applications and
the mark's history rows; every other rule, row and history entry stays.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from finance.models import Category, CategoryRule, RuleApplication, TransactionCorrectionHistory
from finance.rule_services import apply_rule, save_category_rule
from finance.untracked_transfers import mark_untracked_transfer
from tests.test_categorization import make_account, make_household, make_person, make_transaction

BEFORE = [("finance", "0078_balance_snapshot_calculated")]


@pytest.fixture
def executor():
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        yield executor
    finally:
        MigrationExecutor(connection).migrate(head)


@pytest.mark.django_db(transaction=True)
def test_rolling_back_deletes_untracked_rules_and_their_history(executor):
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    buy = make_transaction(owner, account, amount_minor=-50_000, description="SYNTHETIC BROKERAGE BUY")
    move = make_transaction(owner, account, amount_minor=-7_000, description="SYNTHETIC PAYMENT APP MOVE")
    groceries = Category.objects.get(household=household, name="Groceries")
    kept_rule = save_category_rule(
        owner, owner_kind="personal", description_contains="grocer", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries.pk, priority=0,
    )
    untracked_rule = save_category_rule(
        owner, owner_kind="personal", description_contains="brokerage", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=None, priority=0,
        action=CategoryRule.Action.UNTRACKED_TRANSFER,
    )
    apply_rule(owner, untracked_rule.pk)
    mark_untracked_transfer(owner, move.pk)
    assert TransactionCorrectionHistory.objects.filter(field_name="untracked").count() == 2
    assert RuleApplication.objects.filter(rule=untracked_rule).exists()
    other_history = set(
        TransactionCorrectionHistory.objects.exclude(field_name="untracked").values_list("pk", flat=True)
    )

    executor.migrate(BEFORE)

    apps = executor.loader.project_state(BEFORE).apps
    assert list(apps.get_model("finance", "CategoryRule").objects.values_list("pk", "category_id")) == [
        (kept_rule.pk, groceries.pk)
    ]
    assert not apps.get_model("finance", "RuleApplication").objects.filter(rule_id=untracked_rule.pk).exists()
    history = apps.get_model("finance", "TransactionCorrectionHistory")
    assert set(history.objects.values_list("pk", flat=True)) == other_history
    assert apps.get_model("finance", "Transaction").objects.filter(pk__in=(buy.pk, move.pk)).count() == 2
