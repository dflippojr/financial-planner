"""Seed an empty PostgreSQL database for a synthetic deploy-rehearsal dump (#538).

Every app but finance is migrated to its newest migration and finance stops at
--finance-migration, the way a production database sits before a release's new
migrations. builder.build() then writes the synthetic rows through that state's
historical models. The builder writes rows that 0062 must repair, so
0061_savings_goal_wishlist is the newest state it can seed.
scripts/build-synthetic-dump.sh runs it against a throwaway server and dumps
the result for ops/deploy-rehearsal.ps1 -DumpPath.

    python -m tests.migration_fixtures.seed --finance-migration 0061_savings_goal_wishlist

It reads the POSTGRES_* settings and refuses a database that already has tables.
"""
import argparse
import json
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--finance-migration", required=True, help="The finance migration the dump stops at.")
    parser.add_argument("--size", choices=("small", "large"), help="MIGRATION_FIXTURE_SIZE; small by default.")
    args = parser.parse_args(argv)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_planner.test_settings")
    os.environ["FINANCIAL_PLANNER_TEST_DB"] = "postgres"
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from tests.migration_fixtures import builder

    if connection.introspection.table_names():
        sys.exit("seed: the database already has tables; point POSTGRES_* at an empty one")
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    finance = ("finance", args.finance_migration)
    if finance not in executor.loader.graph.nodes:
        sys.exit(f"seed: no finance migration named {args.finance_migration}")
    # The migrate command, not the executor, so post_migrate adds the content
    # types and permissions a deployed database already has.
    call_command("migrate", "finance", args.finance_migration, verbosity=0)
    for app, name in sorted(leaves):
        if app != "finance":
            call_command("migrate", app, name, verbosity=0)
    executor = MigrationExecutor(connection)
    applied = {node for node in executor.loader.applied_migrations if node[0] == "finance"}
    if any(node[1] > args.finance_migration for node in applied):
        sys.exit("seed: another app's migrations pulled finance past --finance-migration")
    summary = builder.build(executor.loader.project_state([finance]).apps, months=builder.months_for(args.size))
    print(json.dumps({"finance_migration": args.finance_migration, **summary}, sort_keys=True))


if __name__ == "__main__":
    main()
