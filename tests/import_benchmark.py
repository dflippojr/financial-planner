"""Synthetic-only import measurement; run ONLY in a disposable database.

docker compose -p fp-test-NAME exec -T app python manage.py shell \
    -c 'from tests.import_benchmark import run; run()'

Each sample flushes and reseeds the database. Never run on an operator database.
"""
import random
import statistics
import time
from collections import Counter, deque
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext, setup_test_environment
from django.urls import reverse
from django.utils import timezone

from finance.category_services import ensure_household_categories
from finance.models import (
    Account, AlertSettings, Budget, BudgetAmount, Category, CategoryRule,
    Household, ImportBatch, Membership, Person, PlannedItem, Tag, Transaction,
    TransactionTag,
)
from tests.huntington_fixtures import huntington_csv


def import_content(size, profile):
    if profile == "huntington":
        return huntington_csv([
            f"09/15/2026,{i},SYNTHETIC MERCHANT {i},,-12.34,,{i}"
            for i in range(size)
        ])
    rows = "".join(
        f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260915<TRNAMT>-12.34"
        f"<FITID>synthetic-{i}<NAME>SYNTHETIC MERCHANT {i}</STMTTRN>"
        for i in range(size)
    )
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n"
            "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD"
            f"<BANKTRANLIST>{rows}</BANKTRANLIST>"
            "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()


def seed(*, budgets=12):
    rng = random.Random(42)
    people = []
    household = Household.objects.create(name="Synthetic benchmark household")
    for i in range(2):
        user = get_user_model().objects.create_user(username=f"synthetic-{i}")
        person = Person.objects.create(user=user, display_name=f"Synthetic member {i}")
        Membership.objects.create(person=person, household=household)
        AlertSettings.objects.create(person=person)
        people.append(person)
    ensure_household_categories(household)
    categories = list(Category.objects.filter(household=household).exclude(code="transfer"))
    accounts = []
    for i in range(10):
        shared = i % 3 == 0
        accounts.append(Account.objects.create(
            owner=people[i % 2], name=f"Synthetic account {i}", account_type="checking",
            scope="household" if shared else "private",
            household=household if shared else None, share_mode="co_owned" if shared else "",
        ))
    rows = []
    for account in accounts:
        batch = ImportBatch.objects.create(
            account=account, imported_by=account.owner, source="huntington",
            source_file_sha256="a" * 64, date_range_start=date(2023, 10, 1),
            date_range_end=date(2026, 9, 30),
        )
        for i in range(2250):
            category = rng.choice(categories) if rng.random() < .85 else None
            rows.append(Transaction(
                account=account, import_batch=batch,
                transaction_date=date(2023, 10, 1) + timedelta(days=rng.randrange(1096)),
                amount_minor=-rng.randrange(100, 10000), currency="USD",
                description=f"SYNTHETIC HISTORY {account.pk} {i}", kind="cash_flow",
                source_row_number=i + 1, fingerprint=f"{account.pk:032x}{i:032x}", original_fields={},
                category=category, category_source=Transaction.CategorySource.MANUAL if category else Transaction.CategorySource.UNSET,
            ))
    Transaction.objects.bulk_create(rows)
    tag = Tag.objects.create(household=household, name="Synthetic tag")
    TransactionTag.objects.bulk_create([TransactionTag(transaction=row, tag=tag) for row in rows[:1500]])
    for i in range(budgets):
        budget = Budget.objects.create(owner=people[0], category=categories[i], scope="private")
        BudgetAmount.objects.create(budget=budget, effective_month=date(2023, 10, 1), amount_minor=1000000)
    for i in range(4):
        PlannedItem.objects.create(owner=people[0], name=f"Synthetic plan {i}", kind="expense",
                                   amount_minor=1000, start_date=date(2026, 10, 1), cadence="monthly")
    CategoryRule.objects.bulk_create([
        CategoryRule(owner_household=household, description_contains=f"SYNTHETIC MERCHANT {i}",
                     category=categories[i % len(categories)], priority=i, confirmed_at=timezone.now())
        for i in range(30)
    ])
    return people[0], accounts[0]


def measure(size, profile, *, budgets=12, matching=True):
    if connection.settings_dict["NAME"] != "synthetic" or connection.settings_dict["HOST"] != "db":
        raise RuntimeError("Measurements require the disposable compose database named synthetic at db.")
    call_command("flush", interactive=False, verbosity=0)
    person, account = seed(budgets=budgets)
    client = Client()
    client.force_login(person.user)
    url = reverse("csv-import-preview", args=[account.pk])
    client.get(url)  # warm up the request path
    response = client.post(url, {"action": "upload", "import_profile": profile,
        "csv_file": SimpleUploadedFile(f"synthetic.{ 'csv' if profile == 'huntington' else 'ofx'}",
                                       import_content(size, profile))})
    assert response.status_code == 200
    form = response.context["mapping_form"]
    data = {"action": "commit", "token": form.data["token"],
            "date_range_start": "2026-09-01", "date_range_end": "2026-09-30"}
    from finance.category_services import refresh_transfer_pairs
    match_seconds = []
    def refresh(*args, **kwargs):
        start = time.perf_counter()
        result = refresh_transfer_pairs(*args, **kwargs) if matching else None
        match_seconds.append(time.perf_counter() - start)
        return result
    connection.queries_log = deque(maxlen=1000000)
    with patch("finance.category_services.refresh_transfer_pairs", side_effect=refresh):
        with CaptureQueriesContext(connection) as queries:
            start = time.perf_counter()
            committed = client.post(url, data)
            elapsed = time.perf_counter() - start
    assert committed.status_code == 302
    assert Transaction.objects.count() == 22500 + size
    return {"queries": len(queries), "seconds": elapsed,
            "excluding_matching_seconds": elapsed - sum(match_seconds),
            "tables": Counter(q["sql"].split(' FROM ')[-1].split(' ')[0] for q in queries
                              if q["sql"].startswith("SELECT"))}


def run(sizes=(500, 2000, 5000, 10000), profiles=("huntington", "ofx"), *, budgets=12, samples=3):
    setup_test_environment()
    for profile in profiles:
        for size in sizes:
            results = [measure(size, profile, budgets=budgets) for _ in range(samples)]
            print(profile, size, "queries", [r["queries"] for r in results],
                  "median_seconds", round(statistics.median(r["seconds"] for r in results), 4),
                  "excluding_matching", round(statistics.median(r["excluding_matching_seconds"] for r in results), 4),
                  flush=True)
