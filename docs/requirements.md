# Requirements

Updated: 2026-10-04. This is a working product brief; open questions are explicit.

## Goal

Build a private, self hosted personal finance app that can eventually replace Rocket Money. The immediate value is transaction-level insight across checking accounts and credit cards, improving on the existing checking-account Google Sheet. The first milestone uses exported CSV files from Huntington Bank, Capital One, Apple Card, and Vanguard, subject to verifying each export format and transaction meaning. Cash flow over time is the lead view. Automatic bank connections are a later research decision.

## Confirmed decisions

- Review runner hardening (#303): keep fork workflow approval at all external contributors. Install a host-side job-started allow-list for this repository's `review.yml` on the default branch (`main`), accepting only base-repository `pull_request_target` and `workflow_dispatch` jobs. Prefer one-job ephemeral registration under a dedicated non-administrator Windows account. The owner applies host changes and reprovisions after each job; implementation must not restart or re-register the live runner. See [runner operations](review-runner.md).

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
- Refund purchase selection (#242): only positive, unlinked transactions outside excluded transfers show the refund search. A plain GET searches visible active outflows of the same kind by description and optional exact purchase amount or same account, returning at most 25 newest purchases. Split choices load only for the selected purchase.
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
- Import throughput (#246): serialize overlapping CSV/OFX commits with the target account row lock, then run the incremental transfer matcher once after that lock is released. Resolve ordered confirmed rules, suggestion jobs, alert audiences and settings by batch rather than by row; preserve rule precedence, amount/account filters, reversible category history, 40-row AI jobs, and alert deduplication. Budget alert reports share reads per member and scope. Query-count tests cover 500 and 5,000 rows; performance measurements use only disposable PostgreSQL databases and synthetic data.
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

- CSV spreadsheet safety (#305): every export prefixes text starting with `=`, `+`, `-`, `@`, TAB or CR with an apostrophe. Signed numeric money columns retain their numeric values; member-export JSON preserves the original text. The ZIP README explains this escaping.
- Show the source and import time for each transaction so errors can be traced and corrected.
- Keep monetary values exact (for example, integer minor units or decimal types), with an explicit currency.
- Support an undo path for an erroneous import.
- Require authentication and per-person authorization before exposing personal financial data on the network.
- Document backup and restore before treating the app as the primary record.
- Use synthetic data in examples and development fixtures.

## Later candidates

Only ideas that have not shipped. Saved mapping profiles (#101), recurring detection, net worth and investment performance (#18), savings goals and scenarios (#77, #151), SimpleFIN aggregation (#67) and the Google Sheet comparison (#154) are built and recorded above.

- Automatic bank/card aggregation beyond SimpleFIN, such as Plaid, after cost, coverage, privacy, and reliability are evaluated (#20; findings in docs/research/account-connections.md). It must not become a paid dependency for now.
- Portfolio and account composition for investments.
- Sign in with ChatGPT as an AI connection (parked, #115).

## Open questions

Resolved questions are kept in their decision sections; the first milestone's CSV shapes are in [csv-shapes.md](csv-shapes.md).

- Whether/when to retire the Google Sheet once the app is trusted as the comparison winner (the comparison itself shipped in #154; the retirement call is the owner's).
- Which further goal or forecast capability should follow the shipped planning features.
- Further categorization rule behavior beyond the shipped rules (#16), if the owner wants any.

Settled: deleting a member's data entirely is decided in [Deleting a member's data](#deleting-a-members-data-2026-10-03-104).

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
- Transfer match starting values (#8), all configurable: 5-day window between dates, exactly equal and opposite amounts, both accounts visible to the same person (including a shared household account). Changing the match window revalidates suggested and auto-marked pairs against the new window (auto-marked pairs now outside it are undone and their snapshot categories restored), leaves confirmed pairs in place, then refreshes pairing among rows the member saving it can see; each member's own refresh pairs rows in their private accounts. Archiving or unsharing an account, or undoing an import batch (#42), revalidates pairs that have a leg in that account or batch even when the other member's private leg is not visible to the actor.
- Incremental transfer matching (#244, owner approved 2026-10-06): edits revisit old and new amount/date candidates; imports and sync match only changed rows, once per batch/pass, after releasing import account locks. Empty imports and unchanged syncs do no transfer scoring. Candidate queries use exact opposite amounts, the configured date window, different accounts, and `visible_to()`. Existing scoring, settled pair behavior, and ordered membership/account/transaction locks stay in force. Full rebuilds are explicit maintenance operations.
- Undoing a wrong exclusion (#8) restores the transaction to income or spending with its original category.
- Refunds (#8): a person links a refund to its original transaction manually. The refund must be positive and the original a negative purchase of the same kind. The refund stores the inherited category on itself. Totals treat a linked refund as negative spending in that stored category using only the refund's own fields, even if the original is no longer visible, and they must not reveal the original. Recategorizing the original copies the new category onto its linked refunds and records that change in correction history.

### Capital One credit card (`*_transaction_download.csv`, 173 data rows, recorded 2026-10-02)

- UTF-8, LF line endings, comma delimiter, header on line 1, no rows before the header, seven columns on every row.
- Columns in order: `Transaction Date`, `Posted Date`, `Card No.`, `Description`, `Category`, `Debit`, `Credit`.
- Both dates are ISO `YYYY-MM-DD`.
- Amounts are two unsigned columns with a decimal point and no thousands separator or currency symbol. Each row fills exactly one of them: `Debit` in 158 rows, `Credit` in 15. No value is negative.
- `Debit` is a charge (money out). `Credit` is a payment or refund (money in). 13 of the 15 credits are categorized `Payment/Credit`; the other 2 carry a spending category, consistent with refunds.
- `Card No.` is four digits (the card's last four). `Description` is always filled (53 distinct values).
- `Category` has 9 source-assigned values: Dining; Entertainment; Gas/Automotive; Health Care; Merchandise; Other; Other Services; Other Travel; Payment/Credit.
- No transaction ID, no running balance, no account identifier.

### Apple Card (`Apple Card Transactions <start> - <end>.csv`, 375 data rows, recorded 2026-10-02)

- UTF-8, LF line endings, comma delimiter, header on line 1, no rows before the header, eight columns on every row. One file can cover a long range (this one spans about 21 months).
- Columns in order: `Transaction Date`, `Clearing Date`, `Description`, `Merchant`, `Category`, `Type`, `Amount (USD)`, `Purchased By`.
- Both dates are month first, `MM/DD/YYYY`, four-digit year.
- `Amount (USD)` is one signed column with a decimal point and no thousands separator or currency symbol. Positive is a charge (money out). Negative is a payment or credit (money in): 17 rows.
- `Type` has 4 values: `Purchase` (356 rows), `Payment` (15), `Credit` (2), `Debit` (2). The 17 negative amounts are exactly the 15 `Payment` and 2 `Credit` rows. `Debit` rows are positive charges that are not purchases (for example interest, a fee, or an adjustment).
- `Description` is the long raw text (always filled, 221 distinct). `Merchant` is the short name (always filled, 195 distinct).
- `Category` has 16 source-assigned values. `Purchased By` has one value in this file (the cardholder; Apple Card Family can add more).
- No transaction ID, no running balance, no account identifier.

### Owner decisions on the card profiles (2026-10-02)

- **Date:** a card transaction is dated by its `Transaction Date` (the purchase day). `Posted Date` and `Clearing Date` are kept only in the original fields.
- **Signs:** the stored sign stays negative = money out, positive = money in. Capital One stores `-Debit` or `+Credit`. Apple Card stores `-Amount (USD)`, so a purchase is negative and a payment or credit is positive. Payments then pair with the checking withdrawal through transfer matching as card payments (#8).
- **Apple Card description:** `Merchant`. The raw `Description` is kept in the original fields. The description feeds the reimport fingerprint, so the rule is fixed once shipped. Capital One uses its single `Description` column.
- **Provider categories:** stored in the original fields only, never applied. The app's own categories and rules (#16) decide categorization. A provider-to-app category mapping may come later.
- **Stored columns:** `Card No.` is dropped and not kept in the original fields. `Purchased By` is kept in the original fields.
- **Profile choice:** as for Huntington, the user picks "Capital One" or "Apple Card" at upload. Profiles are not auto-detected.
- **Vanguard:** no CSV importer. Investment accounts track balances only (2026-10-01 decision), and statement balances are entered as manual snapshots. This supersedes #15.

## Recurring charges decisions (2026-09-30, #17)

- Call a series recurring only after at least 3 occurrences at a regular interval. With 2 occurrences it may be shown only as "possible".
- Detect weekly, biweekly, monthly, quarterly, and annual cadences, each with a few days of date tolerance.
- Amounts may vary by up to 25% within a series, measured against the selected cadence chain's own median rather than the whole merchant cluster. Detection picks a cadence chain for a merchant first, then applies that 25% band. When the chosen chain fails the band, the member farthest from that chain's median is left out of this pick (not marked used) and the chain is picked again, repeating until a chain passes or fewer than two candidates remain; only then does detection fall back to clustering remaining charges by amount. Exact amounts get higher confidence than varying ones.
- Confirmed series appear on their own Recurring page, with monthly and annual totals, linked from the dashboard. Summary cards show those totals and the largest confirmed series is listed first (issue #57). A confirmed series stays matched only to suggestions in its own amount cluster (within 25% of that series' typical amount, not a pairwise median with a second cluster) and is never reassigned onto another cluster or given a colliding fingerprint. Refresh keeps a confirmed series active while at least one of its occurrences is still eligible; it deactivates the series (clears members, drops it from totals, keeps the confirmation) only when none remain. An eligible leftover occurrence is enough even when detection can no longer form a chain. A later eligible chain of the same merchant, cadence, and amount band can reactivate it.

## Recurring series grouping (2026-10-03, #124)

These rules replace the whole-chain median band above. Prices drift with inflation, and a fixed 25% band around one median splits a single bill into several series over time.

- **The 25% band is measured between neighbouring charges.** Each charge after the first must be within 25% of the chain's current level: the median of the up to two accepted charges before it, in date order.
  - **Confirmed price change.** A charge outside that band still belongs to the chain when the next charge is within 25% of it *and* outside the band of the old level. The level then resets to the new price.
    - A charge that returns to the old price confirms nothing.
    - The latest out-of-band charge, with no next charge yet, is treated as an outlier until a later charge confirms it.
  - **Outliers.** A lone out-of-band charge that the next charge doesn't confirm is an outlier. Outlier removal and the amount-clustering fallback (#50) apply as before.
  - **Result.**
    - Gradual drift stays in one series. So do single step changes once confirmed: $20 then $27, $27; or $100, $120, $144, $144.
    - Two concurrent plans at clearly different prices from one merchant stay separate, even when they are less than 50% apart. For example, $100 and $130 interleaved: each $100 that follows a $130 is back in the old band, so it never confirms the $130.
- A series' typical amount is its recent price: the median of its latest three occurrences, or of all of them if there are fewer. Monthly and annual totals, matching a confirmed series to new suggestions, the projection, and export all use it.
- Members correct grouping on the Recurring page, on their own series, whether confirmed or suggested:
  - **Merge** one series into another, even across merchant keys. The target keeps its cadence and name, and becomes confirmed if either series was confirmed. The source series is deleted.
  - **Remove** a charge from a series. The charge is then excluded from recurring detection for that person until it is added back by hand.
  - **Add** an eligible charge that is not in another active series.
- Manual edits survive refresh. Members are marked as detected or manual. Refresh adds detected chains to a series and never drops manual members. A detected chain that shares any charge with an existing active series attaches to that series. Manual members follow the same eligibility and revalidation rules as detected ones, including account deletion and sharing changes.
- Export includes each member's source and the person's excluded charges.

## Manual recurring series creation (2026-10-09, #264)

- **Create series** on the Recurring page lets a member build a series from past charges when detection found none (for example a merchant above the 600-charge cap or one whose name collides after normalisation). Flow: search eligible charges (a capped page at a time, on demand), enter a name and an existing cadence, preview, then confirm. It works without JavaScript.
- **Minimum history: 2 past charges** (owner decision). Detection still needs 3; a member choosing the charges by hand is itself a strong signal, so manual creation covers the rest. One charge is rejected.
- **The member may override the detector.** Irregular dates or amounts are labelled in the preview and in the series reasons, never blocked. The series reason says it was created manually and its confidence is always low, never high-confidence detection.
- The series is confirmed, belongs to the acting person, and all members are manual, so refresh preserves them. Selected charges must be visible, active, negative cash flow, not excluded as transfers or card payments, one currency, and not claimed by another active non-dismissed series. POST revalidates under the household lock and creates the whole series or nothing, with a generic denial for anything inaccessible. Identical membership cannot be created twice.
- New charges do not join automatically beyond the existing confirmed-series matching and manual-member preservation; there is no new matcher.

## Spending by category decisions (2026-09-30, #10)

- The default range and presets match the cash flow view: the last 12 full months plus the current month to date. Presets are this month, last month, last 3 months, last 12 months, and year to date.
- A category whose refunds exceed its spending in the range shows a negative total, marked as a net refund, so totals reconcile exactly with the cash flow view.
- Rows are sorted by spending, largest first, with a percent-of-total column. Uncategorized is always listed.
- Category tiles and a donut chart of the same shares come first (issue #57); the totals table remains the accessible detail, with drilldown from tiles, slices, and rows. Per-category trends over time are issue #100.
- Income is not part of this view.

## Spending by category over time (2026-10-02, #100)

- A Trends tab on the spending-by-category page uses the same range presets, account and private/household filters, and default range as the overview. Grouping matches cash flow: month by default, with week, quarter, and year available.
- Each period's per-category totals come from `spending_by_category_report`, so they reconcile with the overview and with cash flow spending for that period, including refunds and excluded transfers.
- The stacked bar chart shows the top 8 categories by spending in the selected range plus an Other group. The accessible table lists every category, including Uncategorized. Chart data is on the page (`json_script`).
- Category tiles and table names link to `/spending/category/<id>/` (or `uncategorized`). That page shows the category's trend for the selected grouping, its average per period, its change against the previous equal-length range, and a link to the transaction list.
- Colors use the existing stable category color mapping.

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

## Installable home-screen app (2026-10-02, #103)

Defaults recorded so backlog work can continue (the owner may change them):

- There is **no service worker in v1**, so the browser does not cache HTML or financial data on the device. Web push for alerts (#99) would add a service worker under its own decision.
- The web app manifest name is **Financial Planner**, short name **Finances**, `display: standalone`.
- Icons are an original rising-bar SVG in the repository, with committed PNG sizes 180 (Apple touch icon), 192, and 512. No third-party artwork or CDN.
- Theme-color meta tags follow daisyUI light and dark base backgrounds (`#ffffff` / `#1d232a`); the manifest `theme_color` is daisyUI light primary (`#422ad5`).
- Compact phone layout uses `viewport-fit=cover` and `env(safe-area-inset-*)` padding for the notch and home indicator.
- Adding the app to an iPhone home screen, password and Google sign-in in standalone mode, and safe-area spacing are an owner device check after merge, not an automated test.

Constraints carried from the existing requirements:
- **Self-hosted assets only.** No CDN, web fonts, or other third-party requests. The app is private and must work without reaching outside services.
- **No Node in the repository.** The CSS is built with Tailwind's standalone binary (pinned version, verified checksum), both in the Docker build and in a local script. JavaScript libraries (Chart.js, and Alpine.js if needed) are vendored at pinned versions with checksums.
- **Money is never shown by color alone.** In and out amounts keep their signs and labels, and colors meet contrast requirements in both themes.
- **Every chart has an accessible table** of the same numbers. Chart data comes from the page (`json_script`), never from a separate endpoint that could widen access.

## Settings navigation (2026-10-03, #111)

Owner decisions:
- The footer **Account** link is **Settings** (gear icon from #112). Setup and administration live on Settings tabs, each with its own URL that works without JavaScript: Sign-in & security (`/settings/security/`), Connections (`/settings/connections/`), Household (`/settings/household/`), Categories (`/settings/categories/`), Data (`/settings/data/`), and AI (`/settings/ai/`).
- The main menu is day-to-day views: Cash flow, Net worth, Spending, Transactions, Transfers, Recurring, Accounts, Import, Planned items, Budgets, Goals. Import has its own page (#108). Budgets stays in the main menu as a day-to-day view (the issue's enumerated list omitted it).
- Old URLs permanently redirect to the matching Settings tab, including category-rule and SimpleFIN sub-pages. Access checks and re-authentication are unchanged.

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
- **Net worth v1 (#69).** The Net worth page is a monthly series of last-on-or-before `BalanceSnapshot` values (SimpleFIN and manual). Checking, savings, and investment balances are assets; credit-card balances are liabilities and subtract. Members who can edit an account may record, edit, and delete manual snapshots; a manual row never overwrites SimpleFIN. Carry-forward months are flagged. Each viewer sees their private accounts plus household accounts, with a household-only filter; another member's private accounts never appear. Returns, holdings, and projections stay in #18 and #21.
- **Physical assets and loans (#109).** Net worth also includes manually valued *Real estate*, *Vehicle*, *Precious metals*, and *Other asset* accounts, plus *Loan* liabilities (mortgage, auto loan). Asset values are estimates with an optional note, using the same last-on-or-before snapshot rule. They have no transactions and do not affect cash flow, spending, recurring, transfers, or categorization. A loan may pair with an asset of the same scope to show equity without changing totals. Precious metals are a manual total value only.
- **Apple Card:** aggregators cannot reach it, so it stays on CSV for now. A later option is a small iOS companion app, using Apple's FinanceKit through TestFlight, that sends Apple Card transactions to the server over the tailnet.

## Export, rules, and sharing-mode decisions (2026-10-01)

**Data export (#19).**
- One zip download holds CSV and JSON files for each entity: accounts, categories, transactions, import batches, and their provenance.
- A member exports their own private accounts plus household accounts, never another member's private accounts.
- The Google Sheet stays a comparison source. There is no Sheet import now.

**Filtered transaction CSV (#263).** Transactions offers a POST download of all active parent rows matching the validated current filters, in list order and across every page. It requires recent authentication and returns to the same validated filters for resubmission. The UTF-8 CSV preserves exact signed minor and decimal amounts and currency, visible categories/tags/splits, notes, import provenance, and the existing exclusion flag. User-controlled text cells starting with `=`, `+`, `-`, `@`, tab, or carriage return get a single-quote prefix; numeric money remains numeric. Tags and splits are JSON arrays. Hidden category names are blank, and private records, original fields, fingerprints, receipts, and relationship identifiers are omitted. Rows stream in bounded chunks with no staging files; downloads are not cached and record only the existing content-free export security event.

**Categorization rules (#16).**
- A rule matches a case-insensitive "description contains" text. It can optionally be narrowed to one account and a minimum and maximum amount. Its only action is setting a category. There is no regex, and a rule never marks transfers.
- **Personal and household rules:**
  - A personal rule belongs to one member and evaluates transactions in accounts that member can access.
  - A household rule belongs to the household. Any current member can edit it, and it evaluates only household-shared accounts.
- **Precedence:**
  - Personal rules are tried before household rules. Within each group, an explicit priority order applies and the first match wins.
  - A rule never overwrites a category set by hand.
- **Applying and reversing:**
  - A new rule first previews the matches. It applies to existing transactions only after the member confirms.
  - After that it applies automatically to new imports and SimpleFIN syncs.
  - Each application can be reversed, which restores every affected transaction's previous category. Disabling a rule stops future application.
- Saved CSV column mappings are a separate follow-up, after the other providers' shapes are known.

**Sharing modes (#30): confirmed as proposed.**
- Sharing is with the whole household only.
- **Lent:** only the owner may unshare, archive, or change the mode. Other members may view and edit transactions.
- **Co-owned:** any current member may unshare or archive.
- The owner may switch modes either way. Switching from lent to co-owned shows a confirmation that the owner is giving up sole ownership: if they leave, the account stays with the household. Switching from co-owned to lent is allowed only for the account's current owner.
- Existing household accounts migrate as co-owned. The Accounts page (#61) offers Co-owned or Lent when sharing or creating a household account, and the owner can switch later.

## Re-authentication for sensitive actions (2026-10-01)

Owner decision: sensitive actions require a fresh confirmation of identity. One confirmation covers further sensitive actions for **10 minutes** in the same session.

Sensitive actions:
- **Household access:** inviting a member, leaving the household.
- **Sign-in methods:** connecting or disconnecting Google, adding or removing a password.
- **Sharing:** sharing an account, making it private, changing co-owned or lent.
- **Destructive or data-exporting:** deleting an account, downloading the data export.
- **Connections:** connecting or disconnecting SimpleFIN.

Defaults recorded with the decision (owner may change):
- **Ways to re-authenticate:**
  - A member with a password re-enters it.
  - A member with Google confirms with a fresh Google round trip through the account chooser (`prompt=select_account`). Google sends `auth_time` only to published, verified apps, so a household app in testing mode cannot prove a fresh Google password entry. Owner decision (2026-10-01): accept the round trip for every member with Google, checking that the Google account matches and the ID token was just issued (`iat`). This is weaker than a password: someone at an unlocked browser that is still signed in to Google can pass it. If Google ever sends `auth_time`, the app checks that instead.
  - A member with both may use either.
  - Recovery codes are not accepted here.
- **Where the timestamp lives:** the session, never a cookie the client can set. Signing out clears it.
- **Failed attempts** count toward the existing login throttle.
- **Flow:** a sensitive POST without a fresh confirmation is not performed. The member is sent to a re-authentication page, then back to the page they came from, and submits the action again. Requests are never replayed automatically.

## Planning v1: projected cash flow and savings goals (2026-10-01)

Owner decision: build a projected cash-flow view **and** savings goals. #68 (Apple Card iOS app) stays research only for now. Saved CSV column mappings wait until the other providers' export shapes are known.

Projected cash flow:
- Answers "will expected income cover recurring bills and planned expenses over the next N months?"
- **Inputs:**
  - Confirmed recurring series (#17) become projected expenses automatically, at their cadence and typical amount.
  - Members add planned items: recurring or one-time income or expense, with a name, amount, start date, optional end date, cadence (weekly, biweekly, monthly, quarterly, or annual), and an optional category. Each item is private or household-shared, following the account visibility rules.
- **Horizon:** 12 months by default, with 3, 6, 12, and 24 available, grouped by month. The projection starts after the current month's actuals.
- **Display:** projected months continue the cash-flow chart in a dashed, hatched style labeled **Projected**. They are never added into actual totals. There is no uncertainty modeling or scenarios in v1.

Savings goals:
- A goal has a name, target amount, target date, an optional linked account, and private or household visibility.
- **Progress** is informational. It is the linked account's latest balance (#67/#69 balance snapshots) or a manually entered current amount, compared against the target. It shows the monthly amount needed to reach the target by the target date.
- Goals do not change the projection in v1.

## Wishlist import and funding plan (2026-10-09, #295)

Owner decision (the issue's defaults apply; recorded here and on the issue before building):
- **Goal fields.** A goal gains an optional `priority` (whole number, 1 funded first), an optional `depends_on` goal ("buy after"), a `time_sensitive` flag, and its `target_date` becomes optional (a wishlist item may have no deadline). Existing goals are unchanged.
- **Dependencies stay in one scope.** A private goal depends only on the owner's own private goals; a household goal only on household goals of the same household. This keeps a shared plan free of anything private. A dependency cannot be the goal itself or form a loop. A goal other goals depend on cannot move to a scope they do not match.
- **Import.** Planning offers an import of a small CSV or JSON file with columns `name`, `target_amount` (decimal dollars, at most two decimals, converted straight to minor units), `priority` (required), and optional `depends_on` (another row's name in the same file and scope), `time_sensitive`, `target_date` (YYYY-MM-DD) and `scope` (`private` default, or `household`). The file is staged, previewed row by row, and only applied after confirmation; the apply step re-checks everything and is all-or-nothing. A file with any row error imports nothing.
- **Re-import.** Rows match saved goals by name, case-insensitive, within the importer's own private goals or their household's goals. A match is updated, never duplicated (an active goal wins over an archived one of the same name; an archived goal that is the only match is updated and stays archived), and a file that changes nothing writes nothing and records no audit event. A column left out of the file keeps each saved value; a blank cell in an included column clears it. Goals missing from the file are listed in the preview and never deleted or archived. Unknown dependency names get one generic message that does not reveal whether another member holds a goal of that name.
- **Funding plan.** A read-only page, separate from the projection, which it never changes. It takes the projection's monthly surplus (projected income minus projected expenses), sets aside a one-time safety buffer, then fills open goals in order: priority first (goals with no priority after all ranked ones), a goal never before the goal it depends on, then goal id. Allocation is sequential: the whole surplus goes to the first unfunded goal, and any leftover in a month continues into the next goal the same month. Each goal starts from its current progress (linked account balance or manual amount). A month with a negative surplus funds nothing and adds nothing to the buffer; progress is never undone. Completed and archived goals are left out and no longer hold back a dependent.
- **Safety buffer.** The default is one month of average actual spending over the last 3 complete calendar months (integer division), for the same accounts the plan uses. A household can override it with any amount from 0 up, from the plan page. The buffer is set aside once, from the first positive surplus, before any goal is funded.
- **Horizon and output.** The projection's own horizon (3, 6, 12 or 24 months; 12 default). For each goal the page shows the projected month it is fully funded, "Already funded", or "Not within horizon". With a target date, the goal is on time when funded in or before the target month; a time-sensitive goal that would miss its target date is flagged. A target date after the horizon with an unfunded goal is shown as beyond the horizon, not as a miss. The page lists the horizon, buffer and fill order as assumptions.
- **Plan scopes.** "Household goals" uses household goals, household accounts and household planned items only, ignores a household goal's link to a private account, and so comes out the same for every member. "My private goals" uses the member's private goals, accounts and items. "Everything I can see" combines both for that member. A member never sees another member's private goals or their effect on a shared plan. Known gap (#297): a transfer pair between a household and a private account is excluded from totals only for a viewer who sees both legs, so the shared plan's surplus and default buffer can differ slightly in that case, as every other total does today.
- No AI and no external calls; plain integer arithmetic. Out of scope: Notion sync, purchase price tracking, investment-return assumptions, scenarios.

## Investment performance (2026-10-02, #18)

Owner decisions:
- **Measure:** growth versus contributions. Investment transactions are still not imported (balances only). The member enters each statement's ending balance and, optionally, that period's **net contributions** (contributions minus withdrawals). The app then separates market growth from money put in.
- **Cadence:** from each statement, monthly or quarterly. A "statement entry" is a manual balance snapshot with net contributions recorded (zero is a valid value). Performance periods run between consecutive statement entries. Other snapshots, such as daily SimpleFIN balances, still drive Net worth but do not split performance periods.

Calculation (recorded defaults; the owner may change them):
- For consecutive statement entries with start value `V0`, end value `V1`, and net contributions `C` recorded on the later entry: **growth** = `V1 - V0 - C`, and the **period return** = `growth / (V0 + C/2)`. This is the Modified Dietz method, which assumes contributions arrive mid-period. A period with `V0 + C/2 <= 0` has no return.
- **YTD, 1-year and all-time** link the period returns: `product(1 + r) - 1` over periods whose end date falls in the range. Each range also shows total contributions and total growth in dollars. A range starts at the latest statement entry on or before its start date. Without one, it starts at the earliest entry inside the range and is labelled partial.
- A range with any period that lacks a return shows value change only, plus a note. Accounts with fewer than two statement entries show value change only.
- Every return is labelled an **estimate**, with the method named. Amounts are exact minor units; returns are percentages rounded to one decimal.

## AI features (2026-10-02, #90)

Owner decisions:
- **Backends are chosen per member.** Each member picks from the AI backends they have connected in their own settings: one for live chat and one for background jobs (#91). A request uses only the requesting member's own connection. For now it is acceptable that only the hosting member can connect one.
- **The first backends come through Agent Harness.** The hosting member's own Claude, Codex, or Cursor subscription, or the tower's local model. This is intended personal use: the member asks their own subscription about their own data, through the harness's unmodified CLI in non-interactive mode. It never serves other people through that member's subscription. The app talks to the harness only through its documented HTTP App API. Sign in with ChatGPT is research only (#92). No paid API key is required.
- **The goal is a read-only bot** that answers a member's questions about their data (#94), plus background helpers: category suggestions (#93) and a monthly review (#95). Full agent sessions are not needed. The transfer scorer stays deterministic (#8).
- **Live and background work are separate.** Loading the tower's local model is slow and takes GPU and RAM from other work. Background features run as stored jobs and never wake the local model just because a job is queued: they run when it is already loaded, or in a configured quiet window. Chat defaults to a hosted backend. Chat on the local model warms it on the member's first keystroke and shows its loading state.
- **Provider-side use of data is accepted.** Minimizing fields and controlling provider retention are not goals for a member's own data. Nothing is sent until a member connects a backend, so each member, and each other install of this public project, chooses for themselves.
- **Household-shared data** may be sent to a member's AI backend only while every current household member is in acceptance of the privacy and data policy (#106). Other members' private data is never sent.
- **Tools.** The model reaches data only through the app's read-only tools. Each tool runs as the requesting member against `visible_to` and may return anything that member can already see in the app. Tools never return secrets, whatever the provider: credentials, the SimpleFIN access URL, AI connection tokens, recovery codes, and session material. Tool output, model output, and errors never contain data the member cannot see.
- **Logging.** The app records provider, backend, feature, member, time, token counts, and outcome. It never records prompts or responses, and error messages never contain raw model text.
- **Output.**
  - AI output is labeled as AI-generated, with the backend named.
  - Suggestions never overwrite a category set by hand or by a rule, and never change transfer or refund semantics.
  - Category suggestions (#93) run as background jobs after import and SimpleFIN sync, or from Suggest categories on the Uncategorized filter. The model must answer with an existing category id or "unsure". Stored suggestions expire when the transaction changes. After several accepted suggestions share a description-contains string, the app offers the existing categorization-rule preview.
  - Answers are not presented as verified facts or as financial advice.
  - Figures shown come from the app's tools and link to the page that shows them.
- **Re-authentication.** Connecting, changing, or disconnecting an AI backend is a sensitive action under the 10-minute re-authentication rule.
- **Agent Harness work this depends on.**
  - [agent-harness #329](https://github.com/dflippojr/agent-harness/issues/329): sessions limited to the app's own tools.
  - [agent-harness #300](https://github.com/dflippojr/agent-harness/issues/300): app tools reachable from hosted Claude Code.
  - Wanted but not blocking: [agent-harness #330](https://github.com/dflippojr/agent-harness/issues/330), isolated per-app data in the harness.

## Privacy and data policy (2026-10-02, #106)

Owner decisions:

- Every member is asked to accept a privacy and data policy that discloses how data is stored and that a member who connects an outside AI provider may send household-shared data visible to them to that provider, once every current member is in acceptance. Other members' private data is never sent. It covers where data is stored, SimpleFIN, Google sign-in, sending data to outside AI providers (including household-shared data), what other members can see, export, and deletion.
- The default text lives in `finance/policy/default.md` for the owner to edit in the PR. Operators replace it without changing code via `PRIVACY_POLICY_PATH` and `python manage.py publish_privacy_policy` (`--material` when members must accept again). Versions are kept so the text someone accepted can be shown later.
- A member is in acceptance when they have accepted the latest material version, or any later version. A non-material version never takes anyone out of acceptance.
- Setup, join, and Google sign-up present the policy but do not block. Existing members see a prompt until they respond. Without acceptance the app works; AI backends do not. While any current household member is not in acceptance, no member's AI backend may receive household-shared data.
- Acceptance rows are per member and version and are included in that member's export. The policy page is public.
- Accepting records the version that was shown. If that version is no longer current, no acceptance is recorded: the policy page and account settings re-show the current text, and setup, join, and Google sign-up still finish so the persistent prompt can ask again. Historical versions do not offer Accept.
- Only a version an operator marks as material requires members to accept again.

AI refinements (owner, 2026-10-02, #91 and #94):
- **Harness address.** A member may connect only an Agent Harness on the same host or on the tailnet. HTTPS is required except on loopback.
- **Connect errors (#212).** A URL copied with a trailing `/api/v1` is accepted and stored without it. Connect names the actual problem: a token the harness rejects (401/403, create one in that harness's Settings → Apps), a URL that isn't a harness API (404), or a harness the app can't reach (inside Docker, `localhost` is the app's own container). Malformed URLs and badly pasted tokens get a message, never a server error.
- **Error logs (#212).** Unexpected server errors write a traceback to the container's stdout. Logs never carry transaction details, raw import rows or secrets; the log formatter also strips App tokens and URL credentials that an exception message might quote.
- **Chat history.** Conversations are kept only for the member who started them, can be deleted, and expire after 30 days (configurable). They are included in that member's export.
- **Advice.** The chat bot answers factual and explanatory questions. Its suggestions are labeled as opinion and never presented as financial advice.
- **Chat UI.** A Chat page, plus a drawer on every page that continues the same conversation. The drawer passes only the current route and its query parameters, never page data.
- **Chat runs in the background (#218).** Sending stores the question and a pending reply and returns at once; no web request waits on Agent Harness. A chat lane in the `background` container, separate from the batch job loop (no quiet window, no batch backoff), picks the turn up within about a second and answers app-tool calls as the member who asked, with that member's visibility. Turns in one conversation run one at a time. The page shows "Thinking…" and a static script polls for the reply (first check at 0.5 s, then every second; the harness session is polled at 0.25 s, 0.5 s, then every 0.6 s, #245); without JavaScript it says to refresh. A turn whose runner stops sending heartbeats, or that sits unclaimed for a full session timeout while no runner is alive, ends as failed with the usual unavailable message.
- **Local model in chat.** Offered only after a synthetic-data evaluation shows reliable tool calling.

## Sign in with ChatGPT as an AI backend (2026-10-02, #92)

Research is in `docs/research/sign-in-with-chatgpt.md` (sources checked 2026-10-02).

**Owner decision: pursue access.** The research found that a household web connect button doesn't fit OpenAI's self-serve flow today:
- callbacks go only to `http://127.0.0.1`;
- the Sign in with ChatGPT Terms, as quoted in the research, say tokens must stay local and under the user's control, and one person's plan must not serve another person's requests.

The owner wants to find out what access is possible rather than wait. Two routes:
- **The hosting member, self-serve (to prototype).** OpenAI documents a self-hosted procedure: complete sign-in on a machine with a browser through the `127.0.0.1` callback, then move the credentials to a server the same user controls. Here the server is the hosting member's own basement PC. Signing in from a browser on that PC lets the loopback callback reach it directly. The connection is the hosting member's only, which matches #91's "only the hosting member for now". Whether storing that member's token encrypted in this app's database counts as "local and under the user's control" is unverified, and the prototype must record the reading it relies on.
- **Other members: no documented route today.** OpenAI's interest form is for commercial integrations. Open-source developers are directed to the self-serve docs, whose flow needs a `127.0.0.1` callback on the member's own device. OpenAI's documented credential transfer assumes a server that only that member controls, which a household server is not. Revisit when OpenAI documents a self-serve pattern for small multi-user self-hosted apps.
- **Also available now:** the hosting member's ChatGPT plan already works through Agent Harness's `codex` backend (#91), without Sign in with ChatGPT.

**Owner decision (2026-10-03, #115): parked.** The hosting-member loopback prototype is not being built. That member's ChatGPT plan already works through the harness `codex` backend. Revisit if OpenAI documents a self-serve sign-in pattern for small multi-user self-hosted apps.

**Proposed, not yet decided:** Sign in with ChatGPT is an AI connection only, not a sign-in method next to Google. Invitations still apply.

**Data use:** plan-usage Responses must use `store: false`. Whether request bodies are used for training was not established. #106 applies before household-shared data is sent.

## Budgets (2026-10-02, #96)

Owner decisions:
- **Scope.** Budgets are private or household.
  - A household budget counts only household-shared accounts.
  - A private budget counts the owner's visible accounts (private and household).
  - No member's private spending ever appears in a household total.
  - Any current member may edit a household budget. Only the owner may edit a private one.
- **What a budget is.** A monthly amount for one category, plus an optional overall monthly total.
- **Actuals.**
  - Spending comes from the same totals as spending by category (#10): transfers excluded, linked refunds reduce spending, investment activity omitted.
  - Months use the corrected transaction date.
  - Budget totals reconcile exactly with that view.
- **Amount changes** apply from the chosen month onward. Past months keep their amounts.
- **Rollover** is optional per budget, and carries in both directions: unspent adds to the next month, and overspend subtracts from it.
  - The balance starts in the month rollover is turned on.
  - A member who can edit the budget may reset it to zero. Each reset records who reset it and when.
- **Projection.** Budgets do not feed the projected cash flow (#21) in v1.

## Split transactions (2026-10-02, #97)

Owner decisions:
- **Splitting.** A transaction can be split into two or more parts, each with a category. The parts must add up exactly to the transaction amount in minor units, and every part has the transaction's sign.
- **Transfers and refunds.**
  - A transfer leg or a linked refund cannot be split. It must be unpaired or unlinked first.
  - A refund linked to a split purchase is attached to one part chosen by the member, and inherits that part's category.
  - Splitting a purchase that already has linked refunds asks which part each refund belongs to.
- **Categorization rules** skip split transactions: a split counts as categorized by hand.
- **Recurring detection** uses the whole transaction.
- **History.** Splitting and unsplitting are recorded in correction history.
- **Reports.** Spending by category, trends, and budgets count each part in its own category. Cash flow totals are unchanged, because the parts add up to the transaction.

## Notes and tags (2026-10-03, #98)

Owner decisions:
- **Notes.** A transaction can have one free-text note. Notes follow the transaction's visibility, so anyone who can see a household transaction sees its note. Editing a note is not recorded in correction history.
- **Tags** are household-scoped, like categories. A transaction can have many.
- **Filtering.** A tag filter is available on the transaction list, spending by category, and cash flow, so a trip total is one filter away.
- **Reimports and export.** Notes and tags are never changed by a reimport, and both are included in the export.

## Alerts (2026-10-03, #99)

Owner decisions:
- **Delivery in v1** is an in-app inbox only: an unread count in the nav and an Alerts page. No outside service.
- **Alerts in v1:**
  - a SimpleFIN sync failed, or a connection needs re-linking;
  - a recurring charge's price changed or an expected charge is missing (raised by #102);
  - a budget reached 90% or went over;
  - a transaction was above a threshold the member sets.
- **Who gets an alert.** One about a household account goes to every current member. One about a private account goes only to its owner. A member who leaves no longer sees household alerts.

## Saved CSV mappings (2026-10-03, #101)

Owner decisions:
- **Scope.** Saved mappings are household-wide: any current member can use and edit them, like categories.
- **Locked after the first import.** Once a mapping has imported a batch, none of its parsing fields can change: date, amount, sign, and description columns and formats. They all feed the reimport fingerprint (account, date, amount, description). Only the name and account default stay editable. A member makes a new mapping instead.

## Recurring review (2026-10-03, #102)

- Merge selection (#254): load the target selector only for the series the member chooses to merge. Keep the existing member-authorized merge action and exclude the source, inactive, dismissed, cancelled, and inaccessible series from its targets.

Owner decisions:
- **Price changes.** A change is flagged when the latest charge differs from the series' recent typical amount (#124) by 10% or more.
- **Cancelling.** A member can mark a series cancelled. It leaves totals and the projection, and its history stays. If a new matching charge arrives, it shows as "resumed?" for the member to confirm.
- **Step changes** are covered by #124: a confirmed series follows step changes and gradual drift.

## Monthly review (2026-10-03, #95)

Owner decisions:
- **Facts first.** A plain monthly review of computed facts ships first and works with AI off. AI phrasing follows as a later step.
- **Alert.** Each month's review also arrives as an inbox alert (#99): "Your September review is ready".
- **AI phrasing (#141).**
  - A short paragraph of three to five plain sentences sits above the facts, labeled AI-generated with the backend named.
  - Every number must match a computed fact. A paragraph stating any other number is discarded, and the facts show alone.
  - It is on by default for members who have connected an AI backend and are in acceptance of the privacy policy. Each member can turn it off.

## Deleting a member's data (2026-10-03, #104)

Owner decision: a member can delete their own data, self-service from settings.
- **Before deleting:**
  - It needs re-authentication and typing a confirmation.
  - The member is offered an export first.
- **What is deleted:** the member's private accounts and personal records.
- **What stays:**
  - Shared household data stays.
  - The member appears as "former member" in shared history.
- **Lent accounts.** For each household account the member has lent, they choose:
  - hand it over to the household, so it becomes co-owned and stays; or
  - delete it with their data.

  Deletion can't proceed until each lent account has a choice.

## Next round (2026-10-04)

Owner decisions:
- **Queue:** the owner chose all of these:
  - bulk edit (#148), better search and saved filters (#149), a year-end report (#150), and receipts (#146);
  - what-if scenarios (#151), a debt payoff planner (#152), a bills calendar (#153), and a Google Sheet comparison (#154);
  - backup health and an off-site copy (#155), passkeys (#156), a sign-in log and sessions (#157), and Dependabot (#158);
  - local-model chat (#159), AI for other members (#162 and #147), chat proposals (#160), and unusual spending insights (#161).
- **Year-end report:** a print-friendly page plus CSV downloads. No generated PDF.
- **Off-site backups:** each verified dump is encrypted with `age` and copied with rclone to a remote the operator configures. The age private key stays off the tower.
- **Second factor:** passkeys (WebAuthn), optional per member, after password sign-in. Recovery codes stay the fallback. Google sign-in is not challenged.
- **AI for other members:**
  - Other members may use the tower's local model through the hosting member's harness connection when the host offers it (#162). Hosted backends on that connection stay usable only by the host.
  - Any member may bring their own Anthropic or OpenAI API key (#147). It is optional and paid by them; no paid key is ever required.
  - Any member may link their own Claude or Codex plan through a sign-in popup (#237). The login lives in Agent Harness as an end-user login; the app sends only an opaque HMAC id of the Person, a pasted code is forwarded once and never stored, and a linked plan wins over that member's API key for the same backend. Linking goes through the member's own harness connection or one the host offers for plan linking. A link through another member's connection is used only while both are current members of the same household and that member still offers plan linking; ending either membership removes it.
  - Per-user subscription logins wait on Agent Harness support ([agent-harness #365](https://github.com/dflippojr/agent-harness/issues/365)).


## Stack memory (2026-10-06, #249)

Owner-approved: preload the app in Gunicorn before its two gthread workers fork, keeping four threads and the 660-second timeout. One background container and one Django process run the daily SimpleFIN pass, batch AI jobs and interactive chat on independent threads. Keep restart on failure and the container Up check. Measure app and background memory limits with headroom; PostgreSQL remains uncapped. Request-spike root causes belong to the other performance issues.

## Background AI concurrency (2026-10-06, #247)

Owner-approved: the batch lane in the consolidated background runner polls every two seconds and runs at most three jobs concurrently, independently of the four chat workers and the daily pass. Fetch only enough due job IDs to fill free workers; use indexed due/stale lookups. Each job keeps its atomic claim, saved harness session, local-model quiet window, retry back-off and stale recovery rules. Independent category chunks and review/insight jobs do not share a harness session and can run concurrently for the same member. Use the shared harness completion poll from #245. Keep the background container capped at 512 MiB.

## General audit trail (2026-10-08, #270)

Owner-approved: a metadata-only, append-only database trail, reviewed by members in Settings and by a read-only CLI using the same audience rules. Own private events and currently authorized household/object events only; inaccessible surviving targets hide both rows and counts. Deleted shared account metadata is visible only to current members of its recorded household; deleted private account metadata only to its owner. Account deletion retains only the deletion event, without names, values or financial data. Member deletion removes private events and anonymizes retained shared actor/effective-member references.

Retention is configurable, 90 days by default, with a bounded daily purge. Actors come from authenticated members or trusted scheduler/operator code, never request headers or submitted names. Normal database actions and events share a transaction. An audit insert failure rolls back its savepoint while allowing the action, with a visible generic warning and a redacted gap report; a succeeded side effect is never retried. Application paths cannot edit/delete events except documented retention/deletion/anonymization cleanup. Checksums detect accidental changes cheaply; the machine/database owner remains trusted. Backups include the trail and restoring an older dump rewinds it; no independent journal or historical backfill.

### Audit coverage of access, lifecycle and sensitive settings (2026-10-08, #272)

Owner-approved: the trail from #270 also records account create/rename/ownership/debt-terms/loan-pairing changes, household invitations, first-member setup and leaving, sign-in method and session changes, privacy choices, SimpleFIN and AI connection changes, host local-model and plan-link offers, and notification and bill-calendar settings. Household invitations/membership and the host's sharing setting names go to current household members; credentials, connections, notifications and sign-in methods stay with the acting member. Only successful transitions are recorded; denied or no-op requests and unauthenticated input leave no event. Existing security events stay the personal security UI contract. Operator eviction is a follow-up. See docs/audit-trail.md for the coverage table.

## Financial workflow audit coverage (2026-10-08, #273)

Owner-approved decisions: audit all listed workflow actions through the #270 service (imports and import undo, manual entries, balances, budgets, planned items and goals, categories, tags, saved CSV mappings, rules, recurring series, receipts, existing corrections, bulk edits and confirmed chat changes); tag membership changes record a `tags_changed` event without names; ordinary note edits get no event, preserving #98; a completed import with no new rows records one distinct event; every download, including receipts, the data ZIP, year-end CSVs and the filtered transactions CSV, records that a response was prepared, without claiming delivery. Events hold IDs, enum values, changed field names and bounded counts only. Direct, bulk, chat-confirmation and automatic-rule sources are distinguishable, and an undo names the undoing member. Existing correction histories and import provenance stay the source of values. The action matrix lives in docs/audit-trail.md.

## Scheduled, delegated and operator audit coverage (2026-10-08, #274)

Owner-approved: distinguish authenticated UI members, scheduler work and host operators; retain the effective member whose access or connection is used. Operators may declare a validated app-member ID, labeled as a declaration rather than authenticated identity; otherwise they are unattributed. Do not infer OS identity or trust client headers. Correlate nested domain events with their trusted run, and record consequential sync, AI attempt/final, chat, review, alert and email outcomes without polling, health-check or credential-read events. Link AI usage by ID; never copy model text or credentials. Personal outcomes stay personal and domain events keep their existing audience.

Operator-wide command, daily maintenance, backup/off-site/restore/check summaries use a protected metadata-only append journal outside database volumes, with daily rotation, 90-day retention and read-only CLI review. Global summaries contain operation/outcome only, with actor kind, source, run UUID, timestamp and checksum; no private targets or cross-member counts. The machine owner remains trusted. Journal failures visibly warn and report the gap while allowing home-app work to proceed; never repeat a successful side effect for an audit write. Restores can rewind the database trail and the journal if its directory is restored; independence from restores is not required. See docs/audit-trail.md for deployment permissions, incomplete runs and review commands.

## Value bounds and provider hardening (2026-10-09, #308)

Recorded defaults for #308 (P1 hardening). The owner may change them.
- **Activity dates:** transaction and balance dates must be on or after 1900-01-01 and no more than 366 days past today. CSV and OFX previews give an out-of-range row a row error. The edit form and manual entry reject such a date. SimpleFIN rows with an unusable or out-of-range posted time are skipped. `python manage.py check_activity_dates` lists stored rows outside the window by ID only. Cadence stepping that would pass the end of the calendar yields no next date instead of failing.
- **Balance entries:** a recorded balance or statement net contribution is limited to plus or minus $1,000,000,000,000. A return beyond plus or minus 1,000,000% shows as "Over 1,000,000%" or "Below -1,000,000%". A return that cannot be computed shows no percentage. Existing extreme rows still render and can be deleted.
- **SimpleFIN payloads:** amounts must fit a signed 64-bit count of cents, with no fraction of a cent. Ids must fit 255 characters. Text is stored as valid UTF-8 without NULs, and descriptions are cut to 1,000 characters. Unstorable amounts and ids fail the sync with a safe message. An unexpected failure on one connection is recorded as a failed sync and the daily pass continues. The alert pass always runs.
- **SimpleFIN requests:** a claim response is capped at 8 KiB and must finish within 60 seconds. An account fetch is capped at 25 MiB and 180 seconds. Every connection resolves the host once and connects only when every answer is a public internet address. Redirects and environment proxies are not used. The outbound request runs before the household lock is taken. The sync then locks, re-checks the connection and its links, and applies the results.
