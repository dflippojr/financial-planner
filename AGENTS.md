# Financial Planner Agent Guide

## Start here

This public repository is a self hosted personal finance app; anything committed, and any issue or PR text, is visible to anyone. Read docs/requirements.md and docs/backlog.md before changing code. In a whole-issue run, read the live GitHub issue and every comment. In a run driven by a scoped task file, follow that task file as the complete instruction and do not expand into the parent issue. The current user request and recorded product decisions take priority over older issue or document text. Record new decisions in the requirements and issue before building on them.

The first release imports CSV exports from Huntington Bank, Capital One, Apple Card, and Vanguard. Its lead view shows income, spending, and net cash flow over time. People have private accounts and explicitly shared household accounts; every household member may edit shared accounts and transactions. The app will run on the basement PC through the home network and Tailscale. Plaid is a later research item and must not become a paid dependency for the first release. See issue #1 for unresolved provider formats and account lifecycle rules; issue #2 chooses the stack.

## Data and correctness

- Never commit or paste real statements, account numbers, credentials, tokens, transaction exports, or personal financial data into source, fixtures, logs, issues, PRs, or screenshots. Use synthetic examples. The .gitignore is a backup guard, not permission to place sensitive files in the repo.
- Preserve exact money amounts and an explicit currency. Define debit and credit signs for every importer. Keep source and import-batch provenance so imports can be corrected or undone.
- Enforce private versus household access on every read, write, import, export, and dashboard calculation. A private account must never leak through search, aggregates, logs, or error messages.
- Reimports must not double count transactions. Internal transfers, credit card payments, and investment activity need explicit report semantics before they affect income or spending.
- Keep historical actuals distinct from future projections. Do not present generated financial recommendations as verified facts.
- Do not invent a provider's CSV schema or Vanguard transaction meaning. If an export shape is unknown, record the gap and use only clearly marked synthetic fixtures until it is resolved.

## Build workflow

Work is dispatched with Orca. Each worker gets one GitHub issue in its own git worktree and branch, and never edits main.

- One issue per worktree. Independent ready issues may run concurrently. Check dependencies on main and avoid parallel changes to the same schema, access model, or import semantics without an explicit coordination plan.
- Use exactly one readiness label: needs-refinement when a decision or acceptance criterion is missing; blocked when the issue is fully specified but a prerequisite is not merged on main; ready only when the specification is complete and every prerequisite has landed. List prerequisites by issue number in the issue body. Do not start blocked issues. Recheck and relabel them after each prerequisite merges.
- Keep changes scoped to the issue. If a prerequisite is not on main, or a decision is missing, stop at a clean checkpoint and record the blocker on the issue instead of implementing a guess.
- Commit as you go and push the issue branch with an explicit refspec (git push origin HEAD:refs/heads/BRANCH). Open a PR whose body says Closes #N, assigned to the owner (--assignee dflippojr).
- CI runs on every PR and every push to main through .github/workflows/ci.yml, which ends in one required check named gate. Watch it until it is green and fix failures at the root cause. The automated review (review.yml) runs once when the PR opens; after later pushes, dispatch a re-review with gh workflow run review.yml -f pr_number=N -f mode=full.
- The owner approves and merges every PR. Agents must not merge, close issues early, or publish financial data.

## How to prove a change works

- Before pushing, run bash scripts/ci-local.sh. It runs what the gate runs locally: the statement-file guard, the migration check, the suite on a throwaway PostgreSQL 18 with the coverage floor in .coveragerc, and the browser layout tests. It needs Docker. Its header lists the jobs only CI runs. While working, run focused tests with python -m pytest tests/test_x.py, or bash scripts/test_postgres.sh with the same arguments to use PostgreSQL.
- For a UI change, use the verify skill in .claude/skills/verify/SKILL.md (launch, doctor, drive, evidence and cleanup on a disposable instance per docs/ui-testing.md).
- For a bug fix, write the test first and show it fails on main, then make it pass. Do not add tests that merely copy implementation logic.
- Give the PR body a ## Proof table, as the shared PR template asks (dflippojr/.github#13): each acceptance criterion, how it was verified (command, test name, verify skill) and the evidence (output excerpt, artifact link, red before and green after). Under ### Not verified, say what you did not check and why. Under ### Gate changes, list any test deleted or skipped and any check or threshold loosened, with the reason.
- Intentional test deletions and skips, added retries, and edits to workflow, coverage, Sonar or lint config fail the gate-guard check. They need the owner-applied `gate-change-approved` label and an entry under the PR's `### Gate changes`. Agents never add that label; ask the owner in the PR body. The label counts only when the owner applied it after the newest push, so every later push needs a new approval. The ruleset requires gate and gate-guard; gate-guard is posted by .github/workflows/gate-guard-target.yml from main under pull_request_target, so a PR cannot rewrite its own judge.
