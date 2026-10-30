"""Pins the exact minor-unit display strings of every entry point (#587).

Expected values are literals, so moving the formatter cannot change them
unnoticed. These entry points never touch the database: they format fields of
unsaved instances.
"""

import pytest

from finance import money_display
from finance.cash_flow import format_minor
from finance.models import (
    PlannedItem,
    RecurringSeries,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
)
from finance.views import _money_display


MAX_BIGINT = 2**63 - 1
MIN_BIGINT = -(2**63)

CASES = [
    (0, "USD", "0.00 USD"),
    (1, "USD", "0.01 USD"),
    (-1, "USD", "-0.01 USD"),
    (99, "USD", "0.99 USD"),
    (100_000, "USD", "1,000.00 USD"),
    (-123_456_789, "USD", "-1,234,567.89 USD"),
    (123_456_789, "EUR", "1,234,567.89 EUR"),
    (-50, "CAD", "-0.50 CAD"),
    (MAX_BIGINT, "USD", "92,233,720,368,547,758.07 USD"),
    (MIN_BIGINT, "USD", "-92,233,720,368,547,758.08 USD"),
    (MAX_BIGINT, "JPY", "92,233,720,368,547,758.07 JPY"),
]


@pytest.mark.parametrize(("minor", "currency", "expected"), CASES)
def test_cash_flow_format_minor(minor, currency, expected):
    assert format_minor(minor, currency) == expected


def test_cash_flow_format_minor_defaults_to_usd():
    assert format_minor(1_234) == "12.34 USD"


@pytest.mark.parametrize(("minor", "currency", "expected"), CASES)
def test_recurring_page_helper(minor, currency, expected):
    assert _money_display(minor, currency) == expected


@pytest.mark.parametrize(("minor", "currency", "expected"), CASES)
def test_transaction_amount_display(minor, currency, expected):
    assert Transaction(amount_minor=minor, currency=currency).amount_display == expected


@pytest.mark.parametrize(("minor", "currency", "expected"), CASES)
def test_correction_history_previous_and_new_display(minor, currency, expected):
    # Each side formats its own amount in the row's currency.
    previous = TransactionCorrectionHistory(
        field_name=TransactionCorrectionHistory.Field.AMOUNT_MINOR,
        previous_amount_minor=minor,
        new_amount_minor=7,
        currency=currency,
    )
    new = TransactionCorrectionHistory(
        field_name=TransactionCorrectionHistory.Field.AMOUNT_MINOR,
        previous_amount_minor=7,
        new_amount_minor=minor,
        currency=currency,
    )
    assert previous.previous_display == expected
    assert previous.new_display == f"0.07 {currency}"
    assert new.previous_display == f"0.07 {currency}"
    assert new.new_display == expected


@pytest.mark.parametrize(("minor", "currency", "expected"), CASES)
def test_split_amount_display_takes_currency_from_its_transaction(
    minor, currency, expected
):
    split = TransactionSplit(
        amount_minor=minor, transaction=Transaction(currency=currency)
    )
    assert split.amount_display == expected


@pytest.mark.parametrize(("minor", "currency", "expected"), CASES)
def test_planned_item_amount_display(minor, currency, expected):
    assert PlannedItem(amount_minor=minor, currency=currency).amount_display == expected


@pytest.mark.parametrize(
    ("typical", "cadence", "currency", "amount", "monthly", "annual"),
    [
        (
            -1,
            RecurringSeries.Cadence.ANNUAL,
            "USD",
            "-0.01 USD",
            "0.00 USD",
            "0.01 USD",
        ),
        (
            -1_000,
            RecurringSeries.Cadence.MONTHLY,
            "USD",
            "-10.00 USD",
            "10.00 USD",
            "120.00 USD",
        ),
        (
            -1_000,
            RecurringSeries.Cadence.WEEKLY,
            "EUR",
            "-10.00 EUR",
            "43.33 EUR",
            "520.00 EUR",
        ),
        (
            -1_001,
            RecurringSeries.Cadence.BIWEEKLY,
            "CAD",
            "-10.01 CAD",
            "21.69 CAD",
            "260.26 CAD",
        ),
        (
            -123_456,
            RecurringSeries.Cadence.QUARTERLY,
            "USD",
            "-1,234.56 USD",
            "411.52 USD",
            "4,938.24 USD",
        ),
        # 6 cents a year is half a cent a month, which rounds half-even to 0 and 2.
        (
            -6,
            RecurringSeries.Cadence.ANNUAL,
            "USD",
            "-0.06 USD",
            "0.00 USD",
            "0.06 USD",
        ),
        (
            -18,
            RecurringSeries.Cadence.ANNUAL,
            "USD",
            "-0.18 USD",
            "0.02 USD",
            "0.18 USD",
        ),
        (
            MIN_BIGINT,
            RecurringSeries.Cadence.WEEKLY,
            "USD",
            "-92,233,720,368,547,758.08 USD",
            "399,679,454,930,373,618.35 USD",
            "4,796,153,459,164,483,420.16 USD",
        ),
    ],
)
def test_recurring_series_displays(typical, cadence, currency, amount, monthly, annual):
    series = RecurringSeries(
        typical_amount_minor=typical, cadence=cadence, currency=currency
    )
    assert series.amount_display == amount
    assert series.monthly_display == monthly
    assert series.annual_display == annual


def test_cash_flow_keeps_exporting_the_shared_formatter():
    assert format_minor is money_display.format_minor
