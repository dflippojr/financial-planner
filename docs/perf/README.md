# Performance benchmarks

Synthetic data only. Never run these against production or an operator
database. Per-issue results stay in the files next to this one.

## How to run

All scripts share `_seed.py`: a deterministic ledger from `random.Random(42)`
with two members, one household, ten accounts, 36 months, about 85% of rows
categorised and 1,500 tagged (72,502 rows for the recorded runs, dated
2026-10-08). `_seed.bootstrap()` also configures Django on a fresh in-memory
SQLite database. Timing is the Django test client: one warm-up GET, the median
of N GETs, then one query-counting GET.

From the repository root:

- `python docs/perf/perf_budget_rollover.py` (#266, results in `budget-rollover-266.md`)
- `python docs/perf/perf_unusual_spending.py [--reference]` (#265, results in `unusual-spending.md`)
- `PERF_SEED=22500 python manage.py shell -c "exec(open('docs/perf/perf_pages.py').read())"`
  seeds a throwaway database and times the totals-heavy pages; use `PERF_RUNS=5`
  without `PERF_SEED` to time an already-seeded throwaway database.

Compose-based measurements (`docs/performance-ai-jobs.md`,
`docs/transfer-matching-performance.md`) use a throwaway `fp-test-NAME` project
and are removed with `docker compose -p fp-test-NAME -f <file> down -v`.
