"""Issue #266 benchmark. Run: python docs/perf/perf_budget_rollover.py

Always uses a fresh in-memory SQLite database, never an operator database.
Reuses perf_pages.py's deterministic seed, fixed to October 8, 2026.
"""
import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["DJANGO_SETTINGS_MODULE"] = "financial_planner.test_settings"
os.environ.pop("FINANCIAL_PLANNER_TEST_DB", None)

import django

django.setup()

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import Client

from finance.models import Budget, BudgetAmount, Category, Person, Transaction

assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
settings.ALLOWED_HOSTS = ["localhost"]
call_command("migrate", verbosity=0)

namespace = {"__name__": "perf_seed"}
source = Path(__file__).with_name("perf_pages.py").read_text()
exec(source.split("from django.conf import settings")[0], namespace)


class SeedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 8)


namespace["date"] = SeedDate
namespace["seed"](72_502)
owner = Person.objects.get(user__username="perf_a")
categories = Category.objects.filter(household__memberships__person=owner).exclude(
    code=Category.Code.TRANSFER
).order_by("pk")[:12]
for category in categories:
    budget = Budget.objects.create(owner=owner, scope=Budget.Scope.PRIVATE, category=category)
    BudgetAmount.objects.create(budget=budget, effective_month=date(2023, 10, 1), amount_minor=100_000)

client = Client(HTTP_HOST="localhost")
client.force_login(owner.user)
print(f"SQLite {django.get_version()=}, transactions: {Transaction.objects.count()}, median of 3 GETs")
with patch("django.utils.timezone.localdate", return_value=date(2026, 10, 8)):
    for history in (0, 12, 36):
        Budget.objects.update(
            rollover_enabled=bool(history),
            rollover_started_month=date(2026 - history // 12, 10, 1) if history else None,
        )
        for path in ("/", "/planning/budgets/"):
            median_ms, queries = namespace["timed"](client, path)
            print(f"{history:2d} prior months {path:24s} {median_ms:8.1f} ms {queries:4d} queries")
