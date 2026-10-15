"""Synthetic populated upgrade through the historical 0060 transfer schema (one private-account case)."""
from datetime import date
from importlib import import_module

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone

BEFORE = [("finance", "0059_audit_operations")]
AFTER = [("finance", "0060_reevaluate_auto_marked_pairs")]


def _seed(apps):
    models = {name: apps.get_model("finance", name) for name in (
        "Person", "Household", "Membership", "Account", "ImportBatch", "Category",
        "Transaction", "TransferPair", "RefundLink", "TransactionCorrectionHistory",
    )}
    user = apps.get_model("auth", "User").objects.create(username="synthetic-migration-owner")
    owner = models["Person"].objects.create(user=user, display_name="Synthetic Owner")
    household = models["Household"].objects.create(name="Synthetic Household", transfer_match_window_days=9)
    models["Membership"].objects.create(person=owner, household=household)
    groceries = models["Category"].objects.create(household=household, name="Synthetic Groceries")
    transfer = models["Category"].objects.create(household=household, name="Transfer", code="transfer")
    accounts = {}
    batches = {}
    for kind in ("checking", "savings", "credit_card"):
        accounts[kind] = models["Account"].objects.create(
            owner=owner, name=f"Synthetic {kind}", account_type=kind, scope="private",
        )
        batches[kind] = models["ImportBatch"].objects.create(
            account=accounts[kind], imported_by=owner, source="huntington", source_file_sha256="b" * 64,
            date_range_start=date(2026, 1, 1), date_range_end=date(2026, 1, 31),
        )

    def txn(kind, amount, description="Synthetic plain", category=transfer, **extra):
        row = models["Transaction"].objects.count() + 1
        return models["Transaction"].objects.create(
            account=accounts[kind], import_batch=batches[kind], transaction_date=date(2026, 1, 2),
            amount_minor=amount, currency="USD", description=description,
            source_row_number=row, fingerprint=f"{row:064x}", original_fields={"synthetic": True},
            category=category, category_source="manual", **extra,
        )

    def pair(left, right, status="auto_marked", snapshot=groceries.pk):
        return models["TransferPair"].objects.create(
            leg_a=left, leg_b=right, status=status, confidence="high", kind="transfer",
            reasons=["Synthetic legacy reason"], leg_a_category_id_at_mark=snapshot,
            leg_b_category_id_at_mark=None,
        )

    weak_out, weak_in = txn("checking", -2500), txn("savings", 2500)
    weak = pair(weak_out, weak_in)
    good = pair(txn("checking", -8800), txn("credit_card", 8800, "Synthetic PAYMENT thank you"))
    confirmed = pair(txn("checking", -700), txn("savings", 700), "confirmed")
    wrong = pair(txn("checking", 3300, "Synthetic ACH credit"), txn("credit_card", -3300))
    inactive = pair(txn("checking", -4100, status="archived", archived_at=timezone.now()), txn("savings", 4100))
    settled = [good, confirmed, inactive]
    for status in ("suggested", "dismissed", "undone"):
        settled.append(pair(txn("checking", -9900), txn("savings", 9900), status))
    refunds = {
        "restore": txn("checking", 100),
        "same": txn("checking", 200, category=groceries),
        "archived": txn("checking", 300, status="archived", archived_at=timezone.now()),
        "null": txn("savings", 400),
    }
    for name, refund in refunds.items():
        models["RefundLink"].objects.create(refund=refund, original=weak_in if name == "null" else weak_out)
    old_history = models["TransactionCorrectionHistory"].objects.create(
        transaction=weak_out, actor=owner, field_name="exclusion",
        previous_description="included", new_description="excluded",
    )
    return models, weak, wrong, settled, refunds, groceries, old_history


def _snapshot(models):
    return {name: list(model.objects.order_by("pk").values()) for name, model in models.items()}


@pytest.mark.django_db(transaction=True)
@override_settings(TRANSFER_MATCH_WINDOW_DAYS=13)
def test_populated_upgrade_restores_only_weak_auto_marked_pairs():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps
        assert "savings_buffer_minor" not in {field.name for field in apps.get_model("finance", "Household")._meta.fields}
        models, weak, wrong, settled, refunds, groceries, old_history = _seed(apps)
        original = _snapshot(models)
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        after_apps = executor.loader.project_state(AFTER).apps
        assert "savings_buffer_minor" not in {field.name for field in after_apps.get_model("finance", "Household")._meta.fields}
        for pair in (weak, wrong):
            pair.refresh_from_db()
            assert (pair.status, pair.confidence, pair.kind) == ("suggested", "low", "transfer")
            assert "dates within 9 days" in pair.reasons
            assert models["Transaction"].objects.get(pk=pair.leg_a_id).category_id == groceries.pk
            assert models["Transaction"].objects.get(pk=pair.leg_b_id).category_id is None
        assert "no payment or transfer wording on either leg, so it needs confirmation" in weak.reasons
        assert "card pair runs the wrong way (bank credit with card debit), so it needs confirmation" in wrong.reasons
        assert "payment or transfer wording on a leg" in wrong.reasons
        for pair in settled:
            assert models["TransferPair"].objects.values().get(pk=pair.pk) == next(
                row for row in original["TransferPair"] if row["id"] == pair.pk
            )
        expected_categories = {
            "restore": groceries.pk, "same": groceries.pk, "null": None,
            "archived": next(row["category_id"] for row in original["Transaction"] if row["id"] == refunds["archived"].pk),
        }
        for name, refund in refunds.items():
            refund.refresh_from_db()
            assert refund.category_id == expected_categories[name]
            assert refund.category_source == ("inherited" if name in ("restore", "null") else "manual")
        # Preserve source provenance, exact amounts, currency and category-source snapshots on legs.
        updated = _snapshot(models)
        for name in models.keys() - {"Transaction", "TransferPair", "TransactionCorrectionHistory"}:
            assert updated[name] == original[name]
        changing_ids = {weak.leg_a_id, weak.leg_b_id, wrong.leg_a_id, wrong.leg_b_id, refunds["restore"].pk, refunds["null"].pk}
        for before, after in zip(original["Transaction"], updated["Transaction"], strict=True):
            ignored = {"category_id", "category_source", "updated_at"} if before["id"] in changing_ids else set()
            assert {k: v for k, v in before.items() if k not in ignored} == {k: v for k, v in after.items() if k not in ignored}
        history = models["TransactionCorrectionHistory"]
        assert history.objects.values().get(pk=old_history.pk) == original["TransactionCorrectionHistory"][0]
        created = list(history.objects.exclude(pk=old_history.pk).values_list(
            "transaction_id", "field_name", "previous_description", "new_description", "actor_id",
        ))
        expected = []
        for pair in (weak, wrong):
            expected.extend([
                (pair.leg_a_id, "category", "Transfer", groceries.name, None),
                (pair.leg_b_id, "category", "Transfer", "Uncategorized", None),
                (pair.leg_a_id, "exclusion", "excluded", "included", None),
                (pair.leg_b_id, "exclusion", "excluded", "included", None),
            ])
        expected.extend([
            (refunds["restore"].pk, "category", "Transfer", groceries.name, None),
            (refunds["null"].pk, "category", "Transfer", "Uncategorized", None),
        ])
        assert sorted(created) == sorted(expected)
        leg_ids = [weak.leg_a_id, weak.leg_b_id, wrong.leg_a_id, wrong.leg_b_id]
        assert models["Transaction"].objects.filter(pk__in=leg_ids, category_source="manual").count() == 4

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        latest_apps = executor.loader.project_state(latest).apps
        latest_models = {name: latest_apps.get_model("finance", name) for name in models}
        fully_migrated = _snapshot(latest_models)
        executor = MigrationExecutor(connection)
        assert executor.migration_plan(latest) == []
        executor.migrate(latest)
        assert _snapshot(latest_models) == fully_migrated
        # Re-evaluation is idempotent even if invoked again against the latest schema.
        with connection.schema_editor() as editor:
            import_module("finance.migrations.0060_reevaluate_auto_marked_pairs").reevaluate(latest_apps, editor)
        assert _snapshot(latest_models) == fully_migrated
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
