# Financial Planner

A private, self hosted personal finance app. The long term goal is to replace the user's Rocket Money subscription by combining account data with useful transaction analysis. The first usable release imports CSV exports from the target providers and shows cash flow on the home network and Tailscale.

## Status

The Django application foundation and its core financial data model are under development.

- [Requirements](docs/requirements.md)
- [Backlog and milestones](docs/backlog.md)
- [Data model](docs/data-model.md)
- [Authentication and onboarding](docs/authentication.md)
- [Basement PC deployment, backup, and restore](docs/deployment.md)
- [Agent guide and agent-loop workflow](AGENTS.md)
- [GitHub Issues](https://github.com/dflippojr/financial-planner/issues)

## Local model development

Install the pinned dependencies with `python -m pip install -r requirements.txt`. The normal settings use PostgreSQL environment variables documented in `financial_planner/settings.py`. Schema-only checks can use the isolated test settings:

```console
python manage.py migrate --settings=financial_planner.test_settings
python -m pytest
```

The default test settings use in-memory SQLite for speed, which is not the production engine and hides PostgreSQL-only behavior. Before opening or updating a pull request, also run `scripts/test_postgres.sh` (requires Docker), which runs the same suite against a throwaway PostgreSQL 16 container.

Load the explicitly synthetic example records into a development database with `python manage.py loaddata synthetic_demo`. Never substitute a real statement or transaction export into a committed fixture.

Authentication requires `DJANGO_SECRET_KEY`; production also needs the Tailscale HTTPS and host/origin values described in [the authentication guide](docs/authentication.md). After migrating a new installation, create its first household member with `python manage.py seed_first_user --username USERNAME --display-name "DISPLAY NAME" --household "HOUSEHOLD NAME"`. The command prompts for a password without echoing it and prints recovery codes once.

CSV preview temporarily stages uploads outside the repository. Set `CSV_IMPORT_STAGING_DIR` to a private local directory; the default is the operating system's temporary directory. `CSV_IMPORT_STAGE_TTL_SECONDS` defaults to 3600 and is capped at one hour. Uploaded source rows are not logged, and staged files are deleted on cancel, rejected upload, expiry enforcement, and later successful import work.

## Continuous integration

`.github/workflows/review.yml` posts an automated code-bug review as a PR comment when a pull request opens (re-run on demand via `workflow_dispatch`). It runs on a dedicated self-hosted runner (`financial-planner-review`, registered with `ops/github/install-runner.ps1`) that reuses already-authenticated Codex/Claude/Cursor CLIs; see `ops/review/run-review.ps1` for the review logic, adapted from agent-harness.

`.github/workflows/sonar.yml` would run the test suite with coverage and report it to SonarCloud on every push to `main` and every pull request, using a `SONARCLOUD_TOKEN` repository secret and SonarCloud project (organization `dflippojr`, project key `dflippojr_financial-planner`). It's currently disabled (`gh workflow disable`) over private-repo Actions-minutes concerns; local SonarQube (`localhost:9000`) is used instead for now. Re-enable with `gh workflow enable "SonarCloud"` once that's sorted out.

## Working conventions

- Use GitHub Issues for work items. Link pull requests with `Closes #<issue>`.
- Keep real statements, exports, credentials, and local databases out of Git.
- Record decisions and remaining questions in the requirements before implementing provider-specific behavior.
- The owner reviews merges.
