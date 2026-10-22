"""compose's one-shot config-check: refuse what would stop background starting.

background depends on this service completing successfully, so an invalid
SIMPLEFIN_SYNC_CRON fails `docker compose up` with this message instead of
starting background into a restart loop. app does not wait for it (#560).
"""

import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_planner.settings")


def main():
    django.setup()
    from finance.simplefin_schedule import validate_sync_schedule

    try:
        validate_sync_schedule()
    except ValueError as error:
        print(f"ValueError: {error}; background will not start until it is fixed", file=sys.stderr)
        return 1
    print("config-check passed: background can start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
