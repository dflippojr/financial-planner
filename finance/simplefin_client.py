"""HTTP client for the SimpleFIN protocol. Access URLs never appear in errors."""

from __future__ import annotations

import base64
from http.client import HTTPException, HTTPSConnection
import io
import ipaddress
import json
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from finance.simplefin_errors import SimpleFinError, claim_compromised_message

# Per-read timeouts; the deadlines bound a whole request, however slowly the
# provider trickles bytes.
CLAIM_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 60
CLAIM_DEADLINE_SECONDS = 60
FETCH_DEADLINE_SECONDS = 180
MAX_CLAIM_BYTES = 8 * 1024
MAX_FETCH_BYTES = 25 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
_SSL = ssl.create_default_context()

NOT_FINISHED = "SimpleFIN did not finish responding. Try again later."
TOO_LARGE = "SimpleFIN returned a response larger than this app accepts."
UNREADABLE = "SimpleFIN returned a response that could not be read."
DISALLOWED_ADDRESS = "The SimpleFIN address does not resolve to a public internet address."


class DisallowedAddress(OSError):
    """The host resolved to an address outside the public internet."""


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def vetted_address(host: str, port: int):
    """Resolve host once and return a sockaddr, refusing any non-public answer.

    Every answer must be public: a mixed answer could otherwise reach an
    internal service on a retry. The caller connects to the returned address,
    so a second lookup can never swap in a different one.
    """
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except UnicodeError as exc:
        raise OSError("host name could not be encoded") from exc
    if not answers or not all(_public_address(answer[4][0]) for answer in answers):
        raise DisallowedAddress("non-public address")
    return answers[0][4]


class _DeadlineReader(io.RawIOBase):
    """Socket reads whose timeout never runs past the request's deadline.

    Reads go through the socket's own unbuffered file, which keeps the socket
    open after urllib closes the connection object, as a normal response does.
    """

    def __init__(self, sock, deadline, timeout):
        super().__init__()
        self._sock = sock
        self._raw = sock.makefile("rb", buffering=0)
        self._deadline = deadline
        self._timeout = timeout

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("SimpleFIN request deadline passed")
        self._sock.settimeout(min(self._timeout, remaining))
        return self._raw.readinto(buffer)

    def close(self):
        if not self.closed:
            self._raw.close()
        super().close()


class _DeadlineSocket:
    """Wrap a connected socket so every response read honors the deadline."""

    def __init__(self, sock, deadline, timeout):
        self._sock = sock
        self._deadline = deadline
        self._timeout = timeout

    def makefile(self, mode="r", *args, **kwargs):
        if "w" in mode:
            return self._sock.makefile(mode, *args, **kwargs)
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline, self._timeout))

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _GuardedHTTPSConnection(HTTPSConnection):
    """HTTPS to a vetted public address, within a total deadline."""

    def __init__(self, host, *, deadline, **kwargs):
        super().__init__(host, **kwargs)
        self._deadline = deadline

    def connect(self):
        address = vetted_address(self.host, self.port)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("SimpleFIN request deadline passed")
        timeout = min(self.timeout, remaining)
        raw = socket.create_connection(address[:2], timeout=timeout)
        try:
            # Certificate checks still use the host name, not the address.
            secure = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        self.sock = _DeadlineSocket(secure, self._deadline, self.timeout)


class _GuardedHTTPSHandler(HTTPSHandler):
    def __init__(self, context, deadline):
        super().__init__(context=context)
        self._deadline = deadline

    def https_open(self, req):
        deadline = self._deadline

        def connection(host, **kwargs):
            return _GuardedHTTPSConnection(host, deadline=deadline, **kwargs)

        return self.do_open(connection, req, context=self._context)


class _RefuseRedirects(HTTPRedirectHandler):
    """Never follow redirects: the request carries SimpleFIN credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout=None, context=None, deadline=None):
    """Open a request to a public address without following redirects (a 3xx becomes HTTPError).

    No proxy is used, so the address check applies to the real destination.
    """
    deadline = deadline if deadline is not None else time.monotonic() + FETCH_DEADLINE_SECONDS
    opener = build_opener(ProxyHandler({}), _GuardedHTTPSHandler(context or _SSL, deadline), _RefuseRedirects())
    return opener.open(request, timeout=timeout)


def read_capped(response, *, limit, deadline) -> bytes:
    """Read the body in chunks, failing past `limit` bytes or the deadline."""
    chunks = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise SimpleFinError(NOT_FINISHED)
        chunk = response.read(min(READ_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise SimpleFinError(TOO_LARGE)
        chunks.append(chunk)


def _unreachable(exc: URLError) -> SimpleFinError:
    if isinstance(exc.reason, DisallowedAddress):
        return SimpleFinError(DISALLOWED_ADDRESS)
    return SimpleFinError("SimpleFIN could not be reached.")


def _require_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise SimpleFinError("The SimpleFIN address must use HTTPS.")


def _split_credentials(access_url: str):
    """Return the Access URL without userinfo, plus a Basic Authorization header.

    SimpleFIN Access URLs carry credentials as https://user:pass@host/path.
    urllib does not send URL userinfo, so the credentials go in an explicit
    header and are removed from the URL that is requested.
    """
    parts = urlsplit(access_url)
    if parts.username is None:
        return access_url, {}
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    bare_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode("utf-8")
    return bare_url, {"Authorization": "Basic " + base64.b64encode(credentials).decode("ascii")}


def claim_access_url(claim_url: str) -> str:
    """POST the claim URL once. Returns the Access URL. Never includes it in errors."""
    _require_https(claim_url)
    request = Request(claim_url, method="POST", data=b"")
    deadline = time.monotonic() + CLAIM_DEADLINE_SECONDS
    try:
        with urlopen(request, timeout=CLAIM_TIMEOUT_SECONDS, context=_SSL, deadline=deadline) as response:
            body = read_capped(response, limit=MAX_CLAIM_BYTES, deadline=deadline).decode("utf-8").strip()
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SimpleFinError("SimpleFIN returned an unexpected redirect. Nothing was sent elsewhere.") from None
        if exc.code == 403:
            raise SimpleFinError(claim_compromised_message()) from None
        raise SimpleFinError("SimpleFIN could not claim that setup token.") from None
    except URLError as exc:
        raise _unreachable(exc) from None
    except (OSError, HTTPException, UnicodeDecodeError):  # TimeoutError, IncompleteRead
        raise SimpleFinError(NOT_FINISHED) from None
    _require_https(body)
    return body


def fetch_accounts(access_url: str, *, start_date=None, end_date=None, balances_only=False):
    """GET {access_url}/accounts. Returns a parsed Account Set dict."""
    _require_https(access_url)
    params = {"version": "2"}
    if start_date is not None:
        params["start-date"] = str(int(start_date))
    if end_date is not None:
        params["end-date"] = str(int(end_date))
    if balances_only:
        params["balances-only"] = "1"
    bare_url, headers = _split_credentials(access_url)
    url = bare_url.rstrip("/") + "/accounts?" + urlencode(params)
    request = Request(url, method="GET", headers=headers)
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS, context=_SSL, deadline=deadline) as response:
            raw = read_capped(response, limit=MAX_FETCH_BYTES, deadline=deadline).decode("utf-8")
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SimpleFinError("SimpleFIN returned an unexpected redirect. Nothing was sent elsewhere.") from None
        if exc.code == 403:
            raise SimpleFinError(
                "SimpleFIN access was denied. Reconnect if access was revoked.",
                access_denied=True,
            ) from None
        if exc.code == 402:
            raise SimpleFinError("SimpleFIN reported that payment is required.") from None
        raise SimpleFinError("SimpleFIN could not return accounts.") from None
    except URLError as exc:
        raise _unreachable(exc) from None
    except (OSError, HTTPException, UnicodeDecodeError):  # TimeoutError, IncompleteRead
        # Headers arrived but the body stalled or broke off.
        raise SimpleFinError(NOT_FINISHED) from None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):  # JSONDecodeError, integer digit limit, deep nesting
        raise SimpleFinError(UNREADABLE) from None
    if not isinstance(payload, dict):
        raise SimpleFinError(UNREADABLE)
    return payload
