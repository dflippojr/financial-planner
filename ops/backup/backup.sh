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

sanitize_error() {
  printf '%s' "${1:-}" | tr '\n\r' '  ' | cut -c1-180
}

load_status() {
  last_success_at=""
  dump_name=""
  size_bytes=""
  table_count=""
  last_error=""
  offsite_success_at=""
  offsite_error=""
  offsite_configured=""
  if [ ! -f "$status_file" ]; then
    return 0
  fi
  while IFS='=' read -r key value || [ -n "${key:-}" ]; do
    case "$key" in
      last_success_at) last_success_at=$value ;;
      dump_name) dump_name=$value ;;
      size_bytes) size_bytes=$value ;;
      table_count) table_count=$value ;;
      last_error) last_error=$value ;;
      offsite_success_at) offsite_success_at=$value ;;
      offsite_error) offsite_error=$value ;;
      offsite_configured) offsite_configured=$value ;;
    esac
  done < "$status_file"
}

write_status() {
  mkdir -p "$health_dir"
  chmod 755 "$health_dir" 2>/dev/null || true
  tmp="$status_file.tmp"
  umask 022
  cat > "$tmp" <<EOF
last_success_at=$last_success_at
dump_name=$dump_name
size_bytes=$size_bytes
table_count=$table_count
last_error=$last_error
offsite_success_at=$offsite_success_at
offsite_error=$offsite_error
offsite_configured=$offsite_configured
EOF
  mv "$tmp" "$status_file"
  chmod 644 "$status_file" 2>/dev/null || true
  umask 077
  status_written=1
}

fail_offsite() {
  offsite_error=$(sanitize_error "$1")
  last_error=""
  write_status
  echo "$offsite_error" >&2
  exit 1
}

fail_run() {
  last_error=$(sanitize_error "$1")
  write_status
  echo "$last_error" >&2
  exit 1
}

prune_backups() {
  directory=$1
  keep=$2
  pattern=$3
  find "$directory" -type f -name "$pattern" -print \
    | sort -r \
    | awk -v keep="$keep" 'NR > keep' \
    | while IFS= read -r expired; do
        rm -f -- "$expired"
      done
}

prune_remote() {
  prefix=$1
  keep=$2
  match=$3
  # Exit code 3 is rclone's "directory not found": nothing to prune yet.
  listing=$(rclone lsf --config "$rclone_config" --files-only "${remote_base}/${prefix}" 2>/dev/null)
  listed=$?
  if [ "$listed" -eq 3 ]; then
    return 0
  fi
  if [ "$listed" -ne 0 ]; then
    return 1
  fi
  expired=$(printf '%s\n' "$listing" | grep -E "$match" | sort -r | awk -v keep="$keep" 'NR > keep')
  failed=0
  for name in $expired; do
    rclone deletefile --config "$rclone_config" "${remote_base}/${prefix}/${name}" >/dev/null 2>&1 || failed=1
  done
  return "$failed"
}

copy_offsite() {
  src=$1
  dest=$2
  rclone copyto --config "$rclone_config" "$src" "$dest"
}

require_env POSTGRES_DB
require_env POSTGRES_USER
require_env POSTGRES_PASSWORD
require_env POSTGRES_HOST
require_env RECEIPTS_DIR

backup_root=${BACKUP_ROOT:-/backups}
nightly_retention=${NIGHTLY_RETENTION:-14}
weekly_retention=${WEEKLY_RETENTION:-8}
positive_integer NIGHTLY_RETENTION "$nightly_retention"
positive_integer WEEKLY_RETENTION "$weekly_retention"

offsite_remote=${OFFSITE_RCLONE_REMOTE:-}
offsite_recipient=${OFFSITE_AGE_RECIPIENT:-}
rclone_config=${RCLONE_CONFIG:-/config/rclone.conf}
remote_base=${offsite_remote%/}

nightly_dir="$backup_root/nightly"
weekly_dir="$backup_root/weekly"
health_dir="$backup_root/health"
status_file="$health_dir/status"
mkdir -p "$nightly_dir" "$weekly_dir" "$health_dir"
umask 077
status_written=0
load_status
if [ -n "$offsite_remote" ] || [ -n "$offsite_recipient" ]; then
  offsite_configured=1
else
  offsite_configured=0
  offsite_error=""
fi

timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
success_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
filename="financial_planner_${timestamp}.dump"
partial="$nightly_dir/.${filename}.partial"
encrypted=""
receipts_partial=""
receipts_encrypted=""
nightly="$nightly_dir/$filename"

cleanup() {
  code=$?
  rm -f "$partial"
  if [ -n "$receipts_partial" ]; then
    rm -f "$receipts_partial"
  fi
  if [ -n "$encrypted" ]; then
    rm -f "$encrypted"
  fi
  if [ -n "$receipts_encrypted" ]; then
    rm -f "$receipts_encrypted"
  fi
  if [ "$code" -ne 0 ] && [ "${status_written:-0}" != 1 ]; then
    last_error=$(sanitize_error "Backup run failed")
    write_status
  fi
  exit "$code"
}
trap cleanup EXIT HUP INT TERM

export PGPASSWORD=$POSTGRES_PASSWORD
dump_err="$backup_root/.dump.err"
rm -f "$dump_err"
if ! pg_dump \
  --host "$POSTGRES_HOST" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --format custom \
  --no-owner \
  --no-privileges \
  --file "$partial" 2>"$dump_err"
then
  err=$(cat "$dump_err" 2>/dev/null || true)
  rm -f "$dump_err"
  fail_run "${err:-pg_dump failed}"
fi
rm -f "$dump_err"

# Do not publish or prune around an unreadable dump.
list_err="$backup_root/.restore.err"
if ! table_list=$(pg_restore --list "$partial" 2>"$list_err"); then
  err=$(cat "$list_err" 2>/dev/null || true)
  rm -f "$list_err"
  fail_run "${err:-pg_restore --list failed}"
fi
rm -f "$list_err"
table_count=$(printf '%s\n' "$table_list" | grep -c 'TABLE DATA' || true)
mv "$partial" "$nightly"
chmod 600 "$nightly"
size_bytes=$(wc -c < "$nightly" | awk '{print $1}')

weekday=$(date '+%u')
weekly=""
if [ "$weekday" = "7" ] || [ "${BACKUP_FORCE_WEEKLY:-0}" = "1" ]; then
  weekly="$weekly_dir/$filename"
  cp "$nightly" "$weekly"
  chmod 600 "$weekly"
fi

mkdir -p "$RECEIPTS_DIR"
receipts_name="financial_planner_${timestamp}.receipts.tar.gz"
receipts_partial="$nightly_dir/.${receipts_name}.partial"
receipts_archive="$nightly_dir/$receipts_name"
if ! tar -C "$RECEIPTS_DIR" -czf "$receipts_partial" .; then
  fail_run "Receipts archive failed"
fi
mv "$receipts_partial" "$receipts_archive"
receipts_partial=""
chmod 600 "$receipts_archive"
if [ -n "$weekly" ]; then
  cp "$receipts_archive" "$weekly_dir/$receipts_name"
  chmod 600 "$weekly_dir/$receipts_name"
fi

prune_backups "$nightly_dir" "$nightly_retention" 'financial_planner_*.dump'
prune_backups "$weekly_dir" "$weekly_retention" 'financial_planner_*.dump'
prune_backups "$nightly_dir" "$nightly_retention" 'financial_planner_*.receipts.tar.gz'
prune_backups "$weekly_dir" "$weekly_retention" 'financial_planner_*.receipts.tar.gz'

last_success_at=$success_at
dump_name=$filename
last_error=""

if [ -n "$offsite_remote" ] || [ -n "$offsite_recipient" ]; then
  if [ -z "$offsite_remote" ] || [ -z "$offsite_recipient" ]; then
    fail_offsite "Off-site copy is incomplete: set both OFFSITE_RCLONE_REMOTE and OFFSITE_AGE_RECIPIENT"
  fi
  encrypted="$nightly_dir/.${filename}.age.partial"
  if ! age -r "$offsite_recipient" -o "$encrypted" "$nightly"; then
    fail_offsite "age encryption failed"
  fi
  if ! copy_offsite "$encrypted" "${remote_base}/nightly/${filename}.age"; then
    fail_offsite "Off-site upload failed"
  fi
  receipts_encrypted="$nightly_dir/.${receipts_name}.age.partial"
  if ! age -r "$offsite_recipient" -o "$receipts_encrypted" "$receipts_archive"; then
    fail_offsite "age encryption failed"
  fi
  if ! copy_offsite "$receipts_encrypted" "${remote_base}/nightly/${receipts_name}.age"; then
    fail_offsite "Off-site upload failed"
  fi
  if [ -n "$weekly" ]; then
    if ! copy_offsite "$encrypted" "${remote_base}/weekly/${filename}.age"; then
      fail_offsite "Off-site weekly upload failed"
    fi
    if ! copy_offsite "$receipts_encrypted" "${remote_base}/weekly/${receipts_name}.age"; then
      fail_offsite "Off-site weekly upload failed"
    fi
  fi
  rm -f "$encrypted"
  encrypted=""
  rm -f "$receipts_encrypted"
  receipts_encrypted=""
  offsite_success_at=$success_at
  offsite_error=""
  dump_age_match='^financial_planner_[0-9TZ]+\.dump\.age$'
  receipts_age_match='^financial_planner_[0-9TZ]+\.receipts\.tar\.gz\.age$'
  if ! prune_remote nightly "$nightly_retention" "$dump_age_match" \
    || ! prune_remote weekly "$weekly_retention" "$dump_age_match" \
    || ! prune_remote nightly "$nightly_retention" "$receipts_age_match" \
    || ! prune_remote weekly "$weekly_retention" "$receipts_age_match"; then
    fail_offsite "Off-site retention pruning failed"
  fi
fi

write_status
echo "Backup completed: $filename"
