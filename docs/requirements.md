# Requirements

Updated: 2026-09-25. This is a working product brief; open questions are explicit.

## Goal

Build a private, self hosted personal finance app that can eventually replace Rocket Money. The immediate value is transaction-level insight across checking accounts and credit cards, improving on the existing checking-account Google Sheet. The first milestone uses exported CSV files. Automatic bank connections are a later research decision.

## Confirmed decisions

- Repository: private GitHub repository with GitHub Issues as the backlog.
- First usable workflow: import bank and credit card CSV exports, then inspect the resulting transactions.
- Hosting target: basement PC, reachable from the home network.
- Financial account numbers, statements, credentials, and real transaction data stay out of Git.

## First milestone: import to insight

1. Add an account without storing account credentials.
2. Upload a CSV, map its columns, preview how dates, amounts, and descriptions will be interpreted, and see row-level errors before import.
3. Import valid transactions with an account and source file attached.
4. Reimport the same or overlapping export without double-counting transactions.
5. Review transactions, search and filter them, assign or correct categories, and mark transfers between owned accounts.
6. See spending by category and cash flow over time for a selected date range. Credit-card payments and internal transfers must not inflate spending.

## Quality and data handling

- Show the source and import time for each transaction so errors can be traced and corrected.
- Keep monetary values exact (for example, integer minor units or decimal types), with an explicit currency.
- Support an undo path for an erroneous import.
- Require access control before exposing personal financial data on the network.
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

- Which banks and cards should be supported first? Do their CSVs report debit/credit in one amount column or separate columns?
- Which view matters most immediately after import: spending by category, cash flow over time, or account balances?
- Is this single-user initially, or should separate users have separate data?
- Should the first milestone include goals and forecasts?
- Is home-network access sufficient, or should it work through the existing Tailscale setup?
- What category scheme and custom category/rule behavior does the user want?
- What period of historical data should be brought in initially?
- Should the existing Google Sheet be imported later, remain a comparison source, or be retired?
- What retention and deletion controls are wanted for uploaded CSV files and imported rows?

## First milestone acceptance

A user can deploy the app on the basement PC, import at least one bank CSV and one card CSV, correct any mapping errors, reimport an overlap safely, review and categorize transactions, and view category spending and cash flow without internal transfers counted as expenses. Deployment instructions include authentication and backup/restore.
