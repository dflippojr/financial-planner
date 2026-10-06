"""Bootstrap Django once, then start the supervised background lanes."""

import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_planner.settings")

if __name__ == "__main__":
    django.setup()
    from finance.background import main

    main()
