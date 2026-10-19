"""Autovacuum must not rewrite planner statistics under the suite (issue #555).

Every test rolls back its rows, so autovacuum eventually vacuums the tables
while they are empty and records `reltuples = 0`. The planner then assumes one
row per table, and for the correlated transfer-exclusion subquery it joins
the transaction table with itself before probing the pairs. A test with 500
transactions then ran for ten minutes. Never-vacuumed tables get a row estimate
from their real size, which gives the plan production sees.
"""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


@pytest.mark.skipif(connection.vendor != "postgresql", reason="autovacuum is PostgreSQL only")
def test_autovacuum_is_off_for_every_test_table():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND NOT COALESCE('autovacuum_enabled=false' = ANY (c.reloptions), false)"
        )
        assert [name for (name,) in cursor.fetchall()] == []
