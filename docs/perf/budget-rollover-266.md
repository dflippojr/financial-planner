# Budget history batching (#266)

Measured on the same Windows machine with Python 3.10.11, Django 5.2.17 and
fresh in-memory SQLite databases. No production services, containers, volumes
or data were accessed. No tests or other benchmarks overlapped these runs.
These are local diagnostic wall times, not PostgreSQL or deployment claims.

Run `python docs/perf/perf_budget_rollover.py`. It reuses the deterministic
`perf_pages.py` seed: two members, one household, ten accounts and 72,502
synthetic transactions. The seed date and request date are October 8, 2026.
There are twelve private category budgets, each with 100,000 minor units
effective October 2023. Each route gets one warm-up, the median of three GETs,
and a separate query-count GET. The test-settings overlay replaces the
database with in-memory SQLite, disables HTTPS redirects and uses unhashed
static storage.

Before: `04b713a`. After: `4a2c39b`.

| Prior months | Home before ms / queries | Home after ms / queries | Budgets before ms / queries | Budgets after ms / queries |
| --- | ---: | ---: | ---: | ---: |
| Off | 117.1 / 46 | 186.9 / 34 | 36.6 / 34 | 56.5 / 22 |
| 12 | 409.1 / 286 | 213.0 / 34 | 334.4 / 274 | 116.5 / 22 |
| 36 | 1,006.7 / 718 | 444.9 / 34 | 892.0 / 706 | 269.8 / 22 |

At 36 months, rollover overhead relative to rollover off drops from 889.6 to
258.0 ms on Home (71.0%) and from 855.4 to 213.3 ms on Budgets (75.1%).
Absolute timings vary; the fixed query counts are the regression gate.

Configuration reads are one amount query and one reset query for the visible
budgets. Historical spending uses the existing two-query window aggregation
per member/report scope and the same category-combination helper as the
spending report. The current schema has flat categories, with no parent/child
relationship; its existing category and Uncategorized combination semantics
are preserved without introducing a new category policy.

`tests/test_budget_history_queries.py` compares every card field and generated
alert against the former per-month reports. Its ledger exercises splits,
stored-category refunds whose purchases become private, transfer visibility,
investment exclusions, amount changes (including future changes), negative
carry, resets, rollover off/on periods, archived budgets, selected accounts,
private/household scopes and membership revocation. Query assertions cover
Home, Budgets, individual snapshots, batched snapshots and active alerts.
