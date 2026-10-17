"""Migrate a populated synthetic database from an old state to head (#532).

Empty-database migration checks return before any data path runs, so a
migration that breaks on real rows (#391) was first tried on production. This
seeds the old schema through its historical models (tests/migration_fixtures)
and migrates forward to every leaf, then checks that rows, money and NOT NULL
columns survive. postgres.yml runs it on PostgreSQL 18; locally use
bash scripts/test_postgres.sh -m migration -rP.
"""
import time

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from tests.migration_fixtures import builder

START_POINTS = {
    "0040": [("finance", "0040_saved_transaction_filter")],
    # #391's shape: 0060 runs on populated data before 0061 adds a Household column.
    "0059": [("finance", "0059_audit_operations")],
}
# 0070 deletes recurring alerts raised before sources were recorded (#520).
DELETED_ALERT_KINDS = ("recurring_price", "recurring_missed")
# 0060 appends the restorations it makes to the correction history.
APPEND_ONLY = {"finance_transactioncorrectionhistory"}


def _scalar(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def _rows(sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def _counts():
    tables = set(connection.introspection.table_names()) - {"django_migrations"}
    quote = connection.ops.quote_name
    return {table: _scalar(f"SELECT COUNT(*) FROM {quote(table)}") for table in sorted(tables)}


def _money_queries(apps):
    """One SQL query per money column, grouped by account where the table has one."""
    quote = connection.ops.quote_name
    queries = {}
    for model in apps.get_app_config("finance").get_models():
        table = quote(model._meta.db_table)
        columns = {field.name: field.column for field in model._meta.concrete_fields}
        group = columns.get("account")
        for name, column in columns.items():
            if not name.endswith("_minor"):
                continue
            if group:
                sql = (f"SELECT {quote(group)}, COUNT({quote(column)}), SUM({quote(column)}) FROM {table} "
                       f"GROUP BY {quote(group)} ORDER BY {quote(group)}")
            else:
                sql = f"SELECT COUNT({quote(column)}), SUM({quote(column)}) FROM {table}"
            queries[f"{model._meta.db_table}.{column}"] = sql
    return queries


def _money(queries):
    return {key: _rows(sql) for key, sql in queries.items()}


def _became_not_null(old_apps, new_apps):
    """(table, column) pairs that are NOT NULL at head but were nullable or absent at the start."""
    became = []
    for model in new_apps.get_app_config("finance").get_models():
        try:
            old = old_apps.get_model("finance", model.__name__)
            old_fields = {field.column: field for field in old._meta.concrete_fields}
        except LookupError:
            continue  # A table created after the start has no old rows to check.
        for field in model._meta.concrete_fields:
            previous = old_fields.get(field.column)
            if not field.null and (previous is None or previous.null):
                became.append((model._meta.db_table, field.column))
    return became


def _column_is_nullable(table, column):
    if connection.vendor != "postgresql":
        return None
    return _scalar(
        "SELECT is_nullable FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    ) == "YES"


@pytest.mark.migration
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("start", sorted(START_POINTS))
def test_populated_database_migrates_to_head(start):
    target = START_POINTS[start]
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(target)
        old_apps = executor.loader.project_state(target).apps
        began = time.monotonic()
        summary = builder.build(old_apps)
        built = time.monotonic()
        assert summary["transactions"] >= 2000
        assert summary["weak_auto_marked"] > 0

        money_queries = _money_queries(old_apps)
        counts, money = _counts(), _money(money_queries)
        deleted_alerts = old_apps.get_model("finance", "Alert").objects.filter(kind__in=DELETED_ALERT_KINDS).count()
        assert deleted_alerts > 0

        executor = MigrationExecutor(connection)
        executor.migrate(head)
        migrated = time.monotonic()
        new_apps = executor.loader.project_state(head).apps
        print(f"start {target[0][1]}: {summary}; built in {built - began:.1f}s, migrated in {migrated - built:.1f}s")

        after = _counts()
        expected = dict(counts, finance_alert=counts["finance_alert"] - deleted_alerts)
        for table, count in expected.items():
            if table in APPEND_ONLY:
                assert after[table] >= count, table
            else:
                assert after[table] == count, table
        assert _money(money_queries) == money

        became = _became_not_null(old_apps, new_apps)
        assert became, "expected at least one column to become NOT NULL after the start"
        for table, column in became:
            quote = connection.ops.quote_name
            assert _scalar(f"SELECT COUNT(*) FROM {quote(table)} WHERE {quote(column)} IS NULL") == 0, (table, column)
            assert _column_is_nullable(table, column) is not True, (table, column)

        # The data migrations on the way did their jobs on populated rows.
        Pair = new_apps.get_model("finance", "TransferPair")
        assert not Pair.objects.filter(status="auto_marked", leg_a__description="Synthetic plain move").exists()
        assert Pair.objects.filter(status="auto_marked", kind="card_payment").exists()
        Person = new_apps.get_model("finance", "Person")
        assert Person.objects.filter(privacy_policy_dismissed_version__isnull=False).count() == 1
        assert not new_apps.get_model("finance", "Budget").objects.filter(rollover_started_month__year__lt=2000).exists()
    finally:
        MigrationExecutor(connection).migrate(head)
