#!/usr/bin/env python
"""Poll and run due AI background jobs. Does not serve HTTP."""

import os
import sys
import time
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_planner.settings")
django.setup()

from django.conf import settings

from finance.ai_jobs import process_due_jobs


def main():
    poll = max(1, int(getattr(settings, "AI_JOB_POLL_SECONDS", 15)))
    while True:
        process_due_jobs()
        time.sleep(poll)


if __name__ == "__main__":
    main()
