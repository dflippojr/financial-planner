import os
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set.")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"


def allowed_hosts_from_env(value):
    """Split DJANGO_ALLOWED_HOSTS the same way the container health probe does.

    A wrapped env file can leave a space after `DJANGO_ALLOWED_HOSTS=`. The
    probe strips that space for its Host header; Django must allow the same
    host or the container is reported unhealthy while serving real requests.
    """
    return [host.strip() for host in value.split(",") if host.strip()]


ALLOWED_HOSTS = allowed_hosts_from_env(
    os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "finance.apps.FinanceConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "finance.middleware.LoginRequiredExceptStaticMiddleware",
]
ROOT_URLCONF = "financial_planner.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "finance.context_processors.google_signin",
                "finance.context_processors.navigation",
            ],
        },
    }
]
WSGI_APPLICATION = "financial_planner.wsgi.application"

if os.environ.get("FINANCIAL_PLANNER_TEST_SQLITE") == "1":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / ".test.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "financial_planner"),
            "USER": os.environ.get("POSTGRES_USER", "financial_planner"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
SESSION_COOKIE_AGE = int(os.environ.get("DJANGO_SESSION_COOKIE_AGE", 60 * 60 * 24 * 28))
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = os.environ.get("DJANGO_SECURE_COOKIES", "true").lower() == "true"
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "true").lower() == "true"
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]

SETUP_CODE = os.environ.get("SETUP_CODE", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

ACCOUNT_ADAPTER = "finance.allauth_adapters.MemberAccountAdapter"
SOCIALACCOUNT_ADAPTER = "finance.allauth_adapters.MemberSocialAccountAdapter"
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_LOGIN_METHODS = {"username"}
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https" if SECURE_SSL_REDIRECT else "http"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_ONLY = False
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["openid", "email", "profile"],
        "AUTH_PARAMS": {"access_type": "online"},
        "OAUTH_PKCE_ENABLED": True,
        **(
            {
                "APP": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "secret": GOOGLE_CLIENT_SECRET,
                }
            }
            if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
            else {}
        ),
    }
}
INVITATION_TTL_HOURS = int(os.environ.get("INVITATION_TTL_HOURS", "48"))
LOGIN_FAILURE_LIMIT = int(os.environ.get("LOGIN_FAILURE_LIMIT", "5"))
LOGIN_FAILURE_WINDOW_SECONDS = int(os.environ.get("LOGIN_FAILURE_WINDOW_SECONDS", "900"))
LOGIN_BLOCK_SECONDS = int(os.environ.get("LOGIN_BLOCK_SECONDS", "900"))

# Transfer pairing (issue #8). High confidence means each leg has exactly one
# counterpart in the window. Override per household when a window is stored there.
TRANSFER_MATCH_WINDOW_DAYS = int(os.environ.get("TRANSFER_MATCH_WINDOW_DAYS", "5"))

# Uploaded CSVs are short-lived, private staging data. Keep the default outside
# the repository and allow deployments to place it on an appropriate local disk.
CSV_IMPORT_STAGING_DIR = os.environ.get(
    "CSV_IMPORT_STAGING_DIR",
    str(Path(tempfile.gettempdir()) / "financial-planner-csv-imports"),
)
CSV_IMPORT_STAGE_TTL_SECONDS = max(
    1,
    min(int(os.environ.get("CSV_IMPORT_STAGE_TTL_SECONDS", "3600")), 3600),
)

# Handle uploads in memory only. Django's default handlers write any upload over
# 2.5 MB to a temporary file in /tmp before application code runs, which would put
# a real bank export on disk even though staging itself is memory-backed, and it
# does so at any size. With only the memory handler, a file larger than the
# threshold is dropped instead of written anywhere. The threshold sits a little
# above the 5 MB import cap so a file just over the cap still gets the specific
# "exceeds the 5 MB limit" message; anything larger gets the form's generic one.
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]
FILE_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
