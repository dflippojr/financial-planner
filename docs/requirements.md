# Requirements

Updated: 2026-09-25. This is a working product brief; open questions are explicit.

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
- A refund reduces spending in its original category rather than counting as income.
- Preserve shared-account history when account scope or household membership changes, and revoke a person's access when they leave the household.
- Household lifecycle (issue #3): joining is invitation-based, and a person belongs to at most one household at a time. Any current member may leave or remove another current member; membership roles are not part of the first release. Any current household member may share a private account with the household or unshare it back to private. When an account's scope changes, its full transaction history follows the new scope (fully visible once shared, owner-only once private again). Any current household member may delete/archive a shared account; deletion archives (soft-delete) rather than erasing transactions or import batches. If a shared account's owner leaves the household, ownership transfers to another current member; if they were the last member, the account becomes private to them instead.
- The MVP does not require balance history, investment-performance reporting, or portfolio composition. Balance and investment-performance history is a post-MVP goal; composition may follow later.
- Financial account numbers, statements, credentials, and real transaction data stay out of Git.
- Stack (issue #2, see docs/architecture.md): Python with Django, server-rendered pages, PostgreSQL, and Django's built-in migrations. Money is stored as integer minor units plus an explicit currency column. Deployment is Docker Desktop (WSL2) on the Windows basement PC, reached over Tailscale via `tailscale serve` for HTTPS. Backups are nightly `pg_dump` to a second local disk on the basement PC, keeping the 14 most recent nightly backups plus 8 weekly backups (owner decision, issue #11); the backup directory is configured by environment variable, not committed.
- Categories: a standard household-scoped preset list (all members see and use the same categories); custom rules are issue #16.
- Google Sheet: keep it as an ongoing comparison source alongside the app for a trial period rather than retiring or importing it immediately; revisit later.
- Deletion beyond source files: transactions and import batches are archived (soft-deleted), never hard-erased, matching the account-deletion decision in #3 — batch undo and transaction correction stay possible. Household-level data deletion (e.g. a person's full data on request) remains open and is not required for the first release.
- Schema conventions (issue #4): the stored sign is negative = money out, positive = money in, uniformly per account (credit-card/liability accounts use the same transaction sign; balance is a separate, later concern per #18). V1 account types are checking, savings, credit_card, and investment, each with an active/archived status; no renaming or provider-account-number-change handling beyond editing the account's name is needed for v1. Currency is USD only for v1, still stored explicitly per #2's money representation. Original imported fields are stored as a JSON blob per transaction (not a per-batch raw file), kept alongside the transaction rather than time-limited, and never written to logs. Source transaction id (when a provider supplies one) and a computed fingerprint (hash of account + date + amount + description) live on the transaction as intrinsic identity/dedupe fields for #6; transfer-linking between two transactions is a relationship added by categorization in #8, not part of import. Investment activity (Vanguard) is stored in v1 as a neutral transaction kind plus its opaque original fields, with no invented quantity/symbol/price breakdown, until Vanguard's real transaction types are verified (#1, #15).
- Sign-in and sessions (issue #12): onboarding is by a single-use invitation code, shown once to any current household member who creates one and valid for 48 hours; the first user is seeded via a management command. Eight one-time recovery codes are shown only at account creation; recovery consumes one code, changes the password, and revokes all existing sessions. There is no admin/CLI reset path. Sessions last 28 days from sign-in with no sliding idle timeout, and sign-out revokes the current session server-side. Password rules use Django's default validators. Five failed sign-in attempts for one username and network address within 15 minutes block that pair for 15 minutes. Identity uses Django username/password plus database-backed session cookies, and transport is HTTPS via `tailscale serve` as decided in #2/docs/architecture.md. These durations and thresholds are deployment-configurable without changing code.
- Generic CSV preview (issue #5): uploaded CSVs are staged in memory-backed storage (a tmpfs mounted at `/run/csv-staging` in the app container, set by `CSV_IMPORT_STAGING_DIR`), so they never reach disk. Each stage is bound to the uploader's authenticated session and selected visible account and expires after no more than one hour. Cancel deletes a staged file immediately; access and later staging activity remove expired files; a container restart discards all of them. Preview never creates import batches or transactions. Owner decision, 2026-09-28.
- Safe reimports (issue #6): overlap is detected per visible target account by fingerprint multiplicity against active transactions, so two identical same-day purchases in one export both import, and a second import of that export adds none. Preview shows new, duplicate, and invalid counts before commit. Commit writes only new rows (Vanguard-sourced batches use investment_activity; other sources use cash_flow), records batch provenance, and deletes the staged file even when every valid row was a duplicate. Undo archives that batch and its transactions without touching earlier imports. Matching never reads another account's ledger.
- Transaction review (issue #7): list active transactions newest first and show date, account, description, exact signed amount with currency, category state, import source/time, and current account scope. Date, account, category, and description-text filters must begin from the access-filtered transaction set. Authorized people may correct a transaction's date, description, and amount; its original imported fields, account, source row, fingerprint, and import batch remain unchanged. Until issue #8 defines the category scheme, every transaction is shown as `Uncategorized` and that is the only concrete category filter value; issue #7 does not invent category storage.

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

**Transaction edit history.** Recording who changed which correction and when is wanted but is not part of the first release; it is tracked as a separate enhancement.

**Transfers and credit-card payments (issue #8)**
- Transfers between the person's own accounts are detected as pairs of transactions that cancel out (opposite signs, equal amounts, in two accounts the person can see, close in date), each with a confidence reading and the reasons behind it.
- A transaction is excluded from income and spending only when its counterpart is actually present. An unpaired transaction counts normally and may be shown as a possible transfer. A credit-card payment is excluded only when both sides are visible: the outflow from the paying account and the matching credit on the card account.
- Pairs above a high confidence threshold are marked as transfers automatically and listed for review with one-click undo; lower-confidence pairs are only suggested until confirmed. Thresholds and the date window are configurable. *(proposed)*
- The scorer is a deterministic, explainable function that computes locally and shows the reasons behind each confidence reading. A learned or hosted scorer is deliberately out of scope for now; if one is ever considered it needs its own decision, because it would send financial data off the machine.
- Vanguard investment activity stays out of income and spending until its real transaction types are verified (issues #1 and #15). A contribution from a cash account to Vanguard counts as a transfer only when paired.
- Starter category list (household-scoped, editable): Income, Groceries, Dining, Transportation, Housing, Utilities, Health, Insurance, Shopping, Entertainment, Subscriptions, Travel, Education, Personal care, Gifts and donations, Fees and interest, Taxes, and Uncategorized. "Transfer" is a system category that is excluded from income and spending.

**Provider CSV shapes (issue #1).** The owner will run a local tool that reads real exports on their own machine and prints only column headers and masked value patterns, never values, and will provide that output. No real export is committed, attached, or pasted.
