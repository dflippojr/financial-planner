#!/usr/bin/env bash
# Run locally what the CI gate runs on every pull request, before pushing.
#
#   1. The statement-file guard (guards.yml).
#   2. The hashed locks match their inputs, ruff and pyright (lint.yml,
#      scripts/lock.sh, scripts/lint.sh).
#   3. The migration-number collision check (guards.yml) and the migration
#      completeness check (postgres.yml).
#   4. The suite on a throwaway PostgreSQL 18 with coverage, failing below the
#      .coveragerc floor (postgres.yml plus the coverage run in sonar.yml). It
#      includes the populated migration test postgres.yml runs as its own step.
#   5. The browser layout tests in Chromium (playwright.yml).
#
# Not covered here: pip-audit, the SonarCloud scan, the Playwright UI journeys
# (npm run test:ui), the disposable UI smoke and the Docker smoke build. CI
# runs those. Requires Docker, requirements-test.lock, Chromium
# (python -m playwright install chromium) and built CSS (scripts/build_css.sh).
# On Windows some tests skip, so coverage here runs about a point below CI's.
#
# Usage: bash scripts/ci-local.sh
set -euo pipefail

python_bin="${PYTHON:-python}"
cd "$(dirname "$0")/.."

echo "== Statement-file guard"
"$python_bin" scripts/check_statement_files.py

echo "== Locks match their inputs"
PYTHON="$python_bin" bash scripts/lock.sh --check

PYTHON="$python_bin" bash scripts/lint.sh

echo "== Migration numbers do not collide"
"$python_bin" scripts/check_migration_numbers.py

echo "== Migrations are complete"
DJANGO_SETTINGS_MODULE=financial_planner.test_settings \
  "$python_bin" manage.py makemigrations --check --dry-run

echo "== Suite on PostgreSQL 18 with the coverage floor"
bash scripts/test_postgres.sh -n 4 --dist worksteal -m "not browser" --cov=. --cov-report=term

echo "== Browser layout tests"
"$python_bin" -m pytest tests -q -p no:cacheprovider -m browser -n 4 -rs \
  --ignore=tests/test_ui_instance.py --junitxml=test-results/ci-local-browser.xml

# As in playwright.yml: the tests skip themselves when Chromium or the CSS is
# missing, which would pass while checking nothing.
"$python_bin" - <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse("test-results/ci-local-browser.xml").getroot()
suite = root if root.tag == "testsuite" else root.find("testsuite")
tests, skipped = int(suite.get("tests")), int(suite.get("skipped"))
print(f"{tests} browser tests, {skipped} skipped")
sys.exit(1 if tests == 0 or skipped else 0)
PY
