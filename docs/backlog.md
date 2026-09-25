# Backlog and milestones

GitHub Issues are the work queue. This file records the delivery order and scope so the issues remain coherent.

## Milestone 0: decide the first supported workflow

- Confirm export formats, account scope, first dashboard view, and retention expectations.
- Choose a small stack and storage format suited to self hosting and exact monetary values.
- Define a synthetic example for each supported CSV shape.

## Milestone 1: usable CSV to dashboard flow

1. Transaction/account data model and migration baseline.
2. CSV upload, mapping, preview, and validation.
3. Idempotent import, provenance, and import undo.
4. Transaction list with search, filters, and correction.
5. Categories and transfer handling.
6. Spending and cash flow views.
7. Authenticated basement PC deployment and backup/restore guide.

## Milestone 2: Rocket Money replacement depth

- Institution mapping profiles and classification rules.
- Recurring charges and subscriptions.
- Account balance and net worth history.
- Export, portability, and Google Sheet migration.
- Evaluate automatic account connection options.

## Milestone 3: planning

- Savings goals.
- Future cash flow and scenarios.
- Alerts or AI-assisted insights only after core data accuracy is trusted.

Issue labels: `ready` for sufficiently specified work, `needs-refinement` for unresolved product decisions; `P0`, `P1`, `P2` for priority. Work from an issue to a pull request and link it with `Closes #<issue>`. The owner reviews merges.
