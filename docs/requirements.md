# Requirements

Updated: 2026-09-30. This is a working product brief; open questions are explicit.

## Goal

Build a private, self hosted personal finance app that can eventually replace Rocket Money. The immediate value is transaction-level insight across checking accounts and credit cards, improving on the existing checking-account Google Sheet. The first milestone uses exported CSV files from Huntington Bank, Capital One, Apple Card, and Vanguard, subject to verifying each export format and transaction meaning. Cash flow over time is the lead view. Automatic bank connections are a later research decision.

## Confirmed decisions

- Repository: private GitHub repository with GitHub Issues as the backlog.
- First usable workflow: import bank and credit card CSV exports, then inspect the resulting transactions.
- Hosting target: basement PC, reachable from the home network and the existing Tailscale setup.
- The app will support multiple people, with both private data and an explicitly shared household view. All household members can edit shared accounts and transactions.
- The first milestone tracks actual transactions; goals and forecasts come later.
- Cash flow over time is the lead dashboard view, showing income, spending, and net cash flow.
- Plaid may be considered later for account connections; the user does not intend to pay for it now.
- The user chooses the date span represented by each CSV import; there is no fixed lookback period.
- Discard each uploaded CSV after a successful import. Retain batch, source-hash, and row-level provenance needed to trace, correct, and undo an import; do not log raw source rows.
- A refund reduces spending in its stored category rather than counting as income, using only the refund's own fields after it is linked.
- Preserve shared-account history when account scope or household membership changes, and revoke a person's access when they leave the household.
- Household lifecycle (issue #3, membership exit updated by issue #29): joining is invitation-based, and a person belongs to at most one household at a time. A current member may leave themselves; no member can remove another member in the application. Evicting someone is an operator management command on the basement PC. Membership roles are not part of the first release. Any current household member may share a private account with the household or unshare it back to private. When an account's scope changes, its full transaction history follows the new scope (fully visible once shared, owner-only once private again). Any current household member may delete/archive a shared account; deletion archives (soft-delete) rather than erasing transactions or import batches. If a shared account's owner leaves the household, ownership transfers to another current member; if they were the last member, the account becomes private to them instead.
- The MVP does not require balance history, investment-performance reporting, or portfolio composition. Balance and investment-performance history is a post-MVP goal; composition may follow later.
- Financial account numbers, statements, credentials, and real transaction data stay out of Git.
- Stack (issue #2, see docs/architecture.md): Python with Django, server-rendered pages, PostgreSQL, and Django's built-in migrations. Money is stored as integer minor units plus an explicit currency column. Deployment is Docker Desktop (WSL2) on the Windows basement PC, reached over Tailscale via `tailscale serve` for HTTPS. Backups are nightly `pg_dump` to a second local disk on the basement PC, keeping the 14 most recent nightly backups plus 8 weekly backups (owner decision, issue #11); the backup directory is configured by environment variable, not committed.
- Categories: a standard household-scoped preset list (all members see and use the same categories); custom rules are issue #16.
- Google Sheet: keep it as an ongoing comparison source alongside the app for a trial period rather than retiring or importing it immediately; revisit later.
- Deletion beyond source files: transactions and import batches are archived (soft-deleted), never hard-erased, matching the account-deletion decision in #3 — batch undo and transaction correction stay possible. Household-level data deletion (e.g. a person's full data on request) remains open and is not required for the first release.
- Schema conventions (issue #4): the stored sign is negative = money out, positive = money in, uniformly per account (credit-card/liability accounts use the same transaction sign; balance is a separate, later concern per #18). V1 account types are checking, savings, credit_card, and investment, each with an active/archived status; no renaming or provider-account-number-change handling beyond editing the account's name is needed for v1. Currency is USD only for v1, still stored explicitly per #2's money representation. Original imported fields are stored as a JSON blob per transaction (not a per-batch raw file), kept alongside the transaction rather than time-limited, and never written to logs. Source transaction id (when a provider supplies one) and a computed fingerprint (hash of account + date + amount + description) live on the transaction as intrinsic identity/dedupe fields for #6; transfer-linking between two transactions is a relationship added by categorization in #8, not part of import. Investment activity (Vanguard) is stored in v1 as a neutral transaction kind plus its opaque original fields, with no invented quantity/symbol/price breakdown, until Vanguard's real transaction types are verified (#1, #15).
- Financial accounts (issue #61): members add, rename, share, unshare, and archive accounts on `/accounts/`. Currency is USD and is not chosen at create. Creating an account goes to that account's CSV import page. The Import nav item goes to `/accounts/`.
- Sign-in and sessions (issue #12): onboarding is by a single-use invitation code, shown once to any current household member who creates one and valid for 48 hours; the first user is seeded via a management command. Eight one-time recovery codes are shown only at account creation; recovery consumes one code, changes the password, and revokes all existing sessions. There is no admin/CLI reset path. Sessions last 28 days from sign-in with no sliding idle timeout, and sign-out revokes the current session server-side. Password rules use Django's default validators. Five failed sign-in attempts for one username and network address within 15 minutes block that pair for 15 minutes. Identity uses Django username/password plus database-backed session cookies, and transport is HTTPS via `tailscale serve` as decided in #2/docs/architecture.md. These durations and thresholds are deployment-configurable without changing code.
- Generic CSV preview (issue #5): uploaded CSVs are staged in memory-backed storage (a tmpfs mounted at `/run/csv-staging` in the app container, set by `CSV_IMPORT_STAGING_DIR`), so they never reach disk. Each stage is bound to the uploader's authenticated session and selected visible account and expires after no more than one hour. Cancel deletes a staged file immediately; access and later staging activity remove expired files; a container restart discards all of them. Preview never creates import batches or transactions. Owner decision, 2026-09-28.
- Safe reimports (issue #6): overlap is detected per visible target account by fingerprint multiplicity against active transactions, so two identical same-day purchases in one export both import, and a second import of that export adds none. Preview shows new, duplicate, and invalid counts before commit. Commit writes only new rows (Vanguard-sourced batches use investment_activity; other sources use cash_flow), records batch provenance, and deletes the staged file even when every valid row was a duplicate. Undo archives that batch and its transactions without touching earlier imports. Matching never reads another account's ledger.
- Transaction review (issue #7): list active transactions newest first and show date, account, description, exact signed amount with currency, category state, import source/time, and current account scope. Date, account, category, and description-text filters must begin from the access-filtered transaction set. Authorized people may correct a transaction's date, description, and amount; its original imported fields, account, source row, fingerprint, and import batch remain unchanged. Category assignment, transfer exclusion, and refund linking are issue #8.
- Cash flow over time (issue #9): the signed-in home page is a server-rendered table of income, spending, and signed net (income minus spending) by period, plus Chart.js summary cards and a bar chart with a net line (issue #57), with no hosted charting service. Grouping is month (default), week starting Monday, quarter, or year. The default range is the last 12 full months plus the current month through today, and a period that is not yet complete is labeled partial. Totals come from `income_and_spending_totals` (transfers and card payments excluded only while both legs are visible, linked refunds reduce spending, unverified investment activity omitted). Account and private/household filters never include another person's private data. Each period links to the transaction list with the same dates and filters. A period is flagged "Missing import" when no active import batch for a selected visible account overlaps it; that flag is not a zero amount. With no visible transactions, the page prompts to import a CSV. Spending by category remains issue #10.

## First milestone: import to insight

1. Sign in as a person and add an account without storing bank credentials. Each account is private to one person or explicitly shared with a household.
2. Upload a CSV, map its columns, preview how dates, amounts, and descriptions will be interpreted, and see row-level errors before import.
3. Import valid transactions with an account and source file attached.
4. Reimport the same or overlapping export without double-counting transactions.
5. Review transactions, search and filter them, assign or correct categories, and mark transfers between owned accounts.
6. See cash flow over time as the primary view, with spending by category for a selected date range. Credit-card payments and internal transfers must not inflate spending.

## Quality and data handling

- Show the source and import time for each transaction so errors can be traced and corrected.
- Keep monetary values exact (for example, integer minor units or decimal types), with an explicit currency.
- Support an undo path for an erroneous import.
- Require authentication and per-person authorization before exposing personal financial data on the network.
- Document backup and restore before treating the app as the primary record.
- Use synthetic data in examples and development fixtures.

## Later candidates

- Saved mapping profiles and import rules for specific institutions.
- Recurring charge detection, subscription review, and alerts.
- Account balance, net worth, and investment-performance history (issue #18); portfolio/account composition may follow later.
- Savings goals, future cash flow, and scenario planning.
- Automatic bank/card aggregation after cost, coverage, privacy, and reliability are evaluated.
- Import or reconciliation with the existing Google Sheet.

## Open questions

- Verify CSV shapes for Huntington Bank, Capital One, Apple Card, and Vanguard using synthetic examples. Confirm how Vanguard investment activity should affect MVP cash flow; balance and investment-performance data sources and calculations can be settled in post-MVP issue #18.
- Which goal or forecast capability should follow transaction tracking?
- What custom categorization rule behavior does the user want beyond the starter category preset (issue #16)?
- Whether/when to retire the Google Sheet once the app is trusted as the comparison winner.
- Whether household-level data deletion (e.g. removing a person's data entirely) is needed, and if so, its rules.

## First milestone acceptance

Multiple people can sign in, keep private accounts private, and view and edit explicitly shared household accounts. A user can deploy the app on the basement PC with home-network and Tailscale access, import bank and card CSVs from several providers, correct mapping errors, reimport an overlap safely, review and categorize transactions, and view cash flow over time and category spending without internal transfers counted as expenses. Deployment instructions include authentication and backup/restore.

## Household access, transfer, and data-gathering decisions (2026-09-28)

Owner decisions. Where they differ from the earlier "Household lifecycle (issue #3)" bullet, these supersede it. Items marked *(proposed)* are the implementer's reading of a decision and need the owner's confirmation before the work that depends on them starts.

**Membership**
- A member can only leave a household themselves. No member can remove another member in the application. Evicting someone is an operator action: a management command run on the host by whoever administers the basement PC.

**Sharing an account.** Sharing offers two modes, chosen by the owner when sharing:
- *Co-owned* (fully shared): the account belongs to the household. Every current member may view and edit it, and it stays with the household if the owner leaves (ownership moves to another current member, as before).
- *Lent* (owner keeps it): household members may view and edit the account and its transactions while they are members, but the owner remains its owner. If the owner leaves the household, the account leaves with them and becomes private to them; the household loses access and history is preserved.
- If a non-owner member leaves, they simply lose access. If the last member leaves, shared accounts become private to them, as before.
- Both modes apply to the whole household. Sharing with one named person is not in scope; it can be added later if a household grows beyond a few people. *(proposed)*
- In lent mode only the owner may unshare, archive, or change the mode; borrowers may view and edit transactions. In co-owned mode any current member may unshare or archive, as already decided. *(proposed)*

**Uploaded CSV staging.** Staged uploads live in memory-backed storage (tmpfs) and never reach disk. They still expire within an hour and are deleted on cancel or on the next access after expiry; a restart discards them.

**Transaction edit history (issue #31).** Each correction of date, description, or amount appends one history row per changed field (actor, time, previous and new values). Amounts stay integer minor units plus currency. History is visible only through `visible_to` on the parent transaction, including shared and archived accounts. Original imported fields are not rewritten. Undo of a correction is out of scope. This is not part of the first release.

**Transfers and credit-card payments (issue #8)**
- Transfers between the person's own accounts are detected as pairs of transactions that cancel out (opposite signs, equal amounts, in two accounts the person can see, close in date), each with a confidence reading and the reasons behind it.
- A transaction is excluded from income and spending only when its counterpart is actually present. An unpaired transaction counts normally and may be shown as a possible transfer. A credit-card payment is excluded only when both sides are visible: the outflow from the paying account and the matching credit on the card account.
- Pairs above a high confidence threshold are marked as transfers automatically and listed for review with one-click undo; lower-confidence pairs are only suggested until confirmed. Thresholds and the date window are configurable.
- The scorer is a deterministic, explainable function that computes locally and shows the reasons behind each confidence reading. A learned or hosted scorer is deliberately out of scope for now; if one is ever considered it needs its own decision, because it would send financial data off the machine.
- Vanguard investment activity stays out of income and spending until its real transaction types are verified (issues #1 and #15). A contribution from a cash account to Vanguard counts as a transfer only when paired.
- Starter category list (household-scoped, editable): Income, Groceries, Dining, Transportation, Housing, Utilities, Health, Insurance, Shopping, Entertainment, Subscriptions, Travel, Education, Personal care, Gifts and donations, Fees and interest, Taxes, and Uncategorized. "Transfer" is a system category that is excluded from income and spending.

**Provider CSV shapes (issue #1).** The owner will run a local tool that reads real exports on their own machine and prints only column headers and masked value patterns, never values, and will provide that output. No real export is committed, attached, or pasted.

## Observed provider CSV shapes (2026-09-29)

Recorded from `scripts/csv_shape.py` output supplied by the owner. Only masked patterns and header labels were shared; no values. Importers must be built from these observations, not assumed formats.

### Huntington checking, native download (`Huntington_Delimited.csv`, 439 data rows)

- UTF-8, CRLF line endings, comma delimiter, header on line 1, no rows before the header, seven columns on every row.
- Columns in order: `Date`, `Reference Number`, `Payee Name`, `Memo`, `Amount`, `Category Name`, `Transaction Number`.
- `Date`: month first, `MM/DD/YYYY`, four-digit year.
- `Amount`: one signed column with a decimal point and no thousands separator or currency symbol; 282 of 439 values are negative. There are no separate debit and credit columns. Owner confirmed (2026-09-29): a negative amount is money leaving the account, a positive amount is money entering it.
- `Payee Name` is empty in 4 rows (121 distinct values); `Memo` is filled in every row (320 distinct values). Owner decision (2026-09-29): keep both. The importer stores both fields and builds the display description from them; the exact combination rule belongs in #13.
- `Reference Number` is all digits, mostly a single digit (434 of 439 rows), so it does not identify a transaction.
- `Transaction Number` is all digits, 18 digits in 394 rows and 10 digits in 45. All 439 values are distinct within this one export (checked locally, count only). It is a candidate provider transaction ID for reimport matching (#6). Stability across separate, overlapping exports is **still unverified**; until it is, reimport matching must not depend on it alone.
- `Category Name` is empty in every row, so this export carries no category.
- The file has no account identifier and no running balance; the account is chosen at import.

### Variant seen: a six-column file from another tool (`Transactions_29-09-2026.csv`)

Same 439 rows and 282 negative amounts as the native file, so the same transactions. Columns: `Date`, `Description`, `Category`, `Amount`, `Split`, `Tags`. LF endings, `M/D/YY` dates with no zero padding and no stated century, one signed `Amount`, 16 source-assigned categories (at least one with an HTML-escaped `&amp;`), `Split` and `Tags` empty, and no transaction ID. Its origin is unconfirmed. It is not the target for the Huntington importer unless the owner says otherwise.

### Owner decisions on the Huntington profile and on transfers (2026-09-29)

- Huntington description (#13): `Payee Name - Memo`, joined with " - ", with the payee dropped when it is empty. It feeds the reimport fingerprint, so the rule is fixed once shipped.
- Profile choice (#13): the user picks "Huntington" at upload; the profile is not auto-detected. Any other file uses the generic mapper.
- `Transaction Number` (#13) is stored as the source transaction id but is not used for reimport matching; the existing per-account fingerprint stays the matching rule. Cross-export stability may be tested later.
- Transfers (#8): pairs above a high confidence threshold are marked as transfers automatically and listed for review with one-click undo; lower-confidence pairs are only suggested until confirmed.
- Transfer match starting values (#8), all configurable: 5-day window between dates, exactly equal and opposite amounts, both accounts visible to the same person (including a shared household account). Changing the match window revalidates suggested and auto-marked pairs against the new window (auto-marked pairs now outside it are undone and their snapshot categories restored), leaves confirmed pairs in place, then refreshes pairing. Archiving or unsharing an account, or undoing an import batch (#42), revalidates pairs that have a leg in that account or batch even when the other member's private leg is not visible to the actor.
- Undoing a wrong exclusion (#8) restores the transaction to income or spending with its original category.
- Refunds (#8): a person links a refund to its original transaction manually. The refund must be positive and the original a negative purchase of the same kind. The refund stores the inherited category on itself. Totals treat a linked refund as negative spending in that stored category using only the refund's own fields, even if the original is no longer visible, and they must not reveal the original. Recategorizing the original copies the new category onto its linked refunds and records that change in correction history.

## Recurring charges decisions (2026-09-30, #17)

- Call a series recurring only after at least 3 occurrences at a regular interval. With 2 occurrences it may be shown only as "possible".
- Detect weekly, biweekly, monthly, quarterly, and annual cadences, each with a few days of date tolerance.
- Amounts may vary by up to 25% within a series, measured against the selected cadence chain's own median rather than the whole merchant cluster. Detection picks a cadence chain for a merchant first, then applies that 25% band. When the chosen chain fails the band, the member farthest from that chain's median is left out of this pick (not marked used) and the chain is picked again, repeating until a chain passes or fewer than two candidates remain; only then does detection fall back to clustering remaining charges by amount. Exact amounts get higher confidence than varying ones.
- Confirmed series appear on their own Recurring page, with monthly and annual totals, linked from the dashboard. Summary cards show those totals and the largest confirmed series is listed first (issue #57). A confirmed series stays matched only to suggestions in its own amount cluster (within 25% of that series' typical amount, not a pairwise median with a second cluster) and is never reassigned onto another cluster or given a colliding fingerprint. Refresh keeps a confirmed series active while at least one of its occurrences is still eligible; it deactivates the series (clears members, drops it from totals, keeps the confirmation) only when none remain. An eligible leftover occurrence is enough even when detection can no longer form a chain. A later eligible chain of the same merchant, cadence, and amount band can reactivate it.

## Spending by category decisions (2026-09-30, #10)

- The default range and presets match the cash flow view: the last 12 full months plus the current month to date. Presets are this month, last month, last 3 months, last 12 months, and year to date.
- A category whose refunds exceed its spending in the range shows a negative total, marked as a net refund, so totals reconcile exactly with the cash flow view.
- Rows are sorted by spending, largest first, with a percent-of-total column. Uncategorized is always listed.
- Category tiles and a donut chart of the same shares come first (issue #57); the totals table remains the accessible detail, with drilldown from tiles, slices, and rows. Per-category trends over time come later.
- Income is not part of this view.

## Adjusting transaction dates (2026-09-30)

- A person who can edit a transaction may change its date. For example, a bill that posts just after a month boundary can be moved into the month it belongs to, so month-to-month tracking stays consistent. This is an owner requirement: later work must keep it.
- The corrected date is the one every report uses (cash flow, spending by category, transfer pairing, recurring detection). The previous date is kept in correction history, and the provider's original value stays in the stored source fields.
- A date correction must never make a reimport count the transaction again. Reimport matching uses the fingerprint recorded at import, which a correction does not change. `tests/test_date_adjustment.py` guards both rules.

## Sign-in methods (2026-09-30)

Owner decisions:
- **Invitations are required.** Google proves who someone is, not that they belong in the household. Any new member joins with a valid invitation, whatever the sign-in method. The only exception is first-run setup, below.
- **Google and passwords both stay.** Google sign-in is added alongside password sign-in and is the preferred option in the interface. Password sign-in, recovery codes, and the existing session protections (absolute session expiry, login throttling) remain.
- **An account is created with whichever method a person uses first.** Google is offered first. A member with a password account can connect Google from their settings and then use either method.
- **First-run setup works in the browser, with either method.** While no user exists, a setup page creates the first member and household.

Defaults recorded with the decisions (owner may change):
- A Google identity is matched by Google's stable subject id (`sub`), never by email address, and only when Google reports the email as verified. An existing account is never linked automatically by email. Linking is an explicit "Connect Google" action by a signed-in member.
- A member cannot remove their last sign-in method. Disconnecting Google requires a password, and removing a password requires Google to be connected.
- Every new member gets one-time recovery codes, whatever the method. A Google-only member may add a password later.
- First-run setup requires a one-time setup code from `production.env` (`SETUP_CODE`). This stops anyone else who can reach the URL first from claiming ownership. The code stops working once the first member exists, and a database lock prevents two concurrent setups.
- Google sign-in is off unless `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are configured. With it off, the app behaves as it does today.
- Only the `openid email profile` scopes are requested. The app stores the Google subject id and email for display, never tokens.

## Interface styling decisions (2026-10-01)

Owner decisions:
- **Framework:** Tailwind CSS v4 with daisyUI 5 components. Pages stay server-rendered Django templates. No SPA.
- **Look:** bold and visual. Charts and color-coded summary cards come first, with tables as the detail behind them.
- **Theme:** light or dark following the device setting, plus a manual toggle that the browser remembers.
- **Devices:** desktop and iPhone matter equally. The layout is responsive, with a sidebar on desktop and a compact menu on the phone. Wide tables stay usable on a narrow screen.

Constraints carried from the existing requirements:
- **Self-hosted assets only.** No CDN, web fonts, or other third-party requests. The app is private and must work without reaching outside services.
- **No Node in the repository.** The CSS is built with Tailwind's standalone binary (pinned version, verified checksum), both in the Docker build and in a local script. JavaScript libraries (Chart.js, and Alpine.js if needed) are vendored at pinned versions with checksums.
- **Money is never shown by color alone.** In and out amounts keep their signs and labels, and colors meet contrast requirements in both themes.
- **Every chart has an accessible table** of the same numbers. Chart data comes from the page (`json_script`), never from a separate endpoint that could widen access.

## Visual dashboard decisions (2026-10-01, #57)

- Chart.js is vendored at a pinned release under `static/vendor/` with a recorded SHA-256. Pages load it from that static path only.
- Cash flow summary cards show income, spending, and net for the selected range, each with an up or down change against the previous equal-length range.
- Spending category colors are a stable index derived from the category id (or `uncategorized`), mapped onto daisyUI theme tokens. People do not assign colors.
- Chart colors are read from the active theme's CSS variables and update when the theme toggle changes.

## Deleting an account (2026-10-01)

Owner decision: a single **Delete account and all its data** action covers both retiring CSV history (for example, when switching to linked accounts) and removing data permanently. There is no separate bulk-undo or purge feature. Per-import undo stays for correcting a single bad import.

Defaults recorded with the decision (owner may change):
- Only the account's current owner may delete it, including a household-shared account. Other members can still archive or make it private under the existing rules.
- Deletion is permanent. It removes the account and every row that belongs to it: transactions, import batches, correction history, transfer pairs, refund links, and recurring-series memberships.
- Rows in other accounts that referenced the deleted data are repaired, not deleted:
  - A transfer partner returns to income or spending with its original category.
  - A refund linked to a deleted original keeps its stored category but loses the link. A deleted refund is removed from its original.
  - Recurring series are revalidated.
- The confirmation requires typing the account name. It shows how many transactions and imports will be removed, and states that existing backups keep a copy until they rotate out (14 nightly and 8 weekly).
- Logs and messages never contain the deleted data. A success message names only the account.

## Account connections and investment tracking (2026-10-01)

Owner decisions:
- **SimpleFIN Bridge** is the first automatic connection provider ($15/year), worth trying at its price. Plaid is not pursued: it is built for businesses, needs onboarding and public webhooks, and personal use is unclear. CSV import stays available for everything.
- **The SimpleFIN access URL is stored encrypted in the database.** The key lives in `production.env`. Each connection belongs to one member. The URL is never displayed or logged, and backups hold only ciphertext.
- **Linking uses a cut-over date.** When an existing account is linked, linked data starts at a cut-over date the member picks. The default is the day after the account's latest imported transaction. CSV history before the cut-over stays. Linked transactions before it are ignored, so nothing is counted twice. To drop CSV history entirely, delete the account (#62) and link a new one.
- **Sync:** automatic once a day, plus a **Sync now** button.
- **Investment accounts track balances only.** Vanguard and the Fidelity 401(k) (a new account, not in the original four) are tracked as dated balance snapshots for net worth over time. Their investment transactions are not imported. This matches how the owner uses Rocket Money today.
- **Apple Card:** aggregators cannot reach it, so it stays on CSV for now. A later option is a small iOS companion app, using Apple's FinanceKit through TestFlight, that sends Apple Card transactions to the server over the tailnet.
