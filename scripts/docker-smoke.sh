#!/usr/bin/env bash
# Docker smoke test: build the production images, start all four compose
# services (db, app, backup, background) on dummy values and a throwaway
# database, and fail if any of them is not what a deploy needs.
#
#   1. app reports healthy and /sign-in/ answers 200;
#   2. backup and background stay running with RestartCount 0 for SOAK_SECONDS
#      after app is healthy;
#   3. no container logs a Python traceback in that window;
#   4. the documented manual backup command produces a dump that
#      ops/backup/verify-restore.sh restores into a scratch database;
#   5. a SIMPLEFIN_SYNC_CRON the schedule parser rejects makes the background
#      container exit with a message naming the variable, not start (#514).
#
# Called by .github/workflows/docker-smoke.yml. Dummy values only: no secrets,
# no real paths, nothing is pushed. A local run is possible, but it is a
# disposable stack under its own project, volumes and ports; it refuses to run
# as the production project or against its named volumes.
#
# Usage: bash scripts/docker-smoke.sh
set -euo pipefail

cd "$(dirname "$0")/.."

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-fp-smoke}"
soak_seconds="${SOAK_SECONDS:-90}"
app_port="${APP_PORT:-8000}"

# The production project is named financial-planner and its volumes default to
# financial-planner-*; the dummy env below overrides both.
if [ "$COMPOSE_PROJECT_NAME" = "financial-planner" ]; then
  echo "Refusing to run against the production compose project" >&2
  exit 2
fi

work_dir=$(mktemp -d)
backup_dir="$work_dir/backups"
env_file="$work_dir/smoke.env"
mkdir -p "$backup_dir"

cat > "$env_file" <<ENV
POSTGRES_PASSWORD=smoke-only-not-a-secret
DJANGO_SECRET_KEY=smoke-only-not-a-secret
DJANGO_ALLOWED_HOSTS=smoke.example.test
DJANGO_CSRF_TRUSTED_ORIGINS=https://smoke.example.test
FIELD_ENCRYPTION_KEY=dGVzdC1maWVsZC1lbmNyeXB0aW9uLWtleS0zMmJ5dGU=
BACKUP_DIR=$backup_dir
APP_PORT=$app_port
POSTGRES_VOLUME_NAME=${COMPOSE_PROJECT_NAME}-postgres-data
RECEIPTS_VOLUME_NAME=${COMPOSE_PROJECT_NAME}-receipts
SIMPLEFIN_SYNC_CRON='30 6 * * 1-5'
BACKUP_CRON='30 3 * * *'
ENV

compose() {
  docker compose --env-file "$env_file" "$@"
}

group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

fail() {
  echo "::error::$*" >&2
  exit 1
}

cleanup() {
  code=$?
  if [ "$code" -ne 0 ]; then
    echo "== Smoke failed (exit $code); container state and logs follow"
    compose ps --all || true
    compose logs --no-color --tail 200 || true
  fi
  compose down --volumes --remove-orphans || true
  exit "$code"
}
trap cleanup EXIT

services="db app backup background"

group "Build the production images"
compose build app backup background
endgroup

group "Start all four services and wait for app to report healthy"
compose up -d --wait --wait-timeout 240 $services
compose ps
endgroup

echo "== GET /sign-in/"
# The Host and X-Forwarded-Proto headers stand in for Tailscale Serve. An empty
# database redirects /sign-in/ to the first-run /setup/ page.
status=$(curl -sL -o /dev/null -w '%{http_code}' \
  -H 'Host: smoke.example.test' -H 'X-Forwarded-Proto: https' \
  "http://127.0.0.1:$app_port/sign-in/")
echo "GET /sign-in/ -> $status"
[ "$status" = 200 ] || fail "/sign-in/ returned $status, expected 200"

container_state() {
  docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$(compose ps -q "$1")"
}

echo "== backup and background stay running with no restarts for ${soak_seconds}s"
deadline=$((SECONDS + soak_seconds))
while [ "$SECONDS" -lt "$deadline" ]; do
  for service in app backup background; do
    state=$(container_state "$service")
    [ "$state" = "running 0" ] || fail "$service is '$state' (status, restart count), expected 'running 0'"
  done
  sleep 5
done
for service in $services; do
  printf '%-12s %s\n' "$service" "$(docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} RestartCount={{.RestartCount}}' "$(compose ps -q "$service")")"
done

echo "== No Python traceback in any container's logs"
logs=$(compose logs --no-color $services)
if grep -n 'Traceback (most recent call last)' <<< "$logs"; then
  fail "a container logged a Python traceback"
fi
echo "none"

group "Manual backup, then restore it into a scratch database"
compose run --rm -T backup /opt/financial-planner/backup.sh
dump=$(compose run --rm -T backup sh -c 'ls -1t /backups/nightly/financial_planner_*.dump | head -n 1' | tr -d '\r')
[ -n "$dump" ] || fail "the manual backup produced no dump under /backups/nightly"
echo "dump: $dump"
compose run --rm -T backup /opt/financial-planner/verify-restore.sh "$dump" | tee "$work_dir/restore.out"
grep -q '^Restore check passed' "$work_dir/restore.out" || fail "verify-restore.sh did not report a pass"
endgroup

echo "== background refuses an invalid SIMPLEFIN_SYNC_CRON"
# --no-deps and run: a one-off container with no restart policy, so the exit
# and its message are what the operator sees instead of a restart loop.
for schedule in '30 6 * * MON-FRI' '60 6 * * *'; do
  set +e
  output=$(timeout 90 docker compose --env-file "$env_file" run --rm -T --no-deps \
    -e SIMPLEFIN_SYNC_CRON="$schedule" background 2>&1)
  code=$?
  set -e
  echo "SIMPLEFIN_SYNC_CRON='$schedule' -> exit $code"
  printf '%s\n' "$output" | tail -n 3
  [ "$code" -ne 0 ] || fail "background started with SIMPLEFIN_SYNC_CRON='$schedule'"
  [ "$code" -ne 124 ] || fail "background kept running with SIMPLEFIN_SYNC_CRON='$schedule'"
  grep -q 'SIMPLEFIN_SYNC_CRON' <<< "$output" || fail "no message names SIMPLEFIN_SYNC_CRON for '$schedule'"
done

echo "== Docker smoke passed"
