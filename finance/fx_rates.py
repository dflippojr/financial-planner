"""ECB reference rates from Frankfurter (no key), for converting non-USD SimpleFIN amounts.

One request per currency returns every business day of a date range, so a
sync never asks per transaction. A weekend or holiday uses the latest earlier
business day's rate, as the ECB itself does. Any failure raises
RateUnavailable, which callers turn into a skipped account, never a failed
connection.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import Request

from finance import simplefin_client
from finance.simplefin_errors import SimpleFinError

FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
APP_CURRENCY = "USD"
TIMEOUT_SECONDS = 10
DEADLINE_SECONDS = 20
# Ten years of daily rates is about 120 KB.
MAX_RESPONSE_BYTES = 512 * 1024
# The ECB skips weekends and a few holidays; no gap is longer than this.
MAX_STALE_DAYS = 7
# Rates are a handful of digits; anything outside this is a bad response.
MAX_RATE = Decimal("1000000")
MIN_RATE = Decimal("0.000001")
_CODE = re.compile(r"[A-Z]{3}")


class RateUnavailable(Exception):
    """No usable rate; the message is safe to show a member."""


@dataclass(frozen=True)
class Rate:
    value: Decimal
    rate_date: date


def currency_code(value) -> str | None:
    """The upper-case ISO 4217-shaped code, or None when it is missing or malformed."""
    code = str(value or "").strip().upper()
    return code if _CODE.fullmatch(code) else None


def _parse_rates(body: bytes, start: date, end: date) -> dict[date, Decimal]:
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
        raw = payload["rates"]
        parsed = {}
        for key, entry in raw.items():
            day = date.fromisoformat(key)
            value = Decimal(entry[APP_CURRENCY])
            if not value.is_finite() or not MIN_RATE <= value <= MAX_RATE:
                raise ValueError("rate out of range")
            if start <= day <= end:
                parsed[day] = value
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, RecursionError):
        raise RateUnavailable("The exchange-rate service sent a response that could not be read.") from None
    return parsed


def fetch_rates(currency: str, start: date, end: date) -> dict[date, Decimal]:
    """Business-day rates (currency to USD) from start through end."""
    code = currency_code(currency)
    if code is None or code == APP_CURRENCY:
        raise RateUnavailable("That currency has no exchange rate.")
    query = urlencode({"base": code, "symbols": APP_CURRENCY})
    request = Request(  # noqa: S310 - the URL is a constant https address
        f"{FRANKFURTER_URL}/{start.isoformat()}..{end.isoformat()}?{query}", method="GET"
    )
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        with simplefin_client.urlopen(request, timeout=TIMEOUT_SECONDS, deadline=deadline) as response:
            body = simplefin_client.read_capped(response, limit=MAX_RESPONSE_BYTES, deadline=deadline)
    except SimpleFinError:
        raise RateUnavailable("The exchange-rate service sent more data than this app accepts.") from None
    except (OSError, HTTPException):  # URLError, HTTPError, TimeoutError, IncompleteRead
        raise RateUnavailable("The exchange-rate service could not be reached.") from None
    return _parse_rates(body, start, end)


class RateBook:
    """Rates for the currencies one sync needs, loaded before the database lock is taken."""

    def __init__(self):
        self._tables: dict[str, dict[date, Decimal]] = {}
        self._failed: dict[str, str] = {}

    def load(self, currency: str, start: date, end: date) -> None:
        # Reach back far enough that a weekend start still finds its Friday.
        try:
            self._tables[currency] = fetch_rates(currency, start - timedelta(days=MAX_STALE_DAYS), end)
        except RateUnavailable as exc:
            self._failed[currency] = str(exc)

    def rate_on(self, currency: str, day: date) -> Rate:
        table = self._tables.get(currency)
        if table is None:
            raise RateUnavailable(self._failed.get(currency, "No exchange rate was loaded for that currency."))
        for back in range(MAX_STALE_DAYS + 1):
            rate_date = day - timedelta(days=back)
            if rate_date in table:
                return Rate(table[rate_date], rate_date)
        raise RateUnavailable("No exchange rate was published near that date.")


def to_usd_minor(amount: Decimal, rate: Decimal) -> int:
    """Convert a major-unit amount to US cents, rounding half-even once."""
    cents = (amount * rate * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(cents)
