#!/usr/bin/env bash
# Static analysis, as the lint job in ci.yml runs it: ruff with the rules in
# pyproject.toml, then pyright in basic mode on finance/ and financial_planner/
# against pyright-baseline.json. Needs requirements-test.txt.
#
# Usage: bash scripts/lint.sh
set -euo pipefail

python_bin="${PYTHON:-python}"
cd "$(dirname "$0")/.."

echo "== ruff"
"$python_bin" -m ruff check .

echo "== pyright (new errors only)"
"$python_bin" scripts/pyright_baseline.py
