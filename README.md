# Financial Planner

A private, self hosted personal finance app. The long term goal is to replace the user's Rocket Money subscription by combining account data with useful transaction analysis. The first usable release imports CSV exports from the target providers and shows cash flow on the home network and Tailscale.

## Status

Requirements and issue backlog are being defined. No financial data or application code has been added.

- [Requirements](docs/requirements.md)
- [Backlog and milestones](docs/backlog.md)
- [Agent guide and agent-loop workflow](AGENTS.md)
- [GitHub Issues](https://github.com/dflippojr/financial-planner/issues)

## Continuous integration

`.github/workflows/review.yml` posts an automated code-bug review as a PR comment when a pull request opens (re-run on demand via `workflow_dispatch`). It runs on a dedicated self-hosted runner (`financial-planner-review`, registered with `ops/github/install-runner.ps1`) that reuses already-authenticated Codex/Claude/Cursor CLIs; see `ops/review/run-review.ps1` for the review logic, adapted from agent-harness. SonarCloud analysis exists as a workflow but is currently disabled (private-repo Actions-minutes concern); local SonarQube is used instead for now.

## Working conventions

- Use GitHub Issues for work items. Link pull requests with `Closes #<issue>`.
- Keep real statements, exports, credentials, and local databases out of Git.
- Record decisions and remaining questions in the requirements before implementing provider-specific behavior.
- The owner reviews merges.
