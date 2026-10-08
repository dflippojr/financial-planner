"""Seed synthetic data and time the totals-heavy pages. Synthetic only.

Run against a THROWAWAY database, never production, from the repository root:

    PERF_SEED=22500 python manage.py shell -c "exec(open('docs/perf/perf_pages.py').read())"   # seed, then time
    PERF_RUNS=5 python manage.py shell -c "exec(open('docs/perf/perf_pages.py').read())"   # time only

The seed (see `_seed.py`) is 2 members, 1 household, 10 accounts, 36 months,
about 85% of the rows categorised and 1,500 tagged, from `random.seed(42)`.
Timing is the Django test client with one warm-up request, then the median of
PERF_RUNS requests.
"""
import os
import sys

sys.path.insert(0, "docs/perf")

from django.contrib.auth import get_user_model
from django.test import Client

import _seed
from finance.models import Category, Transaction

SEED_ROWS = int(os.environ.get("PERF_SEED", "0"))
RUNS = int(os.environ.get("PERF_RUNS", "3"))

_seed.use_plain_static_storage()

if SEED_ROWS:
    _seed.seed(SEED_ROWS)

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
    median_ms, queries = _seed.timed(client, path, RUNS)
    print(f"{path:40s} {median_ms:8.0f} ms  {queries:4d} queries")
