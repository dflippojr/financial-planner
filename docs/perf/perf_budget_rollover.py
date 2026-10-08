"""Issue #266 benchmark. Run: python docs/perf/perf_budget_rollover.py

Always uses a fresh in-memory SQLite database, never an operator database.
Uses the deterministic seed in _seed.py, fixed to October 8, 2026.
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _seed

_seed.bootstrap()

import django
from django.test import Client

from finance.models import Budget, BudgetAmount, Category, Person, Transaction

_seed.seed(_seed.SEED_ROWS, today=_seed.SEED_DATE)
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
            median_ms, queries = _seed.timed(client, path)
            print(f"{history:2d} prior months {path:24s} {median_ms:8.1f} ms {queries:4d} queries")
