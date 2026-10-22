"""The Frankfurter (ECB) rate client, with no network access (#441)."""

import json
from datetime import date
from decimal import Decimal
from http.client import IncompleteRead
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from finance.fx_rates import MAX_RESPONSE_BYTES, RateBook, RateUnavailable, currency_code, fetch_rates


class FakeResponse:
    def __init__(self, body):
        self._body = BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(monkeypatch, body, *, requests=None):
    def fake_open(request, timeout=None, context=None, deadline=None):
        if requests is not None:
            requests.append(request.full_url)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    monkeypatch.setattr("finance.simplefin_client.urlopen", fake_open)


def series(rates):
    return json.dumps(
        {
            "amount": 1.0,
            "base": "CAD",
            "start_date": "2026-03-09",
            "end_date": "2026-03-16",
            "rates": {day: {"USD": rate} for day, rate in rates.items()},
        }
    ).encode()


MARCH = {"2026-03-12": 0.7352, "2026-03-13": 0.72975, "2026-03-16": 0.73094}


def test_a_normal_response_gives_exact_decimal_rates_and_asks_for_the_range(monkeypatch):
    requests = []
    serve(monkeypatch, series(MARCH), requests=requests)

    rates = fetch_rates("cad", date(2026, 3, 9), date(2026, 3, 16))

    assert rates == {
        date(2026, 3, 12): Decimal("0.7352"),
        date(2026, 3, 13): Decimal("0.72975"),
        date(2026, 3, 16): Decimal("0.73094"),
    }
    assert requests == ["https://api.frankfurter.dev/v1/2026-03-09..2026-03-16?base=CAD&symbols=USD"]


def test_a_weekend_uses_the_previous_business_days_rate(monkeypatch):
    serve(monkeypatch, series(MARCH))
    book = RateBook()
    book.load("CAD", date(2026, 3, 14), date(2026, 3, 15))

    saturday = book.rate_on("CAD", date(2026, 3, 14))
    sunday = book.rate_on("CAD", date(2026, 3, 15))

    assert (saturday.value, saturday.rate_date) == (Decimal("0.72975"), date(2026, 3, 13))
    assert sunday == saturday


def test_a_date_with_no_rate_within_a_week_is_unavailable(monkeypatch):
    serve(monkeypatch, series(MARCH))
    book = RateBook()
    book.load("CAD", date(2026, 3, 9), date(2026, 3, 30))

    with pytest.raises(RateUnavailable):
        book.rate_on("CAD", date(2026, 3, 30))
    with pytest.raises(RateUnavailable):
        book.rate_on("EUR", date(2026, 3, 13))


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("https://api.frankfurter.dev/v1/x", 404, "Not Found", hdrs=None, fp=BytesIO()),
        HTTPError("https://api.frankfurter.dev/v1/x", 500, "Server Error", hdrs=None, fp=BytesIO()),
        URLError("offline"),
        TimeoutError(),
        IncompleteRead(b"{"),
    ],
    ids=["404", "500", "unreachable", "timeout", "truncated"],
)
def test_an_http_or_network_error_is_unavailable_and_the_book_remembers_why(monkeypatch, failure):
    serve(monkeypatch, failure)
    book = RateBook()
    book.load("CAD", date(2026, 3, 9), date(2026, 3, 16))

    with pytest.raises(RateUnavailable, match="exchange-rate service"):
        book.rate_on("CAD", date(2026, 3, 13))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"{}",
        b'{"rates": []}',
        b'{"rates": {"2026-03-13": {"EUR": 0.5}}}',
        b'{"rates": {"2026-03-13": {"USD": "abc"}}}',
        b'{"rates": {"not-a-date": {"USD": 0.5}}}',
        b'{"rates": {"2026-03-13": {"USD": 0}}}',
        b'{"rates": {"2026-03-13": {"USD": -0.5}}}',
        b'{"rates": {"2026-03-13": {"USD": 1e400}}}',
        b"[" * 100000,
    ],
    ids=["text", "bytes", "list", "empty", "rates-list", "no-usd", "text-rate", "bad-date", "zero", "negative",
         "huge", "deep"],
)
def test_malformed_json_is_unavailable(monkeypatch, body):
    serve(monkeypatch, body)

    with pytest.raises(RateUnavailable):
        fetch_rates("CAD", date(2026, 3, 9), date(2026, 3, 16))


def test_an_oversize_body_is_unavailable_without_reading_it_all(monkeypatch):
    big = series(MARCH) + b" " * MAX_RESPONSE_BYTES
    serve(monkeypatch, big)

    with pytest.raises(RateUnavailable, match="more data"):
        fetch_rates("CAD", date(2026, 3, 9), date(2026, 3, 16))


def test_rates_outside_the_requested_range_are_ignored(monkeypatch):
    serve(monkeypatch, series({"2026-03-13": 0.7, "2025-01-01": 0.1}))

    assert fetch_rates("CAD", date(2026, 3, 9), date(2026, 3, 16)) == {date(2026, 3, 13): Decimal("0.7")}


@pytest.mark.parametrize("currency", ["USD", "", "https://evil.test", "CA", "CAD&x=1", "C4D"])
def test_only_a_foreign_currency_code_ever_reaches_the_url(monkeypatch, currency):
    requests = []
    serve(monkeypatch, series(MARCH), requests=requests)

    with pytest.raises(RateUnavailable):
        fetch_rates(currency, date(2026, 3, 9), date(2026, 3, 16))

    assert requests == []


def test_currency_code_normalizes_and_rejects():
    assert currency_code(" cad ") == "CAD"
    assert currency_code(None) is None
    assert currency_code("https://example.test/points") is None
