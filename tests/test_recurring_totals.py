"""The recurring monthly total is the annual total / 12, rounded half-even (#507). Synthetic data only."""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from finance.models import RecurringSeries, monthly_from_annual_minor
from finance.recurring_services import confirmed_totals
from finance.year_end import year_end_report
from tests.page_payload import json_script_payload
from tests.test_recurring_review import add_member, make_account, make_household, make_person, make_series, make_transaction


@pytest.mark.parametrize(("annual", "monthly"), [(12012, 1001), (6, 0), (18, 2), (30, 2), (57057, 4755)])
def test_monthly_is_annual_over_twelve_rounded_half_even(annual, monthly):
    assert monthly_from_annual_minor(annual) == monthly


def _confirmed(owner, account, cadence, name):
    series = make_series(owner, name=name, cadence=cadence, typical_minor=-1001)
    add_member(series, make_transaction(owner, account, transaction_date=date(2025, 6, 2), amount_minor=-1001, description=name))
    return series


@pytest.mark.django_db
def test_three_quarterly_series_total_matches_the_annual_total():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = [_confirmed(owner, account, RecurringSeries.Cadence.QUARTERLY, f"Synthetic Quarterly {name}") for name in "ABC"]

    assert confirmed_totals(series) == (1001, 12012)


@pytest.mark.django_db
def test_mixed_cadence_total_agrees_on_the_page_and_the_year_end_report():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    for cadence in (RecurringSeries.Cadence.WEEKLY, RecurringSeries.Cadence.QUARTERLY, RecurringSeries.Cadence.ANNUAL):
        _confirmed(owner, account, cadence, f"Synthetic {cadence.title()}")
    # 52,052 + 4,004 + 1,001 = 57,057 a year; / 12 = 4,754.75. Flooring each
    # series (4,337 + 333 + 83) would show 4,753.
    client = Client()
    client.force_login(owner.user)

    page = client.get(reverse("recurring-review"))
    payload = json_script_payload(page.content.decode(), "recurring-chart-data")
    report = year_end_report(owner, year=2025, scope="", today=date(2026, 3, 1))

    assert (page.context["monthly_minor"], page.context["annual_minor"]) == (4755, 57057)
    assert (payload["monthly_minor"], payload["annual_minor"]) == (4755, 57057)
    assert "47.55" in page.context["monthly_display"]
    assert (report.recurring.monthly_minor, report.recurring.annual_minor) == (4755, 57057)
    assert sorted(item["monthly_minor"] for item in payload["series"]) == [83, 334, 4338]
