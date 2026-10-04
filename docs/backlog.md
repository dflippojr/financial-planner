# Backlog and milestones

[GitHub Issues](https://github.com/dflippojr/financial-planner/issues) are the work queue. Priority labels are `P0` (first usable release), `P1` (next release), and `P2` (later). Readiness labels are exclusive: `needs-refinement` means a decision or acceptance criterion is missing; `blocked` means the issue is fully specified but a prerequisite has not landed on `main`; `ready` means the issue is fully specified and all prerequisites have landed. Recheck blocked issues after each prerequisite merges.

## Milestone 0: settle first release decisions

- [#1 Resolve import and reporting details](https://github.com/dflippojr/financial-planner/issues/1): verify synthetic CSV shapes, remaining account-sharing rules, and retention.
- [#2 Choose application stack](https://github.com/dflippojr/financial-planner/issues/2): record the implementation and deployment shape.
- [#36 Gather remaining provider CSV shapes and Vanguard meaning](https://github.com/dflippojr/financial-planner/issues/36): Capital One, Apple Card, and Vanguard.
- [#3 Define private and household access](https://github.com/dflippojr/financial-planner/issues/3): settle sharing, editing, and history rules.

## Milestone 1: CSV to cash flow

1. [#4 Account and transaction storage](https://github.com/dflippojr/financial-planner/issues/4) and [#12 sign-in](https://github.com/dflippojr/financial-planner/issues/12).
2. [#5 CSV mapping and preview](https://github.com/dflippojr/financial-planner/issues/5), [#6 safe reimports](https://github.com/dflippojr/financial-planner/issues/6), and provider profiles: [#13 Huntington](https://github.com/dflippojr/financial-planner/issues/13), [#37 Capital One](https://github.com/dflippojr/financial-planner/issues/37), [#14 Apple Card](https://github.com/dflippojr/financial-planner/issues/14), [#15 Vanguard](https://github.com/dflippojr/financial-planner/issues/15).
3. [#7 Transaction review](https://github.com/dflippojr/financial-planner/issues/7) and [#8 categories and transfers](https://github.com/dflippojr/financial-planner/issues/8).
4. [#9 Cash flow over time](https://github.com/dflippojr/financial-planner/issues/9) as the lead view, plus [#10 spending by category](https://github.com/dflippojr/financial-planner/issues/10).
5. [#11 Basement PC deployment, backup, and restore](https://github.com/dflippojr/financial-planner/issues/11).

A release is usable when people can sign in, keep private accounts private, view and edit explicitly shared household accounts, access the app over home network and Tailscale, import the four target providers, safely reimport an overlap, correct transactions, and see cash flow without double counting transfers. Provider-specific transaction meaning and any format limitations must be documented.

## Milestone 2: deeper insight

- [#31 Transaction correction history](https://github.com/dflippojr/financial-planner/issues/31)
- [#16 Saved mapping and categorization rules](https://github.com/dflippojr/financial-planner/issues/16)
- [#17 Recurring charges](https://github.com/dflippojr/financial-planner/issues/17)
- [#46 Recurring detection amount clustering per cadence chain](https://github.com/dflippojr/financial-planner/issues/46)
- [#50 Recurring detection: drop amount outliers and re-pick the cadence chain](https://github.com/dflippojr/financial-planner/issues/50)
- [#124 Recurring series: follow price drift and let members merge, split, and add charges](https://github.com/dflippojr/financial-planner/issues/124) (decisions in requirements)
- [#18 Account balance, net worth, and investment-performance history](https://github.com/dflippojr/financial-planner/issues/18); portfolio/account composition is a possible later extension.
- [#69 Net worth from balances](https://github.com/dflippojr/financial-planner/issues/69)
- [#109 Physical assets and loans for home equity](https://github.com/dflippojr/financial-planner/issues/109)
- [#19 Export and Google Sheet migration](https://github.com/dflippojr/financial-planner/issues/19)

## Milestone 3: connections and planning

- [#20 Research optional account connections](https://github.com/dflippojr/financial-planner/issues/20) (findings: [docs/research/account-connections.md](research/account-connections.md)), including Plaid without a paid requirement now.
- [#21 Savings goals and future cash flow](https://github.com/dflippojr/financial-planner/issues/21).

## Milestone 4: AI and deeper insight

AI decisions are in [requirements](requirements.md#ai-features-2026-10-02-90).

- [#90 AI data policy](https://github.com/dflippojr/financial-planner/issues/90) and [#106 privacy and data policy that every member accepts](https://github.com/dflippojr/financial-planner/issues/106).
- [#91 AI provider layer with per-member backends](https://github.com/dflippojr/financial-planner/issues/91). Research: [#92 Sign in with ChatGPT](https://github.com/dflippojr/financial-planner/issues/92).
- [#94 Chat with your data](https://github.com/dflippojr/financial-planner/issues/94), [#93 category suggestions](https://github.com/dflippojr/financial-planner/issues/93), and [#95 monthly review](https://github.com/dflippojr/financial-planner/issues/95), with [#141 AI phrasing](https://github.com/dflippojr/financial-planner/issues/141) to follow.
- Remaining Rocket Money gaps:
  - [#96 budgets](https://github.com/dflippojr/financial-planner/issues/96) (decisions in requirements)
  - [#97 split transactions](https://github.com/dflippojr/financial-planner/issues/97) (decisions in requirements)
  - [#98 notes and tags](https://github.com/dflippojr/financial-planner/issues/98)
  - [#99 alerts](https://github.com/dflippojr/financial-planner/issues/99)
  - [#100 category trends](https://github.com/dflippojr/financial-planner/issues/100)
  - [#101 saved CSV mappings](https://github.com/dflippojr/financial-planner/issues/101)
  - [#102 recurring review](https://github.com/dflippojr/financial-planner/issues/102)
  - [#103 installable phone app](https://github.com/dflippojr/financial-planner/issues/103)
- Open question: [#104 deleting a person's data](https://github.com/dflippojr/financial-planner/issues/104).

## Milestone 5: everyday polish, planning, and hardening

Decisions are in [requirements](requirements.md#next-round-2026-10-04).

- Everyday: [#148 bulk edit](https://github.com/dflippojr/financial-planner/issues/148), [#149 search and saved filters](https://github.com/dflippojr/financial-planner/issues/149), [#150 year-end report](https://github.com/dflippojr/financial-planner/issues/150), [#146 receipts](https://github.com/dflippojr/financial-planner/issues/146).
- Planning: [#151 what-if scenarios](https://github.com/dflippojr/financial-planner/issues/151), [#152 debt payoff](https://github.com/dflippojr/financial-planner/issues/152), [#153 bills calendar](https://github.com/dflippojr/financial-planner/issues/153), [#154 Google Sheet comparison](https://github.com/dflippojr/financial-planner/issues/154).
- Security and operations: [#155 backup health and off-site copy](https://github.com/dflippojr/financial-planner/issues/155), [#156 passkeys](https://github.com/dflippojr/financial-planner/issues/156), [#157 sign-in log and sessions](https://github.com/dflippojr/financial-planner/issues/157), [#158 Dependabot](https://github.com/dflippojr/financial-planner/issues/158).
- AI:
  - [#159 local-model chat](https://github.com/dflippojr/financial-planner/issues/159)
  - other members, through [#162 the shared local model](https://github.com/dflippojr/financial-planner/issues/162) and [#147 their own API key](https://github.com/dflippojr/financial-planner/issues/147)
  - [#160 chat proposals](https://github.com/dflippojr/financial-planner/issues/160)
  - [#161 unusual spending](https://github.com/dflippojr/financial-planner/issues/161)
- **Build order:** issues that touch the same area go one after another, to avoid conflicts:
  - Transactions page: #148, #149, #146.
  - Security tab: #157, #156.
  - AI provider layer: #162, #147, #159.

Work from an issue to a pull request and link it with `Closes #<issue>`. The owner reviews merges.
