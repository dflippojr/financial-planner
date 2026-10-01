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


def provider_errors(payload: dict) -> list[str]:
    messages = []
    errlist = payload.get("errlist") or []
    if isinstance(errlist, list):
        for item in errlist:
            if isinstance(item, dict) and item.get("msg"):
                messages.append(sanitize_provider_message(item["msg"]))
            elif isinstance(item, str):
                messages.append(sanitize_provider_message(item))
    deprecated = payload.get("errors") or []
    if isinstance(deprecated, list):
        for item in deprecated:
            if isinstance(item, str):
                messages.append(sanitize_provider_message(item))
    return [msg for msg in messages if msg]


class SimpleFinError(Exception):
    """User-facing SimpleFIN failure that must not contain tokens or access URLs."""
