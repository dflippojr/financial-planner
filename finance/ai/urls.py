"""Accept only Agent Harness URLs on an origin the operator allowed (#409)."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# The adapter appends this itself; a URL copied from the harness API docs often ends in it.
API_PATH = "/api/v1"
DEFAULT_PORTS = {"http": 80, "https": 443}
HARNESS_DISABLED = "Agent Harness isn't enabled on this server. The operator sets AGENT_HARNESS_ALLOWED_ORIGINS."
ORIGIN_NOT_ALLOWED = "Connect only the Agent Harness this server allows. Ask the operator for its address."


class HarnessUrlError(ValueError):
    pass


def allowed_origins():
    """The operator's (scheme, host, port) allow-list. Empty disables the harness integration."""
    origins = set()
    for entry in getattr(settings, "AGENT_HARNESS_ALLOWED_ORIGINS", ()):
        try:
            parsed = _parse(entry)
        except HarnessUrlError:
            raise ImproperlyConfigured(f"AGENT_HARNESS_ALLOWED_ORIGINS has an invalid origin: {entry!r}") from None
        if _path(parsed):
            raise ImproperlyConfigured(f"AGENT_HARNESS_ALLOWED_ORIGINS entries are origins without a path: {entry!r}")
        origins.add(_origin(parsed))
    return frozenset(origins)


def harness_enabled():
    return bool(allowed_origins())


def is_allowed_url(url) -> bool:
    """Request-time guard: a stored or derived URL still has to be on an allowed origin."""
    try:
        parsed = urlparse(url or "")
        if parsed.scheme not in DEFAULT_PORTS or not parsed.hostname or parsed.username or parsed.password:
            return False
        return _origin(parsed) in allowed_origins()
    except ValueError:
        return False


def parse_harness_url(raw: str) -> str:
    allowed = allowed_origins()
    if not allowed:
        raise HarnessUrlError(HARNESS_DISABLED)
    parsed = _parse(raw)
    if _origin(parsed) not in allowed:
        raise HarnessUrlError(ORIGIN_NOT_ALLOWED)
    if _path(parsed):
        raise HarnessUrlError("Use the harness's own address, without a page path.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _parse(raw):
    text = (raw or "").strip()
    if not text:
        raise HarnessUrlError("Enter an Agent Harness Server URL.")
    if not text.isascii() or any(ch.isspace() or not ch.isprintable() for ch in text):
        raise HarnessUrlError("The harness URL cannot include spaces or special characters.")
    try:
        parsed = urlparse(text)
        if parsed.port == 0:
            raise ValueError("port 0")
    except ValueError:
        raise HarnessUrlError("That harness URL is not valid.") from None
    if parsed.scheme not in DEFAULT_PORTS:
        raise HarnessUrlError("The harness URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise HarnessUrlError("The harness URL cannot include credentials.")
    if parsed.query or parsed.fragment:
        raise HarnessUrlError("The harness URL cannot include a query or fragment.")
    if not parsed.hostname:
        raise HarnessUrlError("The harness URL is missing a host.")
    return parsed


def _origin(parsed):
    return parsed.scheme, parsed.hostname.lower(), parsed.port or DEFAULT_PORTS[parsed.scheme]


def _path(parsed):
    path = parsed.path.rstrip("/")
    if path.endswith(API_PATH):
        path = path[: -len(API_PATH)].rstrip("/")
    return path
