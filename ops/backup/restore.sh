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
dump_name=$(basename "$backup_file")
receipts_archive="${stem}.receipts.tar.gz"
receipts_name=$(basename "$receipts_archive")
manifest="${stem}.sha256"
signing_namespace=financial-planner-backup
public_key=${BACKUP_SIGNING_PUBLIC_KEY:-${BACKUP_ROOT:-/backups}/signing/backup-signing-key.pub}
allow_unverified=${RESTORE_ALLOW_UNVERIFIED:-0}
extract_script=$(dirname "$0")/extract_receipts.py
work_dir=""
cleanup() {
  code=$?
  if [ -n "$work_dir" ]; then
    rm -rf "$work_dir"
  fi
  audit_end "$code"
  trap - EXIT
  exit "$code"
}
trap cleanup EXIT

refuse() {
  if [ "$allow_unverified" = 1 ]; then
    echo "WARNING: $1; restoring anyway because RESTORE_ALLOW_UNVERIFIED=1" >&2
    return 0
  fi
  echo "Restore refused: $1. Set RESTORE_ALLOW_UNVERIFIED=1 only for a file you trust (docs/deployment.md)" >&2
  exit 1
}

sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

# A manifest names each file once by base name. Print the digest it records
# for $1, or nothing.
manifest_digest() {
  awk -v name="$1" '$2 == name || $2 == "*" name { print $1 }' "$manifest_copy"
}

# Verify a copy of the manifest, so the file checked is the file read.
verify_manifest() {
  if [ ! -f "$manifest" ] || [ ! -f "$manifest.sig" ]; then
    refuse "no signed manifest for this backup"
    return 1
  fi
  if [ ! -f "$public_key" ]; then
    refuse "the backup signing public key is missing"
    return 1
  fi
  work_dir=$(mktemp -d)
  manifest_copy="$work_dir/manifest"
  cp "$manifest" "$manifest_copy"
  printf '%s %s\n' "$signing_namespace" "$(cat "$public_key")" > "$work_dir/allowed_signers"
  if ! ssh-keygen -Y verify -f "$work_dir/allowed_signers" -I "$signing_namespace" \
    -n "$signing_namespace" -s "$manifest.sig" < "$manifest_copy" >/dev/null 2>&1; then
    refuse "the manifest signature does not verify"
    return 1
  fi
  return 0
}

if verify_manifest; then
  expected=$(manifest_digest "$dump_name")
  if [ -z "$expected" ] || [ "$expected" != "$(sha256_of "$backup_file")" ]; then
    refuse "the dump does not match its signed manifest"
  fi
  expected=$(manifest_digest "$receipts_name")
  if [ -f "$receipts_archive" ]; then
    if [ -z "$expected" ] || [ "$expected" != "$(sha256_of "$receipts_archive")" ]; then
      refuse "the receipts archive does not match its signed manifest"
    fi
  elif [ -n "$expected" ]; then
    refuse "the signed manifest lists a receipts archive that is missing"
  fi
fi

restore_receipts=1
if [ ! -f "$receipts_archive" ]; then
  echo "no receipts archive for this backup; receipts directory left unchanged" >&2
  restore_receipts=0
elif ! "$audit_python" "$extract_script" check "$receipts_archive"; then
  exit 1
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
  "$audit_python" "$extract_script" extract "$receipts_archive" "$RECEIPTS_DIR"
fi

echo "Restore completed"
