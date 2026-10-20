#!/usr/bin/env bash
# Docker smoke test: build the production images, start all four compose
# services (db, app, backup, background) on dummy values and a throwaway
# database, and fail if any of them is not what a deploy needs.
#
#   1. app reports healthy and /sign-in/ answers 200;
#   2. backup and background stay running with RestartCount 0 for SOAK_SECONDS
#      after app is healthy, checked again once the time is up;
#   3. no container logs a Python traceback in that window;
#   4. the documented manual backup command produces a dump that
#      ops/backup/verify-restore.sh restores into a scratch database;
#   5. `docker compose up` with a SIMPLEFIN_SYNC_CRON the schedule parser
#      rejects must refuse to start background with a message naming the
#      variable, not leave it in a restart loop. Today restart: unless-stopped
#      restarts the refusing container, so this is a strict expected failure
#      for #560: it passes while background loops and fails once it stops.
#
# Called by .github/workflows/docker-smoke.yml. Dummy values only: no secrets,
# no real paths, nothing is pushed. A local run is possible, but it is a
# disposable stack under its own project, volumes and ports; it refuses to run
# as the production project or against its named volumes.
#
# Usage: bash scripts/docker-smoke.sh
# tests/test_docker_smoke.py sources it to drive the checks against stubs.
set -euo pipefail

soak_seconds="${SOAK_SECONDS:-90}"
poll_seconds="${SOAK_POLL_SECONDS:-5}"
# How long background is watched for a restart after an invalid schedule.
refuse_seconds="${REFUSE_SECONDS:-30}"
services="db app backup background"

compose() {
  docker compose --env-file "$env_file" "$@"
}

group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

fail() {
  echo "::error::$*" >&2
  exit 1
}

# "<status> <restart count>" of the service's container, or "absent 0".
container_state() {
  local id
  id=$(compose ps --all -q "$1")
  if [ -z "$id" ]; then
    echo "absent 0"
    return
  fi
  docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$id"
}

assert_running_without_restarts() {
  local service state
  for service in "$@"; do
    state=$(container_state "$service")
    [ "$state" = "running 0" ] || fail "$service is '$state' (status, restart count), expected 'running 0'"
  done
}

# Check the services every poll_seconds and once more after soak_seconds, so a
# restart in the last interval still fails the smoke.
soak() {
  local deadline=$((SECONDS + soak_seconds))
  while :; do
    assert_running_without_restarts "$@"
    [ "$SECONDS" -lt "$deadline" ] || return 0
    sleep "$poll_seconds"
  done
}

# Start the stack the way an operator does, with SIMPLEFIN_SYNC_CRON set to
# $1, and watch background for refuse_seconds. Prints one word: crash-loop
# (Docker restarted it), started (it is still running), or refused (never
# restarted and not running). The compose output and every service's logs go
# to $2, for the message check.
invalid_cron_outcome() {
  local schedule=$1 out=$2 deadline state
  # The shell value overrides the env file's, as an operator's edit would.
  SIMPLEFIN_SYNC_CRON="$schedule" compose up -d $services > "$out" 2>&1 || true
  deadline=$((SECONDS + refuse_seconds))
  while :; do
    state=$(container_state background)
    case $state in
      restarting\ *|*\ [1-9]*) break ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep "$poll_seconds"
  done
  compose logs --no-color >> "$out" 2>&1 || true
  case $state in
    restarting\ *|*\ [1-9]*) echo crash-loop ;;
    running\ 0) echo started ;;
    *) echo refused ;;
  esac
}

expect_invalid_cron_refused() {
  local schedule=$1 out=$work_dir/invalid-cron.out outcome
  outcome=$(invalid_cron_outcome "$schedule" "$out")
  echo "SIMPLEFIN_SYNC_CRON='$schedule' -> background $outcome ($(container_state background))"
  grep 'SIMPLEFIN_SYNC_CRON' "$out" | tail -n 2 || true
  [ "$outcome" != started ] || fail "background started with SIMPLEFIN_SYNC_CRON='$schedule'"
  grep -q 'SIMPLEFIN_SYNC_CRON' "$out" || fail "no message names SIMPLEFIN_SYNC_CRON for '$schedule'"
  if [ "$outcome" = refused ]; then
    fail "background now refuses SIMPLEFIN_SYNC_CRON='$schedule' without a restart loop, so #560 looks fixed: make this check a plain pass"
  fi
  local note="Expected failure (#560): SIMPLEFIN_SYNC_CRON='$schedule' is rejected with a message, but restart: unless-stopped restarts background in a loop"
  echo "::warning::$note"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- $note" >> "$GITHUB_STEP_SUMMARY"
  fi
}

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

cd "$(dirname "$0")/.."

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-fp-smoke}"
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

group "Build the production images"
compose build app backup background
endgroup

group "Start all four services and wait for app to report healthy"
# background disables its healthcheck, which makes `up --wait` fail on it
# ("has no healthcheck configured"), so only db and app are waited on. The
# soak below is what proves backup and background stay up.
compose up -d --wait --wait-timeout 240 db app
compose up -d backup background
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

echo "== backup and background stay running with no restarts for ${soak_seconds}s"
soak app backup background
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

group "compose up refuses an invalid SIMPLEFIN_SYNC_CRON"
# The normal services and their restart policy, not a one-off container: what
# matters is what Docker does with the refusal, not only the exit code.
for schedule in '30 6 * * MON-FRI' '60 6 * * *'; do
  expect_invalid_cron_refused "$schedule"
done
endgroup

echo "== Docker smoke passed"
