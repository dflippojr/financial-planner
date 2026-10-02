"""Sleep until SIMPLEFIN_SYNC_CRON matches local time, then run sync_simplefin."""

import os
import sys
import time
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_planner.settings")
django.setup()

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from finance.simplefin_schedule import next_scheduled_sync, parse_five_field_cron, seconds_until


def main():
    expression = settings.SIMPLEFIN_SYNC_CRON
    parse_five_field_cron(expression)
    due = next_scheduled_sync(expression)
    while True:
        now = timezone.localtime()
        wait = seconds_until(due, now)
        if wait > 0:
            time.sleep(wait)
            continue
        call_command("sync_simplefin")
        due = next_scheduled_sync(expression, due)


if __name__ == "__main__":
    main()
