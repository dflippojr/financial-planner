#!/usr/bin/env bash
# Docker smoke test: build the production images, start all four long-running
# compose services (db, app, backup, background, which waits for the one-shot
# config-check) on dummy values and a throwaway database, and fail if any of
# them is not what a deploy needs.
#
#   1. app reports healthy and /sign-in/ answers 200;
#   2. backup and background stay running with RestartCount 0 for SOAK_SECONDS
#      after app is healthy, checked again once the time is up;
#   3. no container logs a Python traceback in that window;
#   4. the documented manual backup command produces a dump that
#      ops/backup/verify-restore.sh restores into a scratch database;
#   5. `docker compose up -d` with a SIMPLEFIN_SYNC_CRON the schedule parser
#      rejects must fail in the one-shot config-check service with a message
#      naming the variable, never start background (so no restart loop), and
#      leave app healthy with /health/ naming the variable (#560). The verdict
#      is the pytest file tests/docker_smoke_invalid_cron.py, run with the app
#      image's pytest.
#
# Called by .github/workflows/docker-smoke.yml. Dummy values only: no secrets,
# no real paths, nothing is pushed. A local run is possible, but it is a
# disposable stack under its own project, volumes and ports; it refuses to run
# as the production project, ignores exported compose settings, and stops
# before starting anything if compose resolves a volume or writable bind mount
# outside the throwaway project.
#
# Usage: bash scripts/docker-smoke.sh
# tests/test_docker_smoke.py sources it to drive the checks against stubs.
set -euo pipefail

soak_seconds="${SOAK_SECONDS:-90}"
poll_seconds="${SOAK_POLL_SECONDS:-5}"
# How long background is watched for a restart after an invalid schedule.
refuse_seconds="${REFUSE_SECONDS:-30}"
services="db app backup background"
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Compose lets the shell environment override --env-file, so an exported
# production setting (POSTGRES_VOLUME_NAME, RECEIPTS_VOLUME_NAME, BACKUP_DIR,
# OFFSITE_*, COMPOSE_FILE) would point the throwaway project at real storage,
# and `down --volumes` would delete it. Only these reach docker; every compose
# value comes from the env file, and -f skips any compose.override.yml.
docker_env_names="PATH HOME XDG_RUNTIME_DIR DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY"

compose() {
  local name keep=()
  for name in $docker_env_names; do
    if [ -n "${!name+set}" ]; then
      keep+=("$name=${!name}")
    fi
  done
  env -i "${keep[@]}" docker compose -p "$COMPOSE_PROJECT_NAME" -f "$repo_dir/compose.yml" \
    --env-file "$env_file" "$@"
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

# Fail unless compose resolves every named volume into this project and every
# writable bind mount under work_dir, so neither the run nor the cleanup's
# `down --volumes` can reach another stack's storage.
assert_disposable_storage() {
  local config
  config=$(compose config --format json) || fail "docker compose config failed"
  python3 -I - "$COMPOSE_PROJECT_NAME" "$work_dir" "$config" <<'PYTHON' ||
import json
import sys

project, work_dir, config = sys.argv[1], sys.argv[2].rstrip("/") + "/", json.loads(sys.argv[3])
problems = [
    f"volume {key} is named {spec.get('name')}"
    for key, spec in config.get("volumes", {}).items()
    if not str(spec.get("name", "")).startswith((project + "-", project + "_"))
]
for service, spec in config.get("services", {}).items():
    for mount in spec.get("volumes", []):
        source = str(mount.get("source", ""))
        if mount.get("type") == "bind" and not mount.get("read_only") and not (source + "/").startswith(work_dir):
            problems.append(f"{service} mounts {source} writable")
for problem in problems:
    print(problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PYTHON
    fail "compose resolves storage outside the throwaway $COMPOSE_PROJECT_NAME project (listed above)"
}

# app's health status: healthy, unhealthy, starting, none or absent.
container_health() {
  local id
  id=$(compose ps --all -q "$1")
  if [ -z "$id" ]; then
    echo absent
    return
  fi
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id"
}

# Start the stack the way an operator does, with SIMPLEFIN_SYNC_CRON set to
# $1, and watch background for refuse_seconds. Prints one word: crash-loop
# (Docker restarted it), started (it is still running), or refused (never
# restarted and not running). Records the rest of the case in directory $2:
# up's exit code and output with every service's logs, config-check's logs,
# app's health once it has had time to start, and the /health/ body.
invalid_cron_outcome() {
  local schedule=$1 case_dir=$2 deadline state up_exit=0 cron_env=$work_dir/invalid-cron.env
  # The operator's edit: the smoke env file with only the schedule changed.
  # Every compose call in here, container_state's included, uses it.
  { grep -v '^SIMPLEFIN_SYNC_CRON=' "$env_file"; printf "SIMPLEFIN_SYNC_CRON='%s'\n" "$schedule"; } > "$cron_env"
  local env_file=$cron_env
  # config-check refuses the schedule, so up fails; the smoke carries on.
  compose up -d > "$case_dir/log" 2>&1 || up_exit=$?
  echo "$up_exit" > "$case_dir/up-exit"
  deadline=$((SECONDS + refuse_seconds))
  while :; do
    state=$(container_state background)
    case $state in
      restarting\ *|*\ [1-9]*) break ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep "$poll_seconds"
  done
  # app does not depend on config-check, so it must come up healthy anyway.
  compose up -d --wait --wait-timeout 240 app >> "$case_dir/log" 2>&1 || true
  container_health app > "$case_dir/app-health"
  curl -s -H 'Host: smoke.example.test' -H 'X-Forwarded-Proto: https' \
    "http://127.0.0.1:$app_port/health/" > "$case_dir/health" || true
  compose logs --no-color config-check > "$case_dir/config-check-log" 2>&1 || true
  compose logs --no-color >> "$case_dir/log" 2>&1 || true
  case $state in
    restarting\ *|*\ [1-9]*) echo crash-loop ;;
    running\ 0) echo started ;;
    *) echo refused ;;
  esac
}

# Run tests/docker_smoke_invalid_cron.py on the recorded cases with the app
# image's own pytest, in a one-off container with no database.
judge_invalid_cron() {
  chmod -R a+rX "$work_dir/invalid-cron"
  compose run --rm --no-deps -T --entrypoint python \
    -v "$repo_dir/tests/docker_smoke_invalid_cron.py:/smoke/invalid_cron.py:ro" \
    -v "$work_dir/invalid-cron:/smoke/invalid-cron:ro" \
    app -B -m pytest -p no:django -p no:cacheprovider --noconftest -c /dev/null --rootdir /smoke -rxX /smoke/invalid_cron.py
}

# Try each schedule in $@, record its case for the pytest verdict, and fail
# unless the verdict passes.
expect_invalid_cron_refused() {
  local schedule case_dir outcome number=0
  for schedule in "$@"; do
    number=$((number + 1))
    case_dir=$work_dir/invalid-cron/$number
    mkdir -p "$case_dir"
    printf '%s\n' "$schedule" > "$case_dir/schedule"
    outcome=$(invalid_cron_outcome "$schedule" "$case_dir")
    echo "$outcome" > "$case_dir/outcome"
    echo "SIMPLEFIN_SYNC_CRON='$schedule' -> up exit $(cat "$case_dir/up-exit"), background $outcome ($(container_state background)), app $(cat "$case_dir/app-health")"
    grep -h 'SIMPLEFIN_SYNC_CRON' "$case_dir/config-check-log" "$case_dir/health" || true
  done
  judge_invalid_cron 2>&1 || fail "the invalid SIMPLEFIN_SYNC_CRON check failed (pytest output above)"
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

echo "== Storage stays inside the $COMPOSE_PROJECT_NAME project"
assert_disposable_storage
echo "volumes ${COMPOSE_PROJECT_NAME}-*, writable bind mounts under $work_dir"

# Installed only once the storage check passed: `down --volumes` deletes what
# compose resolves, and nothing was started before this point.
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
compose build app backup background config-check
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
# The operator's `up -d` with the normal services and restart policy, not a
# one-off container: what matters is what Docker does with the refusal.
expect_invalid_cron_refused '30 6 * * MON-FRI' '60 6 * * *'
endgroup

echo "== Docker smoke passed"
