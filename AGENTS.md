# Financial Planner Agent Guide

## Start here

This private repository is a self hosted personal finance app. Read docs/requirements.md and docs/backlog.md before changing code. In a whole-issue run, read the live GitHub issue and every comment. In an agent-loop task-file run, follow that scoped task file as the complete instruction and do not expand into the parent issue. The current user request and recorded product decisions take priority over older issue or document text. Record new decisions in the requirements and issue before building on them.

The first release imports CSV exports from Huntington Bank, Capital One, Apple Card, and Vanguard. Its lead view shows income, spending, and net cash flow over time. People have private accounts and explicitly shared household accounts; every household member may edit shared accounts and transactions. The app will run on the basement PC through the home network and Tailscale. Plaid is a later research item and must not become a paid dependency for the first release. See issue #1 for unresolved provider formats and account lifecycle rules; issue #2 chooses the stack.

## Data and correctness

- Never commit or paste real statements, account numbers, credentials, tokens, transaction exports, or personal financial data into source, fixtures, logs, issues, PRs, or screenshots. Use synthetic examples. The .gitignore is a backup guard, not permission to place sensitive files in the repo.
- Preserve exact money amounts and an explicit currency. Define debit and credit signs for every importer. Keep source and import-batch provenance so imports can be corrected or undone.
- Enforce private versus household access on every read, write, import, export, and dashboard calculation. A private account must never leak through search, aggregates, logs, or error messages.
- Reimports must not double count transactions. Internal transfers, credit card payments, and investment activity need explicit report semantics before they affect income or spending.
- Keep historical actuals distinct from future projections. Do not present generated financial recommendations as verified facts.
- Do not invent a provider's CSV schema or Vanguard transaction meaning. If an export shape is unknown, record the gap and use only clearly marked synthetic fixtures until it is resolved.

## Build workflow with agent-loop

The local launcher is D:/Projects/agent-loop, configured as project financial-planner in its ignored projects/financial-planner.env. Run its scripts from Git Bash in that directory. Logs and state go under its ignored logs/financial-planner directory; issue worktrees are siblings under D:/Projects.

- Each worker owns one GitHub issue. Independent ready issues may run concurrently up to the agent-loop project cap. Check dependencies on main and avoid parallel changes to the same schema, access model, or import semantics without an explicit coordination plan. Refine an issue marked needs-refinement before implementation; a ready label is meaningful only when dependencies are merged and no product decision remains.
- For a ready issue, use audit-issue.sh when a pre-implementation review is warranted, then run-issue.sh --project financial-planner --issue N --tier standard (adjust the tier to the work). The launcher creates an isolated worktree and pushes an issue branch. Do not edit main from a worker session.
- Check status with status.sh --project financial-planner. Once a branch is reviewable, open-pr.sh --project financial-planner --issue N creates or finds the PR. Use run-pr.sh for work on an existing PR branch when needed.
- Link the PR to its issue with Closes #N. The owner approves every merge; agents must not merge, close issues early, or publish financial data.
- Keep changes scoped to the issue. If a prerequisite is not on main, or a decision is missing, stop at a clean checkpoint and record the blocker instead of implementing a guess.
- For code changes, run focused checks while working and the existing full suite before pushing, as agent-loop requires. If no suite exists yet, report that accurately. Do not add tests that merely copy implementation logic.

Do not assume agent-harness CI, deployment, or review infrastructure exists in this new repository. Add only what a financial-planner issue calls for.
