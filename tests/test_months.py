from datetime import date

import pytest

from finance.months import add_months, add_months_clamped, month_end, month_start


@pytest.mark.parametrize(
    "value, delta, first, clamped, last",
    [
        (date(2025, 1, 31), 1, date(2025, 2, 1), date(2025, 2, 28), date(2025, 2, 28)),
        (date(2024, 1, 31), 1, date(2024, 2, 1), date(2024, 2, 29), date(2024, 2, 29)),
        (date(2024, 2, 29), 12, date(2025, 2, 1), date(2025, 2, 28), date(2025, 2, 28)),
        (date(2025, 1, 15), -1, date(2024, 12, 1), date(2024, 12, 15), date(2024, 12, 31)),
        (date(2024, 12, 31), 1, date(2025, 1, 1), date(2025, 1, 31), date(2025, 1, 31)),
        (date(2025, 3, 31), -13, date(2024, 2, 1), date(2024, 2, 29), date(2024, 2, 29)),
        (date(2024, 2, 29), 0, date(2024, 2, 1), date(2024, 2, 29), date(2024, 2, 29)),
        (date(2024, 1, 30), 27, date(2026, 4, 1), date(2026, 4, 30), date(2026, 4, 30)),
    ],
)
def test_month_helpers(value, delta, first, clamped, last):
    assert add_months(value, delta) == first
    assert add_months_clamped(value, delta) == clamped
    assert month_start(clamped) == first
    assert month_end(clamped) == last
