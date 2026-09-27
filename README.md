# Financial Planner

A private, self hosted personal finance app. The long term goal is to replace the user's Rocket Money subscription by combining account data with useful transaction analysis. The first usable release imports CSV exports from the target providers and shows cash flow on the home network and Tailscale.

## Status

The Django application foundation and its core financial data model are under development.

- [Requirements](docs/requirements.md)
- [Backlog and milestones](docs/backlog.md)
- [Data model](docs/data-model.md)
- [Agent guide and agent-loop workflow](AGENTS.md)
- [GitHub Issues](https://github.com/dflippojr/financial-planner/issues)

## Local model development

Install the pinned dependencies with `python -m pip install -r requirements.txt`. The normal settings use PostgreSQL environment variables documented in `financial_planner/settings.py`. Schema-only checks can use the isolated test settings:

```console
python manage.py migrate --settings=financial_planner.test_settings
python -m pytest
```

Load the explicitly synthetic example records into a development database with `python manage.py loaddata synthetic_demo`. Never substitute a real statement or transaction export into a committed fixture.

## Continuous integration

`.github/workflows/sonar.yml` runs the test suite with coverage and reports it to SonarCloud on every push to `main` and every pull request. It needs a `SONARCLOUD_TOKEN` repository secret (Settings > Secrets and variables > Actions); see the workflow file for what it expects from the SonarCloud project (organization `dflippojr`, project key `dflippojr_financial-planner`).

## Working conventions

- Use GitHub Issues for work items. Link pull requests with `Closes #<issue>`.
- Keep real statements, exports, credentials, and local databases out of Git.
- Record decisions and remaining questions in the requirements before implementing provider-specific behavior.
- The owner reviews merges.
