# Backlog and milestones

[GitHub Issues](https://github.com/dflippojr/financial-planner/issues) are the work queue. Priority labels are `P0` (first usable release), `P1` (next release), and `P2` (later). `ready` means implementation can begin; `needs-refinement` means an open product or architecture decision remains.

## Milestone 0: settle first release decisions

- [#1 Resolve import and reporting details](https://github.com/dflippojr/financial-planner/issues/1): verify synthetic CSV shapes, cash flow definitions, and retention.
- [#2 Choose application stack](https://github.com/dflippojr/financial-planner/issues/2): record the implementation and deployment shape.
- [#3 Define private and household access](https://github.com/dflippojr/financial-planner/issues/3): settle sharing, editing, and history rules.

## Milestone 1: CSV to cash flow

1. [#4 Account and transaction storage](https://github.com/dflippojr/financial-planner/issues/4) and [#12 sign-in](https://github.com/dflippojr/financial-planner/issues/12).
2. [#5 CSV mapping and preview](https://github.com/dflippojr/financial-planner/issues/5), [#6 safe reimports](https://github.com/dflippojr/financial-planner/issues/6), and provider profiles: [#13 Huntington/Capital One](https://github.com/dflippojr/financial-planner/issues/13), [#14 Apple Card](https://github.com/dflippojr/financial-planner/issues/14), [#15 Vanguard](https://github.com/dflippojr/financial-planner/issues/15).
3. [#7 Transaction review](https://github.com/dflippojr/financial-planner/issues/7) and [#8 categories and transfers](https://github.com/dflippojr/financial-planner/issues/8).
4. [#9 Cash flow over time](https://github.com/dflippojr/financial-planner/issues/9) as the lead view, plus [#10 spending by category](https://github.com/dflippojr/financial-planner/issues/10).
5. [#11 Basement PC deployment, backup, and restore](https://github.com/dflippojr/financial-planner/issues/11).

A release is usable when people can sign in, keep private accounts private, view explicitly shared household accounts, import the four target providers, safely reimport an overlap, correct transactions, and see cash flow without double counting transfers. Provider-specific transaction meaning and any format limitations must be documented.

## Milestone 2: deeper insight

- [#16 Saved mapping and categorization rules](https://github.com/dflippojr/financial-planner/issues/16)
- [#17 Recurring charges](https://github.com/dflippojr/financial-planner/issues/17)
- [#18 Balances and net worth](https://github.com/dflippojr/financial-planner/issues/18)
- [#19 Export and Google Sheet migration](https://github.com/dflippojr/financial-planner/issues/19)

## Milestone 3: connections and planning

- [#20 Research optional account connections](https://github.com/dflippojr/financial-planner/issues/20), including Plaid without a paid requirement now.
- [#21 Savings goals and future cash flow](https://github.com/dflippojr/financial-planner/issues/21).

Work from an issue to a pull request and link it with `Closes #<issue>`. The owner reviews merges.
