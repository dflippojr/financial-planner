#!/bin/sh
# Daily SimpleFIN sync scheduler. Same five-field cron shape as the backup container.
set -eu

schedule=${SIMPLEFIN_SYNC_CRON:-30 6 * * *}
field_count=$(printf '%s\n' "$schedule" | awk 'NF { lines += 1; fields = NF } END { if (lines == 1) print fields; else print 0 }')
if [ "$field_count" -ne 5 ]; then
  echo "SIMPLEFIN_SYNC_CRON must be one five-field cron schedule" >&2
  exit 2
fi

echo "SimpleFIN sync scheduler started with TZ=${TZ:-UTC} and schedule: $schedule"
exec python /app/scripts/simplefin_sync_loop.py
