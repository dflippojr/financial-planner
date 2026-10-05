"""Reject Agent Harness URLs that are not on this host or the tailnet."""

from __future__ import annotations

from urllib.parse import urlparse

# The adapter appends this itself; a URL copied from the harness API docs often ends in it.
API_PATH = "/api/v1"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
DOCKER_HOSTS = frozenset({"host.docker.internal"})
SAME_HOST = LOOPBACK_HOSTS | DOCKER_HOSTS


class HarnessUrlError(ValueError):
    pass


def parse_harness_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise HarnessUrlError("Enter an Agent Harness Server URL.")
    if not text.isascii() or any(ch.isspace() or not ch.isprintable() for ch in text):
        raise HarnessUrlError("The harness URL cannot include spaces or special characters.")
    try:
        parsed = urlparse(text)
        valid_port = parsed.port != 0
    except ValueError:
        valid_port = False
    if not valid_port:
        raise HarnessUrlError("That harness URL is not valid.")
    if parsed.scheme not in {"http", "https"}:
        raise HarnessUrlError("The harness URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise HarnessUrlError("The harness URL cannot include credentials.")
    if parsed.query or parsed.fragment:
        raise HarnessUrlError("The harness URL cannot include a query or fragment.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HarnessUrlError("The harness URL is missing a host.")
    if host.endswith(".ts.net"):
        if parsed.scheme != "https":
            raise HarnessUrlError("A tailnet harness must use HTTPS.")
        return _normalized(parsed)
    if host in SAME_HOST:
        return _normalized(parsed)
    raise HarnessUrlError(
        "Connect only an Agent Harness on this computer or on the tailnet."
    )


def _normalized(parsed) -> str:
    path = parsed.path.rstrip("/")
    if path.endswith(API_PATH):
        path = path[: -len(API_PATH)].rstrip("/")
    netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}{path}"
