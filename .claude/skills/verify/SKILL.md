---
name: verify-financial-planner
description: Launch a disposable financial-planner instance on synthetic data, check its health, drive the main path in a browser, save evidence and clean up everything it started. Use to verify this change, launch and check, answer "does it work in the app", or smoke test a change in the real app rather than only in unit tests.
---

# Verify financial-planner in the real app

This skill proves a change works in a running app. It wraps the disposable
instance contract in `docs/ui-testing.md`; that file stays authoritative for
setup, isolation, seed data, sign-in and teardown. Do not restate or bend its
rules here; if the skill needs a new fact, add it to the contract first.

Prerequisites: the contract's "Scope and isolation" setup (venv, `npm ci`,
Chromium for Playwright, built CSS). Run everything from the repo root with the
venv's Python (`.venv/Scripts/python.exe` on Windows, `.venv/bin/python` on Linux).

## One command

```bash
python .claude/skills/verify/scripts/verify.py                 # whole main path
python .claude/skills/verify/scripts/verify.py --feature alert # one area from the feature map
```

It runs the five steps below in order, prints the evidence folder and exits 0
only when every step passed and cleanup was verified. Cleanup runs even when
launch, doctor or drive fails. Read `summary.md` in the evidence folder and
report it; on failure, read `drive.log`, `log-excerpts.txt` and
`test-results/` there before changing code.

## Steps

1. **Launch.** Starts `scripts/ui_instance.py` exactly as the contract's
   "Start" section describes, as an owned background process with
   `--startup-handle` (see "Teardown"), from an allowlisted environment.
   Each process the skill starts is owned with all its descendants: a
   process group on Linux, a Job Object on Windows.
   Before reading readiness it writes `ledger.json` with the launcher PID, log
   path and startup handle; after readiness it adds URL, port and instance
   directory. The readiness sign-in is not written to evidence.
2. **Doctor.** `scripts/doctor.py` checks `/health/`, that the URL is
   `127.0.0.1` on the instance's own port and does not answer on non-loopback
   addresses, that storage is the disposable SQLite directory, that the
   contract's "Seed and sign-in" data is present, and that the launch
   environment holds no credential or data-path variables (names only, never
   values). One PASS/FAIL line per check; non-zero exit on any failure.
   Standalone: `python .claude/skills/verify/scripts/doctor.py --ledger .verify/<run>/ledger.json`.
3. **Drive.** Playwright (`playwright.verify.config.ts`) against the launched
   URL, reusing `signIn` and `submit` from `tests/ui/fixtures.ts`. The desktop
   project (1440x900) drives the feature map in order on one instance; a
   read-only phone project (Pixel 7) then screenshots the same pages.
   `--feature NAME` limits both to one row of the map.
4. **Evidence.** `.verify/<timestamp>-<feature>/` (git-ignored):
   `summary.md`, `doctor.txt`, `screenshots/desktop-*.png` and `phone-*.png`,
   `drive-results.json`, `drive.log`, `instance.log`, `log-excerpts.txt`
   (errors, warnings and 5xx lines plus the log tail) and `ledger.json`.
   Never commit or paste it anywhere public beyond `summary.md`.
5. **Cleanup.** Stops the drive's process group or job (even if its parent
   already exited), runs the contract's "Teardown" command for the recorded
   directory, waits for the launcher to exit, then verifies: no process
   remains in any owned group or job, the port refuses connections, the
   instance directory is gone and the startup handle is removed. Each action
   runs even when an earlier one fails; failures become FAIL rows. A forced stop or retained directory is a cleanup FAIL; follow
   the contract's "Teardown" recovery rules for it. Never stop processes by
   name or by guessing a port.

## Feature map (main path)

| Feature | Page / route | How it is driven | What proves it |
| --- | --- | --- | --- |
| (sign-in) | `/sign-in/` | `signIn` helper before every check, seeded account from the contract | Cash flow heading after submit |
| `import` | `/accounts/` → Import CSV → `/accounts/<id>/imports/preview/` | Generic profile, `tests/fixtures/ui-pass/synthetic.csv`, mapping as in the contract's CSV journey, Preview rows, then Import new rows | Preview says "2 new; 0 duplicate; 1 invalid."; `/transactions/` shows -12.34 and +56.78 rows |
| `cashflow` | `/` | Open Home | Key numbers region shows Income, Spending, Net cash flow; By period section present |
| `recurring` | `/recurring/` | Open recurring review | Recurring charges and Confirmed headings |
| `dashboard` | `/` side panels | Open Home | Budgets panel lists Dining; Needs attention panel visible |
| `alert` | `/alerts/` | Mark the seeded "Dining is over budget" alert read, reload, follow its link | Row loses Unread and Mark as read; link reaches Budgets |

Add a row (and a test in `scripts/drive.verify.ts`, plus its phone page in
`scripts/features.ts`) when a new area joins the main path. Deeper journeys
belong in `tests/ui/*.spec.ts` per the contract's "Journeys", not here.

## Tests

`tests/test_verify_skill.py` covers the doctor checks (healthy, not ready,
non-loopback, missing seed, credentials, storage) and runs the whole skill with
a deliberately failed drive step whose parent exits and leaves a child behind,
proving cleanup leaves nothing running (marked `browser` because it needs built
CSS). A unit test checks that a failing stop does not abort the rest of cleanup.
