"""Seed synthetic data and time the totals-heavy pages. Synthetic only.

Run against a THROWAWAY database, never production:

    PERF_SEED=22500 python manage.py shell -c "exec(open('scripts/perf_pages.py').read())"   # seed, then time
    PERF_RUNS=5 python manage.py shell -c "exec(open('scripts/perf_pages.py').read())"   # time only

The seed is 2 members, 1 household, 10 accounts, 36 months, about 85% of the
rows categorised and 1,500 tagged, from `random.seed(42)`. Timing is the Django
test client with one warm-up request, then the median of PERF_RUNS requests.
"""
import os
import random
import statistics
import time
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

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

PASSWORD = "Synthetic-passphrase-42!"
SEED_ROWS = int(os.environ.get("PERF_SEED", "0"))
RUNS = int(os.environ.get("PERF_RUNS", "3"))


def seed(rows):
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
    start = date.today() - timedelta(days=36 * 30)
    batches = {
        account.pk: ImportBatch.objects.create(
            account=account,
            imported_by=account.owner,
            source=ImportBatch.Source.HUNTINGTON,
            source_file_sha256="b" * 64,
            date_range_start=start,
            date_range_end=date.today(),
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


def timed(client, path):
    client.get(path)
    samples = []
    for _ in range(RUNS):
        started = time.perf_counter()
        response = client.get(path)
        samples.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200, (path, response.status_code)
    with CaptureQueriesContext(connection) as queries:
        client.get(path)
    return statistics.median(samples), len(queries)


from django.conf import settings

# Page timing, not static-file hashing: skip the production manifest lookup.
settings.STORAGES = {
    **settings.STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

if SEED_ROWS:
    seed(SEED_ROWS)

user = get_user_model().objects.get(username="perf_a")
client = Client(HTTP_HOST="localhost")
client.force_login(user)
category_id = Category.objects.filter(household__memberships__person=user.person).order_by("pk").values_list("pk", flat=True)[2]
print(f"transactions: {Transaction.objects.count()}")
for path in (
    "/",
    "/planning/year-end/",
    f"/spending/category/{category_id}/",
    "/transactions/?q=Kroger",
    "/spending/",
):
    median_ms, queries = timed(client, path)
    print(f"{path:40s} {median_ms:8.0f} ms  {queries:4d} queries")
