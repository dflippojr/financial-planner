#!/usr/bin/env bash
# Run the test suite against a throwaway PostgreSQL 18 container.
#
# The default test settings use in-memory SQLite, which is fast but is not the
# production engine and silently ignores behavior PostgreSQL enforces (for
# example SELECT ... FOR UPDATE combined with DISTINCT). Run this before
# opening or updating a pull request. Requires Docker.
#
# Like CI, the server keeps its data in memory with durability off; every
# database here is thrown away. Pass -n 4 to use 4 xdist workers as CI does
# (pip install -r requirements-test.txt).
#
# Usage: scripts/test_postgres.sh [pytest args...]
set -euo pipefail

port="${TEST_PG_PORT:-55432}"
# One container per port, so parallel runs (each with its own TEST_PG_PORT)
# never remove each other's database.
name="${TEST_PG_NAME:-financial-planner-test-pg-${port}}"
python_bin="${PYTHON:-python}"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$name" --tmpfs /var/lib/postgresql:rw \
  -e POSTGRES_USER=financial_planner -e POSTGRES_PASSWORD=test -e POSTGRES_DB=financial_planner \
  -p "127.0.0.1:${port}:5432" postgres:18-alpine \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null
until docker exec "$name" pg_isready -U financial_planner >/dev/null 2>&1; do sleep 1; done

FINANCIAL_PLANNER_TEST_DB=postgres POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$port" POSTGRES_PASSWORD=test \
  "$python_bin" -m pytest tests -q -p no:cacheprovider "$@"
