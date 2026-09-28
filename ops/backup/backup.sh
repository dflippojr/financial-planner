#!/bin/sh
set -eu

require_env() {
  eval "value=\${$1:-}"
  if [ -z "$value" ]; then
    echo "$1 must be set" >&2
    exit 2
  fi
}

positive_integer() {
  case "$2" in
    ''|*[!0-9]*|0) echo "$1 must be a positive integer" >&2; exit 2 ;;
  esac
}

prune_backups() {
  directory=$1
  keep=$2
  find "$directory" -type f -name 'financial_planner_*.dump' -print \
    | sort -r \
    | awk -v keep="$keep" 'NR > keep' \
    | while IFS= read -r expired; do
        rm -f -- "$expired"
      done
}

require_env POSTGRES_DB
require_env POSTGRES_USER
require_env POSTGRES_PASSWORD
require_env POSTGRES_HOST

backup_root=${BACKUP_ROOT:-/backups}
nightly_retention=${NIGHTLY_RETENTION:-14}
weekly_retention=${WEEKLY_RETENTION:-8}
positive_integer NIGHTLY_RETENTION "$nightly_retention"
positive_integer WEEKLY_RETENTION "$weekly_retention"

nightly_dir="$backup_root/nightly"
weekly_dir="$backup_root/weekly"
mkdir -p "$nightly_dir" "$weekly_dir"
umask 077

timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
filename="financial_planner_${timestamp}.dump"
partial="$nightly_dir/.${filename}.partial"
nightly="$nightly_dir/$filename"
trap 'rm -f "$partial"' EXIT HUP INT TERM

export PGPASSWORD=$POSTGRES_PASSWORD
pg_dump \
  --host "$POSTGRES_HOST" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --format custom \
  --no-owner \
  --no-privileges \
  --file "$partial"

# Do not publish or prune around an unreadable dump.
pg_restore --list "$partial" >/dev/null
mv "$partial" "$nightly"
trap - EXIT HUP INT TERM

weekday=$(date '+%u')
if [ "$weekday" = "7" ] || [ "${BACKUP_FORCE_WEEKLY:-0}" = "1" ]; then
  weekly="$weekly_dir/$filename"
  cp "$nightly" "$weekly"
  chmod 600 "$weekly"
fi

prune_backups "$nightly_dir" "$nightly_retention"
prune_backups "$weekly_dir" "$weekly_retention"

echo "Backup completed: $filename"
