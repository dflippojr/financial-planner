# Background AI job concurrency (#247)

Measured on 2026-10-06 on the tower, using only a throwaway copy under the
system temp directory and Compose project `fp-test-ai247`. Its PostgreSQL 18
database, network and volume are separate from production. The synthetic
ledger contains two members, one household, ten checking accounts and 22,500
transactions over 36 months, generated with `random.seed(42)`.

The before image is main at `93f618b` (#256's consolidated background runner).
The after image adds the three-worker batch lane. Both use the existing shared
harness poll (0.25, 0.5, then 0.6 seconds), Gunicorn preload with two workers
and four threads, and a 512 MiB background cap. The isolated app and background
have `AGENT_HARNESS_HOSTED_SESSIONS=true`; no production setting was read.

As in the issue, a time-exact fake HTTP harness completes each session five
seconds after creation, independently of polling frequency. Each run calls
`enqueue_job(person, feature="structured")` four times from one `manage.py
shell`, then reads `AiJob.finished_at` every 100 ms. Times are measured from
immediately before the first enqueue. The fake also records every session
creation, so duplicates can be counted independently of final job rows.

| Run | Before job 1 | Before job 2 | Before job 3 | Before job 4 | After job 1 | After job 2 | After job 3 | After job 4 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 19.449 s | 25.025 s | 30.603 s | 36.180 s | 5.843 s | 5.844 s | 5.845 s | 11.841 s |
| B | 20.057 s | 25.635 s | 31.215 s | 36.793 s | 7.489 s | 7.490 s | 7.490 s | 13.493 s |
| C | 18.499 s | 24.076 s | 29.655 s | 35.232 s | 5.974 s | 5.973 s | 5.974 s | 11.976 s |

All four finish within 14 seconds in each after run. The fourth waits for a
free worker; the first three start together. Their start detection times were
0.327, 1.935 and 0.422 seconds respectively. All 24 jobs across before and after
runs have one attempt and distinct harness sessions.

The issue's original 32–35-second baseline used main at `73fdcc4` and the older
0.5/1/2/4-second harness back-off. The new before measurement above includes
the already-merged shared poll fix and a different phase of the 15-second
idle poll; it does not treat the issue's old measurements as new measurements.

Twenty further jobs, queued at varied poll phases while a worker was free,
started within 2.029 seconds; nearest-rank p95 was 2.028 seconds. Start is
detected when the saved harness session ID appears, using the same 100 ms
database poll. Jobs waiting for an occupied pool and local-model quiet-window
jobs are not part of this dispatch-latency sample.

A separate run queued 200 five-second jobs and forcibly restarted only the
throwaway background container with `restart -t 0` while three jobs had saved
sessions and were still running. All 200 succeeded, each with one attempt and
a distinct session. The interrupted jobs resumed their original IDs. Across
the four-job runs, restart run and latency samples, the fake recorded exactly
244 session creations for 244 jobs, with no extra creation. The 200-job run
finished in 403.923 seconds, including recovery.

For that restart drill only, the throwaway runner used a ten-second session
timeout and two-second stale margin so recovery could be observed without
waiting twelve minutes. Production defaults (600 plus 120 seconds), retry
back-off and the local-model quiet window are unchanged. Background cgroup
memory peaked at 72.5 MiB during these structured-job measurements, with its
512 MiB cap active and no OOM events. This is a structured-job measurement,
not a new memory bound for every feature or a simultaneous daily pass.

The full SQLite suite passed 1,621 tests with 14 skips; the full PostgreSQL 18
suite passed 1,634 tests with one skip. Focused PostgreSQL
tests cover competing pools across 200 jobs, the existing stale-running
recovery and quiet window, failure isolation, bounded submission and lane
independence. Migration 0055 adds only the two job poll indexes.
