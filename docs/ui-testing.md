# UI testing contract

Contract version: 1

## Scope and isolation

Public repo dflippojr/financial-planner. Use only scripts/ui_instance.py from this checkout. Never use Docker, financial-planner production compose, live Postgres, .env, a reused server or an existing browser profile. Python 3.12+, Node 22+, Bash (Git Bash on Windows), gh 2.99+ and playwright-cli are required.

Setup: create .venv (py -3.13 -m venv .venv on Windows), install requirements-test.txt with its Python, python -m playwright install chromium webkit, npm ci, npx playwright install chromium webkit, bash scripts/build_css.sh. Windows Python: .venv/Scripts/python.exe; Linux: .venv/bin/python. No real bank, Google, SMTP or paid AI credentials are needed.

The launcher refuses inherited DATABASE*, POSTGRES_*, PG*, DB_HOST/DB_NAME/DB_URL/DB_PASSWORD before startup. It uses an allowlisted environment, ui_settings, fresh SQLite db.sqlite3 under system-temp financial-planner-ui-*, and isolated receipts/csv-staging/operator-audit storage. No .env loading. Email is in-memory; operator AI/Google settings are discarded. A fresh loopback fake Agent Harness and owned chat worker replace external models. Do not link providers or sync accounts. Inspect launcher/ui_settings and run python -m pytest tests/test_ui_instance.py -q -m "not browser" before sign-in.

## Start

One command from repo root, venv active:

```bash
python scripts/ui_instance.py
```

Keep running. Builds missing CSS, migrates and seeds, binds 127.0.0.1 on a fresh OS-allocated port, then prints JSON readiness containing url and directory plus disposable sign-in details. Directory is the instance handle. Logs go to stderr; keep separate from readiness. The fake provider has its own free loopback port. Never reuse an occupied port. TemporaryDirectory/finally cleanup removes storage and closes connections/server/fake/worker on failure. Install finally cleanup and maintain an owned process/port/directory/browser ledger before launching.

## Seed and sign-in

tests.mobile_seed.seed_phone_data loads synthetic_demo and adds current-date payroll/purchases, Shell Oil 5741 (-4118 USD minor units), Kroger, Corner Bistro, Dining 500.00, Groceries 600.00 and Transportation 250.00 budgets and unread Dining is over budget. Synthetic Alex belongs to the household and edits visible accounts. Synthetic policy is accepted. Sign in at /sign-in/ as synthetic_alex with Synthetic-passphrase-42!. Fake chat replies exactly: Synthetic UI reply. No real provider was contacted.

Reset: Teardown, wait for process exit and directory removal, then Start. Every mutating device/theme/orientation case and automated test gets a fresh instance. No concurrent writers on seeded rows. Resolve IDs from UI links. tests/fixtures/ui-pass/synthetic.csv is a generic synthetic shape, not any provider schema. Negative means outflow, positive income, explicit USD. The adjacent PNG is a synthetic pixel upload asset.

## Journeys

Headings below are readiness signals unless specified. Reload after every write, verify changed value/absence and matching audit event. /settings/audit/ shows IDs and changed field names, not financial values: match synthetic actor member ID, target ID, action and success. Correction history additionally identifies Synthetic Alex and old/new values. Scenario GET, preview and read markers have no financial correction history.

| ID | Path/readiness | Actions and reload/audit expectations |
| --- | --- | --- |
| AUTH | /sign-in/, Sign in | One invalid password gives generic error; seed sign-in reaches Cash flow. Fresh context deep-linked to Activity returns there after sign-in. Tab/Shift+Tab/Enter work. App Sign out revokes session; direct Activity redirects. Match sign-in/out audit where authorized. |
| HOME | /, Cash flow | Income, Spending, Net cash flow. Filters (Change range on phone): first day this month to today, Week, Update. Reload retains URL/selection. Reset range clears filters. Reversed dates validate without changing actuals. Read-only: no audit. |
| SCENARIO | /, What-if scenario summary | One-off amount on a date, Synthetic scenario expense, Expense, 10.00, next month first day. Add, reload same URL retains change/comparison and unchanged actuals. Remove clears it. Missing name/amount validates. Do not Make this real. URL-only state: no audit. |
| ACTIVITY | /transactions/, Transactions | Shell Oil 5741 Correct, Category Transportation, Save category. Reload edit and list retain category; history actor Synthetic Alex, Category Transportation. Cancel unsaved description preserves original. |
| CORRECT | Correct transaction | Description Synthetic corrected Shell, Save correction, reload field/history. Save note Synthetic UI note and Create tag synthetic-ui, reload note/tag and Tags changed audit; ordinary note edits deliberately have no audit (#98). Clear note and selected tag and reload absence. Keep -41.18 USD. |
| SPLIT | Correct transaction, Split | -20.00/-20.00 Transportation/Dining rejects mismatch. -20.00/-21.18 saves total -41.18. Reload parts/history. Unsplit with Transportation, reload removes parts and retains category/history. Match negative parent sign. |
| RECEIPT | Correct transaction, Receipts | Attach synthetic-receipt.png, reload filename, download image/png. Match receipt-added actor/transaction audit. Remove, reload absent, receipt-deleted audit. Unsupported synthetic text rejected. |
| BUDGET | /planning/budgets/, Budgets | Inspect three seeded budgets. Dining Actions/Edit, Monthly amount 550.00, Save, reload list/edit and budget change actor/target audit. Cancel unsaved edit preserves 550.00. Add unique category budget via dialog, reload, archive with confirmation, reload absence/audit. Zero validates. |
| ALERT | /alerts/, Alerts | Seed Dining link reaches Budgets. Mark as read, reload no Unread/Mark as read for row. Mark all read consistent. Read marker has no correction audit. |
| CHAT | /chat/, Chat | Send How much is my synthetic spending?, wait exact fake reply, reload question/reply. New conversation blank. Delete with confirmation and reload absence. Fake does not verify financial answers; no correction audit for text. |
| SETTINGS | /settings/security/, Settings | Inspect session. Visit categories, tags, alerts, data, AI, audit paths under /settings/; effective tab and fake loopback connection. Toggle Budgets near or over the limit, Save, reload, settings audit actor/changed fields. No provider linking/export/data deletion. |
| INVITE | /settings/household/, Household | Create invitation, confirm Password on /reauth/ when requested, then Create invitation again; hold code only in memory, navigate with GET back to Household then reload: no redisplay; invitation audit. Fresh /join/ context: code, synthetic_ui_guest, Synthetic UI Guest, strong synthetic password, accept policy. Eight recovery codes once, hold one only in memory. NEVER snapshot/log/trace/screenshot code pages. GET /join/ then reload hides codes; guest sign-in/member list persists. Invalid invitation creates nobody. |
| RECOVERY | /recover/, Recover account | Guest in-memory code, new strong synthetic password: updated status, prior guest session revoked, new password works, old password/reused code fail. No artifacts; clear code variables. Screenshot empty form or safe success only. Recovery actor/target audit. Seed Alex has no plaintext recovery code: no hash inspection/bypass. |
| CSV | Synthetic Checking Import CSV from /accounts/, Preview import | Generic profile upload synthetic.csv. Map Date/Description/Amount/Currency, YYYY-MM-DD, dot decimal, One signed amount column, no inversion. 2 new/0 duplicate/1 invalid, -12.34/+56.78 USD. Preview/reload does not change Activity. Cancel and delete upload returns Home; revisit import to verify upload form and removed stage. No commit/import audit in preview-only journey. |

CRUD applies only to entities named above. Current UI labels/source fields are authoritative. Record failed and untested branches individually; page visits alone do not complete flows.

## Theme and layout

Fresh contexts honor colorScheme; verify html data-theme. More (/more/) exposes Switch to dark/light theme; desktop account controls expose Dark theme with aria-pressed. App switch stores financial-planner-theme in localStorage. Check switch, discard contexts between themes. Do not edit sidebar (#345 in flight).

Desktop 1440x900; iPhone SE WebKit 375x667; iPhone 15 WebKit 393x852; Pixel 7 Chromium 412x915; iPad Mini WebKit 768x1024. Light/dark, phones portrait/landscape. Verify actual innerWidth, UA, touch and DPR. Retain device emulation during rotation. All primary pages: 320/360/375/390/414/600/768/820/1024/1280/1440/1920; short 1280x600, 1024x500; wide 2560. No root horizontal overflow. Check text, contained charts/tables, focus, 44x44 targets and 8px gaps.

Live resize: Budgets Add budget dialog, partly fill Monthly amount, choose category/scope and focus amount. 1440 -> 768 -> 375 -> 1440 without reload; retain input/options/focus and reachable actions, internal scrolling. Escape closes/restores trigger focus, Cancel creates nothing. Repeat Dining row Actions menu separately, no stale backdrop. Shrink phone height to 60% for simulated keyboard; check focused field and submit reachable, restore. Rotate partly filled form; Back subpages and fresh-session deep links. Root font size 200% is text approximation, not browser zoom; inspect fixed-pixel text separately and real 320px reflow independently.

PWA /static/manifest.webmanifest: valid start_url/scope/display/icons. No native installation automation; scoped standalone matchMedia override tests only a simulated branch. No service-worker registration exists in the current app: offline checking observes browser error/unavailable responses, not a cached offline app shell. Offline plus 503 routes, restore/discard. Chromium performance: 150ms latency, 1.6Mbps down/750Kbps up, 4x CPU; time to useful content and CLS through next interaction. Physical notch/safe-area, native keyboard/installation/OS require hardware and are not verified by emulation. No swipe/drag features in specified flows; tap/keyboard alternatives required.

## Playwright and CI

Python browser tests remain intact. Lock-in: Playwright Test 1.63.0, npm lockfile, playwright.config.ts and tests/ui/*.spec.ts. npm ci, install browsers, npm run test:ui. Each test starts a fresh documented instance with finally teardown, never reused server. UI_PYTHON selects venv interpreter. Only verified 127.0.0.1 readiness baseURL. Projects include desktop light/dark and real phone descriptors; workers bounded, mutations isolated.

Public repo: GitHub-hosted ubuntu-latest only, existing Playwright workflow; browser install, new tests, always cleanup via .venv/bin/python -m scripts.cleanup_ui_tests. Pytest capped at -n 4; full Python suite before push. Reports test-results/ and playwright-report/. No automatic screenshot/trace/video or failure DOM snapshot for code-bearing auth/recovery. Runtime invitation/recovery inputs use browser evaluate because Playwright fill embeds its value in HTML step titles. scripts/check_ui_report_privacy.py inspects the embedded archive in memory and gates uploads; it prints only counts, never matched codes. Sanitized explicit evidence output/ui-pass/financial-planner/YYYY-MM-DD, attach defects via gh. Never commit raw output/auth state/cookies/logs. No skips/retries to make known bugs or missing browsers pass. Known failures from the first pass: settings reflow #365, touch targets #366, phone input fonts #367, enlarged Home #368 Budgets #369, enlarged Settings #370 and covered phone Chat delete-all #371 and keyboard focus hidden by the dock #373; short-landscape invalid-range reset #375. AI/alert-settings are excluded from passing reflow assertions until #365 is fixed; their functional journeys are still covered.

## Teardown

One command, venv/root, exact readiness directory:

```bash
python scripts/ui_instance.py --teardown "<directory from readiness JSON>"
```

Validates system-temp prefix and sends authenticated loopback request, never arbitrary PID kill/deletion. Wait starter exit; verify directory gone and owned ports closed. Repeating after removal is already clean (currently FileNotFoundError): check absence before repeating. Playwright passes --startup-handle <registry path>: a token-free directory handle is written atomically before CSS/migrations/seeding. Teardown before readiness publishes cancellation; the launcher interrupts startup and runs finally cleanup. The fixture never kills the interpreter (Windows venv launchers may have a child process), retains failed-cleanup handles for CI recovery, and refuses to overwrite a previous handle. Idle browser preconnections have a 5-second socket timeout so server_close can finish. Forced kill recovery outside the fixture: verify owned PID/port stopped before removing exact resolved temp directory. Preserve evidence. Close only owned CLI sessions when another worker is present, never global close-all then. Clean on failure/interruption/success; reconcile ledger.
