#!/bin/sh
# Restore a dump into a throwaway database on the same server to prove that it
# restores, not just that pg_restore --list can read it, then drop that
# database. Errors are fixed messages: pg_restore and psql output can quote row
# values, so it is discarded. Success prints only a table count.
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: verify-restore.sh /backups/nightly/financial_planner_TIMESTAMP.dump" >&2
  exit 2
fi

for variable in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_HOST; do
  eval "value=\${$variable:-}"
  if [ -z "$value" ]; then
    echo "$variable must be set" >&2
    exit 2
  fi
done

backup_file=$1
if [ ! -f "$backup_file" ]; then
  echo "Restore check: dump file does not exist" >&2
  exit 2
fi

# Fixed and unmistakable, so it can never be the live database.
scratch_db=financial_planner_restore_check
core_tables="django_migrations finance_account finance_person finance_transaction"

export PGPASSWORD=$POSTGRES_PASSWORD

run_psql() {
  database=$1
  shift
  psql \
    --host "$POSTGRES_HOST" \
    --port "${POSTGRES_PORT:-5432}" \
    --username "$POSTGRES_USER" \
    --dbname "$database" \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --set ON_ERROR_STOP=1 \
    "$@"
}

drop_scratch() {
  run_psql postgres --command "DROP DATABASE IF EXISTS $scratch_db WITH (FORCE)" >/dev/null 2>&1
}

fail() {
  echo "Restore check failed: $1" >&2
  exit 1
}

cleanup() {
  code=$?
  if ! drop_scratch; then
    echo "Restore check failed: could not drop the scratch database" >&2
    if [ "$code" -eq 0 ]; then
      code=1
    fi
  fi
  exit "$code"
}

# pg_restore 17+ always sends SET transaction_timeout, which an older server
# rejects, so restore.sh can't restore there either. Say so instead of failing
# with a generic restore error.
if ! server_version=$(run_psql postgres --command "SHOW server_version_num" 2>/dev/null); then
  fail "could not connect to the database server"
fi
server_version=$(printf '%s' "$server_version" | tr -d '[:space:]')
case "$server_version" in
  ''|*[!0-9]*) fail "could not read the server version" ;;
esac
server_major=$((server_version / 10000))
client_major=$(pg_restore --version | sed -n 's/^pg_restore (PostgreSQL) \([0-9][0-9]*\).*/\1/p')
if [ -n "$client_major" ] && [ "$server_major" -lt "$client_major" ]; then
  fail "PostgreSQL $server_major server is older than pg_restore $client_major; upgrade the server (docs/deployment.md)"
fi

if ! drop_scratch; then
  fail "could not drop a leftover scratch database"
fi
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if ! table_list=$(pg_restore --list "$backup_file" 2>/dev/null); then
  fail "pg_restore could not read the dump"
fi
expected=$(printf '%s\n' "$table_list" | grep -c 'TABLE DATA' || true)

if ! run_psql postgres --command "CREATE DATABASE $scratch_db" >/dev/null 2>&1; then
  fail "could not create the scratch database"
fi

if ! pg_restore \
  --host "$POSTGRES_HOST" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "$POSTGRES_USER" \
  --dbname "$scratch_db" \
  --no-owner \
  --no-privileges \
  --exit-on-error \
  "$backup_file" >/dev/null 2>&1
then
  fail "pg_restore could not restore the dump"
fi

if ! restored=$(run_psql "$scratch_db" --command "
SELECT count(*)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'" 2>/dev/null); then
  fail "could not count the restored tables"
fi
restored=$(printf '%s' "$restored" | tr -d '[:space:]')
if [ "$restored" != "$expected" ]; then
  fail "restored $restored tables but the dump has $expected"
fi

if ! present=$(run_psql "$scratch_db" --command "
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'" 2>/dev/null); then
  fail "could not list the restored tables"
fi
for table in $core_tables; do
  if ! printf '%s\n' "$present" | grep -qx "$table"; then
    fail "core table $table is missing"
  fi
done

echo "Restore check passed: $restored tables restored"
