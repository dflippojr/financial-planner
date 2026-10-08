"""Shared deterministic benchmark seed for the scripts in docs/perf. Synthetic only.

The seed is 2 members, 1 household, 10 accounts, 36 months, about 85% of the
rows categorised and 1,500 tagged, from `random.Random(42)`. Import this module
only after Django is configured (`bootstrap()` or `manage.py shell`).
"""
import os
import random
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

PASSWORD = "Synthetic-passphrase-42!"
SEED_DATE = date(2026, 10, 8)
SEED_ROWS = 72_502


def bootstrap():
    """Configure Django on a fresh in-memory SQLite database and migrate it.

    Never reads deployment settings or an operator database.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "financial_planner.test_settings"
    os.environ.pop("FINANCIAL_PLANNER_TEST_DB", None)

    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection

    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    settings.ALLOWED_HOSTS = ["localhost"]
    call_command("migrate", verbosity=0)


def use_plain_static_storage():
    """Time pages, not static-file hashing: skip the production manifest lookup."""
    from django.conf import settings

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def seed(rows, today=None):
    """Create the deterministic synthetic ledger; `today` pins the date window."""
    from django.contrib.auth import get_user_model

    from finance.category_services import ensure_household_categories
    from finance.models import (
        Account,
        Category,
        Household,
        ImportBatch,
        Membership,
        Person,
        Tag,
        Transaction,
        TransactionTag,
    )

    today = today or date.today()
    rng = random.Random(42)
    users = get_user_model()
    people = []
    for name in ("perf_a", "perf_b"):
        user = users.objects.create_user(username=name, password=PASSWORD)
        people.append(Person.objects.create(user=user, display_name=name))
    household = Household.objects.create(name="Perf household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    categories = list(Category.objects.filter(household=household).exclude(code=Category.Code.TRANSFER))
    accounts = []
    for index in range(10):
        shared = index < 5
        accounts.append(
            Account.objects.create(
                name=f"Perf account {index}",
                account_type=Account.Type.CREDIT_CARD if index % 3 == 0 else Account.Type.CHECKING,
                owner=people[index % 2],
                scope=Account.Scope.HOUSEHOLD if shared else Account.Scope.PRIVATE,
                household=household if shared else None,
                share_mode=Account.ShareMode.CO_OWNED if shared else "",
            )
        )
    start = today - timedelta(days=36 * 30)
    batches = {
        account.pk: ImportBatch.objects.create(
            account=account,
            imported_by=account.owner,
            source=ImportBatch.Source.HUNTINGTON,
            source_file_sha256="b" * 64,
            date_range_start=start,
            date_range_end=today,
        )
        for account in accounts
    }
    objects = []
    for number in range(rows):
        account = rng.choice(accounts)
        amount = rng.randint(500, 40000) * (1 if rng.random() < 0.15 else -1)
        categorised = rng.random() < 0.85
        objects.append(
            Transaction(
                account=account,
                import_batch=batches[account.pk],
                transaction_date=start + timedelta(days=rng.randint(0, 36 * 30)),
                amount_minor=amount,
                description=rng.choice(["Kroger", "Shell", "Payroll", "Netflix", "Target", "Water bill"]),
                source_row_number=number + 2,
                fingerprint=f"{number:064x}",
                original_fields={"n": number},
                category=rng.choice(categories) if categorised else None,
                category_source=Transaction.CategorySource.MANUAL if categorised else "",
            )
        )
    Transaction.objects.bulk_create(objects, batch_size=2000)
    tag = Tag.objects.create(household=household, name="perf")
    tagged = list(Transaction.objects.order_by("pk").values_list("pk", flat=True)[:1500])
    TransactionTag.objects.bulk_create([TransactionTag(transaction_id=pk, tag=tag) for pk in tagged])


def timed(client, path, runs=3):
    """One warm-up GET, the median of `runs` timed GETs, then a query-counting GET."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.get(path)
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        response = client.get(path)
        samples.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200, (path, response.status_code)
    with CaptureQueriesContext(connection) as queries:
        client.get(path)
    return statistics.median(samples), len(queries)
