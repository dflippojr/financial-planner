import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-with-enough-entropy-not-for-production-12345")

from .settings import *  # noqa: F403

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
