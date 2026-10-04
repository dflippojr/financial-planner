import mimetypes
import os
import tempfile
from pathlib import Path

mimetypes.add_type("application/manifest+json", ".webmanifest")

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
    "finance.middleware.MemberSessionActivityMiddleware",
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
                "finance.context_processors.privacy_policy_prompt",
                "finance.context_processors.ai_features",
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
# Follow the deployment TZ (also used by the backup and SimpleFIN schedulers).
TIME_ZONE = os.environ.get("TZ") or "America/New_York"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
WHITENOISE_MIMETYPES = {".webmanifest": "application/manifest+json"}
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
# True only when the app port is loopback-only behind a trusted proxy such as
# Tailscale Serve. When on, client IP comes from the right-most X-Forwarded-For
# hop the proxy added; when off, X-Forwarded-For is ignored.
TRUST_PROXY_FORWARDED_FOR = os.environ.get("TRUST_PROXY_FORWARDED_FOR", "false").lower() == "true"
SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "true").lower() == "true"
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]

SETUP_CODE = os.environ.get("SETUP_CODE", "")
# File or directory. A directory must contain privacy-policy.md. Empty uses the
# default template shipped in finance/policy/default.md.
PRIVACY_POLICY_PATH = os.environ.get("PRIVACY_POLICY_PATH", "").strip()
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
REAUTH_WINDOW_SECONDS = int(os.environ.get("REAUTH_WINDOW_SECONDS", "600"))
GOOGLE_REAUTH_MAX_AGE_SECONDS = int(os.environ.get("GOOGLE_REAUTH_MAX_AGE_SECONDS", "300"))

# Transfer pairing (issue #8). High confidence means each leg has exactly one
# counterpart in the window. Override per household when a window is stored there.
TRANSFER_MATCH_WINDOW_DAYS = int(os.environ.get("TRANSFER_MATCH_WINDOW_DAYS", "5"))

# Receipt images and PDFs. Never place this directory under STATIC_ROOT or a
# public media URL; files are served only through an access-checked view.
RECEIPTS_DIR = os.environ.get(
    "RECEIPTS_DIR",
    str(Path(BASE_DIR) / "data" / "receipts"),
)
RECEIPT_MAX_BYTES = 10 * 1024 * 1024
RECEIPT_MAX_PER_TRANSACTION = 5

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

# Fernet key for SimpleFIN access URLs and AI App tokens. Required to connect
# or sync; collectstatic and other management commands can start without it.
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "").strip()
SIMPLEFIN_SYNC_CRON = os.environ.get("SIMPLEFIN_SYNC_CRON", "30 6 * * *").strip()
SIMPLEFIN_SYNC_MIN_INTERVAL_SECONDS = int(os.environ.get("SIMPLEFIN_SYNC_MIN_INTERVAL_SECONDS", "900"))

AGENT_HARNESS_PROJECT = os.environ.get("AGENT_HARNESS_PROJECT", "financial-planner").strip() or "financial-planner"
AGENT_HARNESS_HOSTED_SESSIONS = os.environ.get("AGENT_HARNESS_HOSTED_SESSIONS", "false").lower() == "true"
AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = int(os.environ.get("AGENT_HARNESS_SESSION_TIMEOUT_SECONDS", "600"))
AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS = int(os.environ.get("AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS", "120"))
AI_LOCAL_QUIET_WINDOW = os.environ.get("AI_LOCAL_QUIET_WINDOW", "22:00-06:00").strip()
AI_JOB_POLL_SECONDS = int(os.environ.get("AI_JOB_POLL_SECONDS", "15"))
AI_JOB_MAX_ATTEMPTS = int(os.environ.get("AI_JOB_MAX_ATTEMPTS", "5"))
AI_JOB_RESUME_DELAY_SECONDS = int(os.environ.get("AI_JOB_RESUME_DELAY_SECONDS", "300"))
AI_JOB_RESUME_MAX_AGE_SECONDS = int(os.environ.get("AI_JOB_RESUME_MAX_AGE_SECONDS", "86400"))
AI_CHAT_EXPIRE_DAYS = int(os.environ.get("AI_CHAT_EXPIRE_DAYS", "30"))
AI_CHAT_MAX_TURNS = int(os.environ.get("AI_CHAT_MAX_TURNS", "20"))
AI_CHAT_MAX_TOOL_CALLS = int(os.environ.get("AI_CHAT_MAX_TOOL_CALLS", "40"))
AI_CHAT_LOCAL_ENABLED = os.environ.get("AI_CHAT_LOCAL_ENABLED", "false").lower() == "true"
AI_SHARED_LOCAL_DAILY_CAP = int(os.environ.get("AI_SHARED_LOCAL_DAILY_CAP", "200"))

# Written by ops/backup/backup.sh and mounted read-only into the app.
BACKUP_STATUS_PATH = os.environ.get("BACKUP_STATUS_PATH", "/backup-health/status").strip() or "/backup-health/status"
OPERATOR_USERNAMES = os.environ.get("OPERATOR_USERNAMES", "").strip()

# Handle uploads in memory only. Django's default handlers write any upload over
# 2.5 MB to a temporary file in /tmp before application code runs, which would put
# a real bank export on disk even though staging itself is memory-backed, and it
# does so at any size. With only the memory handler, a file larger than the
# threshold is dropped instead of written anywhere. The threshold sits above the
# 10 MB receipt cap (and the 12 MB CSV oversize check) so those files still reach
# application code; CSV imports keep a separate 5 MB cap.
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]
FILE_UPLOAD_MAX_MEMORY_SIZE = 13 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 13 * 1024 * 1024
