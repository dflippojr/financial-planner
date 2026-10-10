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
- Keep GitHub's fork workflow approval setting at **Require approval for all external contributors** (`all_external_contributors`). The automated review workflow's job guard skips fork heads and Dependabot PRs; the shared reviewer also refuses fork PRs.
- The review runner installer configures `ACTIONS_RUNNER_HOOK_JOB_STARTED` with a host-side allow-list for this repository's `review.yml` on `main`, triggered only by base-repository `pull_request_target` or `workflow_dispatch`. Other jobs and missing or malformed context fail before workflow steps. The owner must apply these host changes; editing the repository does not update a live runner.
- Review registration is ephemeral (one job), under a dedicated non-administrator Windows account. The hook and registration limit exposure but do not sandbox the reviewer or erase its host. Untrusted PR content is read with shell access. See [runner setup and lifecycle](docs/review-runner.md) for the controls, account isolation, and owner reprovisioning steps.

## Automated guards

The `Repository guards` workflow runs on pull requests, on pushes to `main`, and weekly.

- **Statement files.** `scripts/check_statement_files.py` fails if `git ls-files` lists a `.csv`, `.ofx`, `.qfx`, `.qif`, `.xlsx`, `.xls` or `.pdf` file outside `tests/fixtures/`. It reads paths only, never contents. If it fires, remove the file with `git rm --cached` and, if it was ever pushed, treat the data as exposed. A genuinely synthetic fixture belongs under `tests/fixtures/`; any other exception needs an explicit entry in `ALLOWED` in the script.
- **Dependency audit.** `pip-audit -r requirements.txt` fails on a pin with a known advisory. Bump the pin to the fixed release. If no fixed release exists, add `--ignore-vuln <ID>` to the workflow step with a dated justification comment, and remove it once a fix ships. The weekly run only fails visibly; it opens no issue.
