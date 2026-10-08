#!/bin/sh
set -eu

. "$(dirname "$0")/audit.sh"
audit_operation=restore
audit_line "$audit_operation" started
trap 'audit_end $?' EXIT

if [ "$#" -ne 1 ]; then
  echo "Usage: restore.sh /backups/nightly/financial_planner_TIMESTAMP.dump" >&2
  exit 2
fi

for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_HOST RECEIPTS_DIR; do
  eval "value=\${$variable:-}"
  if [ -z "$value" ]; then
    echo "$variable must be set" >&2
    exit 2
  fi
done

backup_file=$1
if [ ! -f "$backup_file" ]; then
  echo "Backup file does not exist" >&2
  exit 2
fi

stem=${backup_file%.dump}
receipts_archive="${stem}.receipts.tar.gz"
restore_receipts=1
if [ ! -f "$receipts_archive" ]; then
  echo "no receipts archive for this backup; receipts directory left unchanged" >&2
  restore_receipts=0
fi

export PGPASSWORD=$POSTGRES_PASSWORD
pg_restore --list "$backup_file" >/dev/null 2>&1
pg_restore \
  --host "$POSTGRES_HOST" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  --exit-on-error \
  "$backup_file" >/dev/null 2>&1

if [ "$restore_receipts" -eq 1 ]; then
  mkdir -p "$RECEIPTS_DIR"
  # Replace the live receipts tree with the archived copy for this dump.
  find "$RECEIPTS_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  tar -xzf "$receipts_archive" -C "$RECEIPTS_DIR"
fi

echo "Restore completed"
