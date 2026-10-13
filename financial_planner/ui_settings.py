"""Isolated development settings used by scripts/ui_instance.py."""
import os
from copy import deepcopy
from pathlib import Path

instance = Path(os.environ["FINANCIAL_PLANNER_UI_DIR"])
if not instance.is_absolute() or not instance.name.startswith("financial-planner-ui-"):
    raise RuntimeError("A disposable UI directory is required.")

from .settings import *  # noqa: F403 -- isolated development overlay

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": instance / "db.sqlite3"}}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST = ""
# Offer the ntfy channel, but keep every message in memory.
NTFY_URL = "http://ntfy.invalid"
NTFY_TOKEN = ""
NTFY_TRANSPORT = "fake"
LOGGING = deepcopy(LOGGING)
LOGGING["handlers"]["console"]["stream"] = "ext://sys.stderr"
