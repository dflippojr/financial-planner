"""Synthetic, isolated SQLite benchmark for issue #265.

Run before: python docs/perf/perf_unusual_spending.py --reference
Run after:  python docs/perf/perf_unusual_spending.py
Never reads deployment settings or an existing database.
"""
import statistics
import sys
from datetime import date
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _seed

_seed.bootstrap()

from django.db import connection
from django.test.utils import CaptureQueriesContext

from finance.models import Person
from finance.unusual_spending import compute_unusual_flags

if "--reference" in sys.argv:
    from finance import unusual_spending
    from tests.unusual_spending_reference import reference_merchant_flags

    unusual_spending._merchant_flags = reference_merchant_flags

print("seed_rows=72502 random_seed=42 seed_date=2026-10-08 engine=SQLite memory", flush=True)
_seed.seed(_seed.SEED_ROWS, today=_seed.SEED_DATE)
person = Person.objects.get(user__username="perf_a")
samples = []
reference = None
for _ in range(3):
    with CaptureQueriesContext(connection) as queries:
        started = perf_counter()
        flags = compute_unusual_flags(person, date(2026, 9, 1))
        samples.append(perf_counter() - started)
    if reference is None:
        reference = flags
    assert reference == flags
    print(f"seconds={samples[-1]:.3f} queries={len(queries)} flags={len(flags)}", flush=True)
print(f"median_seconds={statistics.median(samples):.3f}")
