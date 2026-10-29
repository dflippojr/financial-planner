"""Each cadence's step, pinned so the shared step tables cannot drift (#597). Synthetic data only."""

from datetime import date
from itertools import pairwise

import pytest

from finance.models import RecurringSeries
from finance.projection import occurrence_dates
from finance.recurring_services import add_cadence

Cadence = RecurringSeries.Cadence
MID_MONTH = date(2026, 1, 15)
MONTH_END = date(2026, 1, 31)
# A start on the 30th, before a 24 month window, so calendar steps clamp in February.
START = date(2025, 11, 30)
WINDOW = (date(2026, 1, 1), date(2027, 12, 31))


ADD_CADENCE_CASES = [
    (Cadence.WEEKLY, date(2026, 1, 22), date(2026, 2, 7)),
    (Cadence.BIWEEKLY, date(2026, 1, 29), date(2026, 2, 14)),
    (Cadence.MONTHLY, date(2026, 2, 15), date(2026, 2, 28)),
    (Cadence.QUARTERLY, date(2026, 4, 15), date(2026, 4, 30)),
    (Cadence.ANNUAL, date(2027, 1, 15), date(2027, 1, 31)),
]


def test_every_cadence_is_pinned():
    assert {case[0] for case in ADD_CADENCE_CASES} == set(Cadence)


@pytest.mark.parametrize(("cadence", "from_mid_month", "from_month_end"), ADD_CADENCE_CASES)
def test_add_cadence_steps_from_mid_month_and_month_end(cadence, from_mid_month, from_month_end):
    assert add_cadence(MID_MONTH, cadence) == from_mid_month
    assert add_cadence(MONTH_END, cadence) == from_month_end


@pytest.mark.parametrize(
    ("cadence", "count", "first", "last"),
    [
        (Cadence.WEEKLY, 104, date(2026, 1, 4), date(2027, 12, 26)),
        (Cadence.BIWEEKLY, 52, date(2026, 1, 11), date(2027, 12, 26)),
    ],
)
def test_week_cadence_occurrences_over_24_months(cadence, count, first, last):
    dates = occurrence_dates(START, None, cadence, *WINDOW)

    assert (len(dates), dates[0], dates[-1]) == (count, first, last)
    stride = (dates[1] - dates[0]).days
    assert stride == (7 if cadence == Cadence.WEEKLY else 14)
    assert all((later - earlier).days == stride for earlier, later in pairwise(dates))


def _monthly_dates():
    dates = []
    for year in (2026, 2027):
        dates += [date(year, month, 28 if month == 2 else 30) for month in range(1, 13)]
    return dates


@pytest.mark.parametrize(
    ("cadence", "expected"),
    [
        (Cadence.MONTHLY, _monthly_dates()),
        (
            Cadence.QUARTERLY,
            [
                date(2026, 2, 28),
                date(2026, 5, 30),
                date(2026, 8, 30),
                date(2026, 11, 30),
                date(2027, 2, 28),
                date(2027, 5, 30),
                date(2027, 8, 30),
                date(2027, 11, 30),
            ],
        ),
        (Cadence.ANNUAL, [date(2026, 11, 30), date(2027, 11, 30)]),
    ],
)
def test_calendar_cadence_occurrences_over_24_months(cadence, expected):
    assert occurrence_dates(START, None, cadence, *WINDOW) == expected


@pytest.mark.parametrize(
    ("cadence", "annual"),
    [
        (Cadence.WEEKLY, 52052),
        (Cadence.BIWEEKLY, 26026),
        (Cadence.MONTHLY, 12012),
        (Cadence.QUARTERLY, 4004),
        (Cadence.ANNUAL, 1001),
    ],
)
def test_annual_minor_for_a_fixed_amount(cadence, annual):
    assert RecurringSeries(cadence=cadence, typical_amount_minor=-1001).annual_minor == annual

