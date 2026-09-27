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
- Household lifecycle (issue #3): joining is invitation-based, and a person belongs to at most one household at a time. Any current household member may share a private account with the household or unshare it back to private. When an account's scope changes, its full transaction history follows the new scope (fully visible once shared, owner-only once private again). Any current household member may delete/archive a shared account; deletion archives (soft-delete) rather than erasing transactions or import batches. If a shared account's owner leaves the household, ownership transfers to another current member; if they were the last member, the account becomes private to them instead.
- The MVP does not require balance history, investment-performance reporting, or portfolio composition. Balance and investment-performance history is a post-MVP goal; composition may follow later.
- Financial account numbers, statements, credentials, and real transaction data stay out of Git.
- Stack (issue #2, see docs/architecture.md): Python with Django, server-rendered pages, PostgreSQL, and Django's built-in migrations. Money is stored as integer minor units plus an explicit currency column. Deployment is Docker Desktop (WSL2) on the Windows basement PC, reached over Tailscale via `tailscale serve` for HTTPS. Backups are nightly `pg_dump` to a second local disk on the basement PC (schedule/retention finalized in issue #11).
- Categories: a standard household-scoped preset list (all members see and use the same categories); custom rules are issue #16.
- Google Sheet: keep it as an ongoing comparison source alongside the app for a trial period rather than retiring or importing it immediately; revisit later.
- Deletion beyond source files: transactions and import batches are archived (soft-deleted), never hard-erased, matching the account-deletion decision in #3 — batch undo and transaction correction stay possible. Household-level data deletion (e.g. a person's full data on request) remains open and is not required for the first release.
- Schema conventions (issue #4): the stored sign is negative = money out, positive = money in, uniformly per account (credit-card/liability accounts use the same transaction sign; balance is a separate, later concern per #18). V1 account types are checking, savings, credit_card, and investment, each with an active/archived status; no renaming or provider-account-number-change handling beyond editing the account's name is needed for v1. Currency is USD only for v1, still stored explicitly per #2's money representation. Original imported fields are stored as a JSON blob per transaction (not a per-batch raw file), kept alongside the transaction rather than time-limited, and never written to logs. Source transaction id (when a provider supplies one) and a computed fingerprint (hash of account + date + amount + description) live on the transaction as intrinsic identity/dedupe fields for #6; transfer-linking between two transactions is a relationship added by categorization in #8, not part of import. Investment activity (Vanguard) is stored in v1 as a neutral transaction kind plus its opaque original fields, with no invented quantity/symbol/price breakdown, until Vanguard's real transaction types are verified (#1, #15).
- Sign-in and sessions (issue #12): onboarding is any-current-household-member invites (the first user is seeded via a management command); recovery is one-time recovery codes shown at account creation, not admin/CLI reset; sessions are long-lived (weeks) with no idle timeout, and sign-out revokes the session server-side; password rules use Django's default validators plus basic login rate-limiting. Identity mechanism (Django username/password + session cookies) and transport (HTTPS via `tailscale serve`) were already decided in #2/docs/architecture.md.

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
