import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-with-enough-entropy-not-for-production-12345")

from .settings import *  # noqa: F403  # NOSONAR python:S2208 -- Django test overlay of production settings

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


# The default is a fast in-memory SQLite database. It is not the production
# engine and hides PostgreSQL-only behavior (for example, SELECT ... FOR UPDATE
# is silently ignored). Set FINANCIAL_PLANNER_TEST_DB=postgres to run the
# suite against the PostgreSQL connection configured in settings.py; see
# scripts/test_postgres.sh.
(BASE_DIR / "staticfiles").mkdir(exist_ok=True)
if os.environ.get("FINANCIAL_PLANNER_TEST_DB") != "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
