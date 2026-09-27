from .settings import *  # noqa: F403

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
