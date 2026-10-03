"""JSON HTTP client that never logs credentials or raw provider bodies."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they would carry the App token past the URL allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class HarnessHttpError(Exception):
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}
        super().__init__("The AI provider could not be reached.")


def json_request(url, *, token, method="GET", body=None, timeout=30):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = _read_error_payload(exc)
        raise HarnessHttpError(exc.code, payload) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        raise HarnessHttpError(0, {}) from None


def _read_error_payload(exc):
    try:
        raw = exc.read()
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
