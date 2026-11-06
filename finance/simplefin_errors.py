import re
from urllib.parse import quote

from django.utils.html import strip_tags

CLAIM_COMPROMISED = (
    "That setup token was already used or is not valid. "
    "If you did not claim it in this app, treat it as compromised and disable it at SimpleFIN."
)

_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


def claim_compromised_message():
    return CLAIM_COMPROMISED


def clean_text(value) -> str:
    """Provider text as storable UTF-8: lone surrogates become "?" and NULs are dropped."""
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")


def sanitize_provider_message(message: str) -> str:
    """Make a protocol error msg safe to show. Never keep URLs (they could be access URLs)."""
    text = strip_tags(clean_text(message or ""))
    text = _URL_IN_TEXT.sub("[redacted]", text)
    text = " ".join(text.split())
    return text[:300]


def _from_errlist(errlist) -> list[str]:
    messages = []
    if not isinstance(errlist, list):
        return messages
    for item in errlist:
        if isinstance(item, dict) and item.get("msg"):
            messages.append(sanitize_provider_message(item["msg"]))
        elif isinstance(item, str):
            messages.append(sanitize_provider_message(item))
    return messages


def _from_deprecated_errors(errors) -> list[str]:
    if not isinstance(errors, list):
        return []
    return [sanitize_provider_message(item) for item in errors if isinstance(item, str)]


def remote_account_key(item: dict) -> str:
    """Stable key for a SimpleFIN account.

    Account ids are unique only within one provider connection, so the key
    includes the connection id when SimpleFIN reports one.
    """
    # Percent-encode each part so a ':' inside an id can never make two
    # different accounts share one key.
    account_id = quote(clean_text(item["id"]), safe="")
    conn_id = quote(clean_text(item.get("conn_id") or ""), safe="")
    return f"{conn_id}:{account_id}" if conn_id else account_id


def is_api_usage_notice(item) -> bool:
    """A `gen.api` item naming no account or connection: advice to the developer, not missing data (#629).

    The protocol: "Error in how the API is being used. This is meant for the
    developer and not the user." Bridge sends it when the requested date range
    passes 45 days (recommended) or 90 days (capped).
    """
    return (
        isinstance(item, dict)
        and str(item.get("code") or "").strip().lower() == "gen.api"
        and not (item.get("account_id") or item.get("conn_id"))
    )


def provider_errors(payload: dict) -> list[str]:
    messages = _from_errlist(payload.get("errlist") or [])
    messages.extend(_from_deprecated_errors(payload.get("errors") or []))
    return [msg for msg in messages if msg]


class SimpleFinError(Exception):
    """User-facing SimpleFIN failure that must not contain tokens or access URLs.

    access_denied is True only when SimpleFIN refused the credentials (HTTP
    403), which is the one failure that should stop scheduled syncs.
    """

    def __init__(self, message="", *, access_denied=False):
        super().__init__(message)
        self.access_denied = access_denied


class SimpleFinRateLimited(SimpleFinError):
    """A manual sync was refused because the last one was too recent."""


class SimpleFinAlreadyClaimed(SimpleFinError):
    """The member is already connected, or another request is claiming this token."""
