#!/bin/sh
set -eu

schedule=${BACKUP_CRON:-0 2 * * *}
field_count=$(printf '%s\n' "$schedule" | awk 'NF { lines += 1; fields = NF } END { if (lines == 1) print fields; else print 0 }')
if [ "$field_count" -ne 5 ]; then
  echo "BACKUP_CRON must be one five-field cron schedule" >&2
  exit 2
fi

umask 077
printf '%s %s\n' "$schedule" 'OPERATOR_AUDIT_ACTOR=scheduler /opt/financial-planner/backup.sh >>/proc/1/fd/1 2>>/proc/1/fd/2' > /etc/crontabs/root

echo "Backup scheduler started with TZ=${TZ:-UTC} and schedule: $schedule"
exec crond -f -l 2
