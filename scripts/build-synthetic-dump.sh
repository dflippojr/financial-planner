#!/usr/bin/env bash
# Build a synthetic pg_dump for ops/deploy-rehearsal.ps1 -DumpPath (#538).
#
# Starts a throwaway PostgreSQL 18 container, seeds it with the #532 fixture
# builder (tests/migration_fixtures/seed.py) with finance stopped at
# FINANCE_MIGRATION (0061_savings_goal_wishlist at the newest), and writes a custom-format dump, the format the nightly
# backup writes. Every name, amount and identifier in it is synthetic. Needs
# Docker and the test requirements (pip install -r requirements-test.txt).
#
# Usage: scripts/build-synthetic-dump.sh OUTPUT.dump FINANCE_MIGRATION [small|large]
#   e.g. scripts/build-synthetic-dump.sh .tmp/synthetic.dump 0061_savings_goal_wishlist
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "Usage: scripts/build-synthetic-dump.sh OUTPUT.dump FINANCE_MIGRATION [small|large]" >&2
  exit 2
fi
output=$1
migration=$2
size=${3:-small}
python_bin="${PYTHON:-python}"
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name="fp-synthetic-dump-$$"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" --tmpfs /var/lib/postgresql:rw \
  -e POSTGRES_USER=financial_planner -e POSTGRES_PASSWORD=synthetic -e POSTGRES_DB=financial_planner \
  -p 127.0.0.1::5432 postgres:18-alpine -c fsync=off -c synchronous_commit=off >/dev/null
until docker exec "$name" pg_isready -U financial_planner -d financial_planner >/dev/null 2>&1; do sleep 1; done
port=$(docker port "$name" 5432/tcp | head -n 1 | sed 's/.*://')

(cd "$repo_dir" && POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$port" POSTGRES_PASSWORD=synthetic \
  "$python_bin" -m tests.migration_fixtures.seed --finance-migration "$migration" --size "$size")
docker exec "$name" pg_dump --username financial_planner --dbname financial_planner \
  --format custom --no-owner --no-privileges > "$output"
echo "wrote $output"
