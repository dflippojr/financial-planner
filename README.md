# Financial Planner

A private, self hosted personal finance app. The long term goal is to replace the user's Rocket Money subscription by combining account data with useful transaction analysis. The first usable release imports CSV exports from the target providers and shows cash flow on the home network and Tailscale.

## Status

Requirements and issue backlog are being defined. No financial data or application code has been added.

- [Requirements](docs/requirements.md)
- [Backlog and milestones](docs/backlog.md)
- [Agent guide and agent-loop workflow](AGENTS.md)
- [GitHub Issues](https://github.com/dflippojr/financial-planner/issues)

## Working conventions

- Use GitHub Issues for work items. Link pull requests with `Closes #<issue>`.
- Keep real statements, exports, credentials, and local databases out of Git.
- Record decisions and remaining questions in the requirements before implementing provider-specific behavior.
- The owner reviews merges.
