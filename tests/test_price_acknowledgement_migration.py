"""Synthetic upgrade through 0071: series dismissals become their owner's own (#523)."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("finance", "0070_alert_source_accounts")]
AFTER = [("finance", "0071_recurring_price_acknowledgements")]


def _series(apps, owner, name, acknowledged):
    return apps.get_model("finance", "RecurringSeries").objects.create(
        person=owner, merchant_key=name.casefold(), display_name=name, cadence="monthly",
        typical_amount_minor=-6000, status="confirmed", confidence="high", reasons=["synthetic"],
        fingerprint=name.encode().hex().ljust(64, "a")[:64], acknowledged_amount_minor=acknowledged,
    )


@pytest.mark.django_db(transaction=True)
def test_upgrade_keeps_each_dismissal_as_the_series_owners_and_rolls_back():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps
        user = apps.get_model("auth", "User").objects.create(username="synthetic-ack-owner")
        owner = apps.get_model("finance", "Person").objects.create(user=user, display_name="Synthetic Owner")
        dismissed = _series(apps, owner, "Synthetic Water", -9000)
        _series(apps, owner, "Synthetic Power", None)

        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        after = executor.loader.project_state(AFTER).apps
        rows = after.get_model("finance", "RecurringPriceAcknowledgement").objects.values_list(
            "person_id", "series_id", "amount_minor"
        )
        assert list(rows) == [(owner.pk, dismissed.pk, -9000)]

        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        restored = executor.loader.project_state(BEFORE).apps.get_model("finance", "RecurringSeries")
        assert restored.objects.get(pk=dismissed.pk).acknowledged_amount_minor == -9000
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
