"""HTTP client for the SimpleFIN protocol. Access URLs never appear in errors."""

from __future__ import annotations

import base64
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from finance.simplefin_errors import SimpleFinError, claim_compromised_message

CLAIM_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 60
_SSL = ssl.create_default_context()


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
    try:
        with urlopen(request, timeout=CLAIM_TIMEOUT_SECONDS, context=_SSL) as response:
            body = response.read().decode("utf-8").strip()
    except HTTPError as exc:
        if exc.code == 403:
            raise SimpleFinError(claim_compromised_message()) from None
        raise SimpleFinError("SimpleFIN could not claim that setup token.") from None
    except URLError:
        raise SimpleFinError("SimpleFIN could not be reached.") from None
    except (TimeoutError, OSError, UnicodeDecodeError):
        raise SimpleFinError("SimpleFIN did not finish responding. Try again later.") from None
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
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS, context=_SSL) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 403:
            raise SimpleFinError(
                "SimpleFIN access was denied. Reconnect if access was revoked.",
                access_denied=True,
            ) from None
        if exc.code == 402:
            raise SimpleFinError("SimpleFIN reported that payment is required.") from None
        raise SimpleFinError("SimpleFIN could not return accounts.") from None
    except URLError:
        raise SimpleFinError("SimpleFIN could not be reached.") from None
    except (TimeoutError, OSError, UnicodeDecodeError):
        # Headers arrived but the body stalled or broke off.
        raise SimpleFinError("SimpleFIN did not finish responding. Try again later.") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise SimpleFinError("SimpleFIN returned a response that could not be read.") from None
    if not isinstance(payload, dict):
        raise SimpleFinError("SimpleFIN returned a response that could not be read.")
    return payload
