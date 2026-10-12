"""Synthetic upgrade through 0066: existing alert settings get the 10% price-drift default."""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("finance", "0065_privacy_policy_dismissal")]
AFTER = [("finance", "0066_recurring_price_percent")]


@pytest.mark.django_db(transaction=True)
def test_existing_alert_settings_get_the_default_percent():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps
        user = apps.get_model("auth", "User").objects.create(username="synthetic-migration-member")
        person = apps.get_model("finance", "Person").objects.create(user=user, display_name="Synthetic Member")
        apps.get_model("finance", "AlertSettings").objects.create(person=person, recurring_price_enabled=False)

        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        after_apps = executor.loader.project_state(AFTER).apps
        prefs = after_apps.get_model("finance", "AlertSettings").objects.get(person_id=person.pk)
        assert prefs.recurring_price_percent == 10
        assert prefs.recurring_price_enabled is False
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
