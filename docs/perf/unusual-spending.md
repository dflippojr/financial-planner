# Unusual-spending performance (#265)

Synthetic diagnostics on Windows Python 3.10.11 / Django 5.2.17, in isolated
in-memory SQLite databases, seeded as of 2026-10-08. No production data or
containers were used. Reproduce with:

```powershell
python docs/perf/perf_unusual_spending.py --reference
python docs/perf/perf_unusual_spending.py
```

The reference preserves the merchant implementation from
`04b713a583e1d896c8756cdaaeca0c061cb43af0`; the optimized implementation is
`e40e37a`. Both use the shared seed described in `README.md` (72,502 rows).
Each sample calls `compute_unusual_flags(perf_a_person, date(2026, 9, 1))`
with `perf_counter` and `CaptureQueriesContext`. These two three-sample runs
had no overlapping benchmark or test process; no profiling was mixed in.

| Implementation | Three samples (seconds) | Median | Flags | SQL queries |
| --- | --- | --- | --- | --- |
| Scan/sort reference | 6.354, 6.469, 5.860 | 6.354 s | 7 | 49, 46, 46 |
| Chronological heaps | 0.455, 0.334, 0.350 | 0.350 s | 7 | 49, 46, 46 |

Median latency decreased **94.5%** (18.2 times faster). The first sample creates
alert settings, accounting for its three extra queries. This is a same-machine
CPU/algorithm comparison, not a prediction of PostgreSQL production latency;
the issue's original 12.995-second measurement was a separate run.

Merchant analysis now streams six fields ordered by corrected date and ID.
Each eligible charge is inserted once into its merchant's two heaps, after
comparison with the strictly earlier history. Exact integer halves yield
Decimal medians without rounding the comparison. History remains unlimited;
the six-month category policy is unchanged.

Differential tests compare complete flags and ordering against the old oracle,
including same-day IDs, odd/even medians, half-even display rounding, threshold
boundaries, eligibility, private/shared accounts, hidden transfer counterparts,
and membership revocation. A regression test counts exactly 11,001 insertions
and 1,000 median reads for a frequent merchant and rejects per-charge sorting;
it has no wall-clock gate.
