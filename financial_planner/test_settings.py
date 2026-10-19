import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-with-enough-entropy-not-for-production-12345")
os.environ.setdefault("FIELD_ENCRYPTION_KEY", "dGVzdC1maWVsZC1lbmNyeXB0aW9uLWtleS0zMmJ5dGU=")

from .settings import *  # noqa: F403  # NOSONAR python:S2208 -- Django test overlay of production settings
from .settings import BASE_DIR

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

# Django's default hasher (PBKDF2, about half a second per hash) made password
# hashing most of the suite's run time. Test passwords are synthetic, and the
# production settings keep the default (tests/test_fast_password_hasher.py).
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Never contact SMTP from the test suite, even if operator variables are set.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST = ""

# Never contact an ntfy server from the test suite, even if operator variables are set.
NTFY_URL = ""
NTFY_TOKEN = ""
NTFY_TRANSPORT = "fake"
