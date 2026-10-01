import re

from django.utils.html import strip_tags

CLAIM_COMPROMISED = (
    "That setup token was already used or is not valid. "
    "If you did not claim it in this app, treat it as compromised and disable it at SimpleFIN."
)

_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


def claim_compromised_message():
    return CLAIM_COMPROMISED


def sanitize_provider_message(message: str) -> str:
    """Make a protocol error msg safe to show. Never keep URLs (they could be access URLs)."""
    text = strip_tags(str(message or ""))
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
