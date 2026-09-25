# Requirements

Updated: 2026-09-25. This is a working product brief; open questions are explicit.

## Goal

Build a private, self hosted personal finance app that can eventually replace Rocket Money. The immediate value is transaction-level insight across checking accounts and credit cards, improving on the existing checking-account Google Sheet. The first milestone uses exported bank and credit card CSV files from several providers. Cash flow over time is the lead view. Automatic bank connections are a later research decision.

## Confirmed decisions

- Repository: private GitHub repository with GitHub Issues as the backlog.
- First usable workflow: import bank and credit card CSV exports, then inspect the resulting transactions.
- Hosting target: basement PC, reachable from the home network.
- The app will support multiple people. The data-sharing model (private, household, or both) is still open.
- The first milestone tracks actual transactions; goals and forecasts come later.
- Cash flow over time is the lead dashboard view.
- Financial account numbers, statements, credentials, and real transaction data stay out of Git.

## First milestone: import to insight

1. Sign in as a person and add an account without storing bank credentials. Access to each person's data follows the agreed sharing model.
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
- Account balance and net worth history.
- Savings goals, future cash flow, and scenario planning.
- Automatic bank/card aggregation after cost, coverage, privacy, and reliability are evaluated.
- Import or reconciliation with the existing Google Sheet.

## Open questions

- Which banks and card issuers should be supported first? Do their CSVs report debit/credit in one amount column or separate columns?
- Should cash flow chart net movement, separate income and expenses, or both?
- Should people have private data, a shared household view, or both?
- Which goal or forecast capability should follow transaction tracking?
- Is home-network access sufficient, or should it work through the existing Tailscale setup?
- What category scheme and custom category/rule behavior does the user want?
- What period of historical data should be brought in initially?
- Should the existing Google Sheet be imported later, remain a comparison source, or be retired?
- What retention and deletion controls are wanted for uploaded CSV files and imported rows?

## First milestone acceptance

Multiple people can sign in under the chosen sharing model. A user can deploy the app on the basement PC, import bank and card CSVs from several providers, correct mapping errors, reimport an overlap safely, review and categorize transactions, and view cash flow over time and category spending without internal transfers counted as expenses. Deployment instructions include authentication and backup/restore.
