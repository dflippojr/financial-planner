"""Migration 0087 adds the note fields on rules and recurring series (#684).

Existing rules and series keep every row and start with no note; rolling back
drops only the new columns.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from finance.models import CategoryRule, RecurringSeries
from finance.recurring_services import confirm_recurring_series, refresh_recurring_series
from tests.test_auto_notes import _rule
from tests.test_recurring import add_monthly_charges, make_account, make_household, make_person

BEFORE = [("finance", "0086_ai_proposal_balance_cards")]
AFTER = [("finance", "0087_rule_and_series_notes")]


@pytest.fixture
def executor():
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        yield executor
    finally:
        MigrationExecutor(connection).migrate(head)


@pytest.mark.django_db(transaction=True)
def test_rules_and_series_keep_their_rows_and_start_without_a_note(executor):
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    rule = _rule(owner, household, note="Dropped on rollback")
    add_monthly_charges(owner, account)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    confirm_recurring_series(owner, series.pk)

    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    old_rule = old_apps.get_model("finance", "CategoryRule")
    old_series = old_apps.get_model("finance", "RecurringSeries")
    assert list(old_rule.objects.values_list("pk", flat=True)) == [rule.pk]
    assert list(old_series.objects.values_list("pk", "status")) == [(series.pk, "confirmed")]
    assert "note" not in {field.name for field in old_rule._meta.get_fields()}

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new_apps = executor.loader.project_state(AFTER).apps
    assert list(new_apps.get_model("finance", "CategoryRule").objects.values_list("pk", "note")) == [(rule.pk, "")]
    assert list(
        new_apps.get_model("finance", "RecurringSeries").objects.values_list("pk", "note", "note_updated_at")
    ) == [(series.pk, "", None)]
    assert CategoryRule._meta.get_field("note").max_length == 2_000
