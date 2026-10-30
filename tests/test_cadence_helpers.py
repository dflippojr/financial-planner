"""Each cadence's interval and tolerance, pinned before the lookups move behind helpers (#598)."""

import pytest

from finance.bills_calendar import _income_tolerance
from finance.models import RecurringSeries
from finance.projection import CADENCE_ONE_TIME
from finance.recurring_review import cadence_tolerance_days
from finance.recurring_services import CADENCE_DAYS

Cadence = RecurringSeries.Cadence
CASES = [
    (Cadence.WEEKLY, 7, 3),
    (Cadence.BIWEEKLY, 14, 3),
    (Cadence.MONTHLY, 30, 3),
    (Cadence.QUARTERLY, 91, 4),
    (Cadence.ANNUAL, 365, 5),
]


def test_every_cadence_is_pinned():
    assert {case[0] for case in CASES} == set(Cadence) == set(CADENCE_DAYS)


@pytest.mark.parametrize(("cadence", "interval", "tolerance"), CASES)
def test_cadence_table(cadence, interval, tolerance):
    assert CADENCE_DAYS[cadence] == (interval, tolerance)


@pytest.mark.parametrize(("cadence", "interval", "tolerance"), CASES)
def test_review_tolerance(cadence, interval, tolerance):
    assert cadence_tolerance_days(cadence) == tolerance


@pytest.mark.parametrize(("cadence", "interval", "tolerance"), CASES)
def test_income_tolerance_follows_the_cadence(cadence, interval, tolerance):
    assert _income_tolerance(cadence) == tolerance


def test_income_tolerance_of_a_one_time_item_is_zero():
    assert _income_tolerance(CADENCE_ONE_TIME) == 0
