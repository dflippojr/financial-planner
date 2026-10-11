# UI testing

## Start command and URL

Use this checkout with Python 3.12+ and Bash (Git Bash on Windows). Install pinned
dependencies once: `python -m venv .venv`, then use the venv's Python to run
`python -m pip install -r requirements-test.txt` and
`python -m playwright install chromium`. On Windows, create the venv with
`py -3.13 -m venv .venv` and use `.venv/Scripts/python.exe`.

Start: `python scripts/ui_instance.py` (with the venv active). It builds missing
CSS, migrates a fresh SQLite file under the system temp directory, and seeds
synthetic data. The final startup line is JSON containing `url` and `directory`.
Open that URL; its free port changes on every start. Keep the command running.
The server binds only to `127.0.0.1`. No Docker, production database, or `.env`
is used. Start in a shell without `DATABASE*`, `POSTGRES_*`, `PG*`, or `DB_*`
database connection variables; the script refuses configured databases.
Uploads, receipts and operator audit files are also temporary. Inherited AI,
SMTP, Google sign-in and policy settings are discarded.

## Seed data and sign-in

Sign in at `/sign-in/` as `synthetic_alex` with
`Synthetic-passphrase-42!`. These are disposable test credentials.
The seed uses `tests.mobile_seed.seed_phone_data()`: Synthetic Checking,
recent payroll and purchases (including Shell Oil 5741, Corner Bistro and
Kroger), categories, three budgets and a budget alert. Dates track today.
The synthetic privacy policy is already accepted. Chat uses an in-process
fake Agent Harness on another free loopback port, with a local chat worker;
it never uses paid provider credentials. Its reply is
`Synthetic UI reply. No real provider was contacted.`

## Flows

1. Sign in: submit the credentials and reach Home (`/`), headed Cash flow.
   Verify Income, Spending and Net cash flow appear at desktop and phone widths.
2. Activity (`/transactions/`): find Shell Oil 5741; open Correct. Select
   Transportation and Save category. Reload the correction page and Activity:
   the selection, category audit row and list category must persist.
3. Correct transaction: change the description and Save correction. Save a
   synthetic note and a new tag with Save note and tags, then reload. Split
   the Shell purchase into USD 20.00 and 21.18 with distinct categories; the
   parts must total USD 41.18. Verify both parts after reload, then Unsplit.
4. Budgets (`/planning/budgets/`): inspect the seeded Dining, Groceries and
   Transportation budgets; edit a limit and verify it persists after reload.
5. Chat (`/chat/`): send a synthetic question and wait for the fake reply above.
   Reload to verify conversation history; no external provider setup is needed.
6. Settings (`/settings/security/`): inspect sessions and visit the categories,
   tags, alerts, data and AI tabs. AI shows the loopback fake connection.
7. Sign out using the application button, then visit Activity directly; it
   must return to sign-in.

The first automated flow runs with
`python -m pytest tests/test_ui_instance.py -q -m browser`.
It performs real sign-in and a category save, then checks the reloaded form,
audit history and Activity list. CI uses GitHub-hosted `ubuntu-latest`, with no skips
allowed for a missing browser. Use synthetic data only in reports and traces.

## Teardown

Press Ctrl-C in the start terminal, or run
`python scripts/ui_instance.py --teardown "<directory from startup JSON>"`
in another terminal. Wait for the start command to exit: it stops the fake
provider and chat worker, closes SQLite, and removes the whole temporary
directory. Teardown sends a token-protected request to that instance only;
it never kills an arbitrary PID or deletes a user-supplied directory.
If the process is forcibly killed, its temporary directory may remain; remove
only the exact printed directory after verifying the process has stopped.
