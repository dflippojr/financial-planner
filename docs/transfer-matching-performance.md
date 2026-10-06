# Transfer matching performance (#244)

Measured on 2026-10-06, baseline `d4e752e`, in the throwaway Compose project
`fp-test-transfer-244`: PostgreSQL 18, Python 3.13, Django 5.2.17, debug off,
Gunicorn two workers with four threads. No production containers, volumes,
configuration or data were used. The project's normal Compose file and
entrypoint were not changed; the throwaway Compose definition lived in TEMP.

`tests/benchmark_transfer_matching.py` records the seed and request code.
The method follows the issue: shell function timing and `resource.getrusage`,
Django test-client edit POSTs with `CaptureQueriesContext` (one warm-up, median
of three), and a 500-row multipart upload followed by the commit POST through
Gunicorn using `urllib`; worker RSS comes from `docker top` before and after.
HTTP import/duplicate measurements below are individual commit POSTs, excluding
upload, mapping and the redirect target. Function timings are two runs.

| Operation | Before | After |
| --- | --- | --- |
| Description edit, 19,370 visible rows, median | 1.757 s / 22 queries | 0.021 s / 26 queries |
| All-duplicate 500-row import, ~73k ledger rows | 6.590 s | 0.243 s; no scoring |
| 500 new rows over HTTP, starting with 72,502 rows | 8.365 s | 2.418 s |
| Import worker RSS growth (before/after process RSS) | 96,900 ? 306,620 KiB (+204.8 MiB) | 108,788 ? 109,116 KiB (+0.32 MiB) |
| Shell refresh, 68,222 visible rows; after targets one changed row | 6.064 / 5.898 s, 8 queries | 0.018 / 0.012 s, 13 queries |
| Shell peak RSS | 73.3 ? 642.1 MiB | 73.9 ? 73.9 MiB |

The fresh synthetic seed has two household members, ten accounts, 36 months,
72,502 transactions, and `random.seed(42)`. It preserves the issue's visible row
counts (19,370 and 68,222). Compared with the issue's original seed, this fixture
uses uncategorized random outflows concentrated in three accounts and omits
budgets, planned items and tags. This isolates unrelated-ledger overhead; the
same seed and import file are used before and after. Before the after-change
new-row import, only the benchmark's 500 synthetic imported rows were removed.
These numbers do not claim to reproduce a transfer-heavy or densely ambiguous
ledger. The differential tests separately exercise ambiguous candidates, card
payments, private/shared visibility, settled decisions, corrections and archives.

Incremental refresh discovers complete affected candidate components (including
old candidates and stored pair legs), rather than arbitrary unrelated history.
A dense chain of equal-amount candidates can still require a large component:
this is needed to preserve both-leg confidence and deterministic occupancy. The
full maintenance rebuild intentionally retains whole-ledger cost.

For a repeat run, use only an empty throwaway `fp-test-NAME` Compose project with
its own PostgreSQL volume and loopback port. Copy the benchmark to
`/tmp/benchmark.py` in that app, run its `seed` mode once, then `refresh`,
`incremental`, `edit`, `import` and `duplicate` modes with `manage.py shell` as
shown in the script. Record RSS around imports. Remove the throwaway project
with `docker compose -p fp-test-NAME -f <throwaway-compose-file> down -v`.
