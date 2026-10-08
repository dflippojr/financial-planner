# Security policy

Financial Planner is a self-hosted personal finance app. It runs on one household's own machine and is reachable only through a private network (Tailscale). This repository contains the code only. It holds no hosted service, credentials, or financial data.

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a vulnerability** button on this repository's Security tab. Do not open a public issue. Include the affected version or commit, the steps to reproduce, and the impact you expect. You should get an acknowledgement within a week.

## Scope

In scope:
- Authentication, sessions, and Google sign-in.
- The private and household access checks.
- CSV import parsing and staging.
- Anything that could expose one member's private accounts to another, through pages, totals, charts, exports, logs, or error messages.

Out of scope:
- The operator's own network and host configuration.
- Third-party services (Google, Tailscale).
- Problems that need an already-compromised host.

## Data handling in this repository

- Never commit real statements, CSV exports, account numbers, credentials, tokens, or other personal financial data. Tests and fixtures use clearly synthetic data only.
- Secrets live in an environment file outside the repository (see `docs/deployment.md`).
- Pull requests from forks never run on the maintainer's self-hosted runner. The automated review workflow runs on pull requests opened from this repository's branches or when dispatched by a maintainer, and it refuses pull requests from forks.

## Automated guards

The `Repository guards` workflow runs on pull requests, on pushes to `main`, and weekly.

- **Statement files.** `scripts/check_statement_files.py` fails if `git ls-files` lists a `.csv`, `.ofx`, `.qfx`, `.qif`, `.xlsx`, `.xls` or `.pdf` file outside `tests/fixtures/`. It reads paths only, never contents. If it fires, remove the file with `git rm --cached` and, if it was ever pushed, treat the data as exposed. A genuinely synthetic fixture belongs under `tests/fixtures/`; any other exception needs an explicit entry in `ALLOWED` in the script.
- **Dependency audit.** `pip-audit -r requirements.txt` fails on a pin with a known advisory. Bump the pin to the fixed release. If no fixed release exists, add `--ignore-vuln <ID>` to the workflow step with a dated justification comment, and remove it once a fix ships. The weekly run only fails visibly; it opens no issue.
