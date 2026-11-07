"""Home opens on This month and always marks exactly one range option (#700)."""

from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.urls import reverse

from finance.cash_flow import date_range_presets, home_default_date_range
from tests.page_payload import json_script_payload
from tests.test_cash_flow import make_account, make_household, make_person, make_transaction

TODAY = date(2026, 11, 7)


def _client():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    # One row this month and one last month, so a 13-month default would differ.
    make_transaction(
        owner,
        checking,
        transaction_date=date(2026, 11, 3),
        amount_minor=-2500,
        range_start=date(2026, 11, 1),
        range_end=date(2026, 11, 7),
    )
    make_transaction(
        owner,
        checking,
        transaction_date=date(2026, 10, 28),
        amount_minor=-9900,
        range_start=date(2026, 10, 1),
        range_end=date(2026, 10, 31),
    )
    client = Client()
    client.force_login(owner.user)
    return client


def _current(response):
    return [option.label for option in response.context["range_options"] if option.current]


def test_home_default_is_the_this_month_preset():
    presets = {preset.key: preset for preset in date_range_presets(TODAY)}
    assert home_default_date_range(TODAY) == (presets["this-month"].date_from, presets["this-month"].date_to)
    assert home_default_date_range(date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 1, 1))


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_with_no_range_opens_on_this_month(_localdate):
    response = _client().get(reverse("home"))
    content = response.content.decode()

    assert response.status_code == 200
    assert (response.context["range_from"], response.context["range_to"]) == (date(2026, 11, 1), TODAY)
    form = response.context["filter_form"]
    assert form["date_from"].value() == date(2026, 11, 1)
    assert form["date_to"].value() == TODAY
    summary = response.context["report"].summary
    assert summary.spending_minor == 2500
    assert (summary.previous_from, summary.previous_to) == (date(2026, 10, 25), date(2026, 10, 31))
    assert summary.previous_spending_minor == 9900
    chart = json_script_payload(content, "cash-flow-chart-data")
    actual = [row for row in chart["periods"] if not row["projected"]]
    assert [row["spending_minor"] for row in actual] == [2500]
    assert any(row["projected"] for row in chart["periods"])
    assert chart["summary"]["previous_from"] == "2026-10-25"
    assert _current(response) == ["This month"]
    assert 'aria-current="true">This month</a>' in content
    assert 'aria-current="true" data-open-details="cash-flow-filters">Custom' not in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_every_home_range_marks_exactly_one_option(_localdate):
    client = _client()
    first = client.get(reverse("home"))
    for option in first.context["range_options"]:
        if option.label == "Custom":
            continue
        query = {key: values[-1] for key, values in parse_qs(urlparse(option.url).query).items()}
        assert _current(client.get(reverse("home"), query)) == [option.label]

    # A typed custom range equal to a preset marks that preset, not Custom.
    typed = client.get(reverse("home"), {"date_from": "2026-09-01", "date_to": "2026-11-07"})
    assert _current(typed) == ["3 months"]
    typed = client.get(reverse("home"), {"date_from": "2026-11-01", "date_to": "2026-11-07"})
    assert _current(typed) == ["This month"]
    # A range that matches no preset marks Custom alone.
    custom = client.get(reverse("home"), {"date_from": "2026-10-01", "date_to": "2026-10-31"})
    assert _current(custom) == ["Custom"]
    assert 'aria-current="true" data-open-details="cash-flow-filters">Custom</a>' in custom.content.decode()


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_reset_range_and_dateless_home_links_land_on_this_month(_localdate):
    client = _client()
    page = client.get(reverse("home"), {"date_from": "2026-10-01", "date_to": "2026-10-31"})
    content = page.content.decode()
    reset_href = f'href="{reverse("home")}">Reset range</a>'
    assert reset_href in content
    # Other filters without dates (a grouping or scope link) also open on This month.
    for query in ({}, {"grouping": "week"}, {"scope": "private"}):
        landed = client.get(reverse("home"), query)
        assert (landed.context["range_from"], landed.context["range_to"]) == (date(2026, 11, 1), TODAY)
        assert _current(landed) == ["This month"]
        # The filter form shows those dates, bound or not.
        form = landed.context["filter_form"]
        assert str(form["date_from"].value()) == "2026-11-01"
        assert str(form["date_to"].value()) == "2026-11-07"
        content = landed.content.decode()
        assert 'name="date_from" value="2026-11-01"' in content
        assert 'name="date_to" value="2026-11-07"' in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_fills_one_missing_date_from_the_other(_localdate):
    client = _client()
    only_end = client.get(reverse("home"), {"date_to": "2026-10-15"})
    assert not only_end.context["filter_form"].errors
    assert (only_end.context["range_from"], only_end.context["range_to"]) == (date(2026, 10, 1), date(2026, 10, 15))
    assert only_end.context["filter_form"]["date_from"].value() == "2026-10-01"
    assert only_end.context["filter_form"]["date_to"].value() == "2026-10-15"
    assert _current(only_end) == ["Custom"]
    only_start = client.get(reverse("home"), {"date_from": "2026-09-01"})
    assert (only_start.context["range_from"], only_start.context["range_to"]) == (date(2026, 9, 1), TODAY)
    assert only_start.context["filter_form"]["date_to"].value() == "2026-11-07"
    assert _current(only_start) == ["3 months"]


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_other_pages_keep_the_thirteen_month_default(_localdate):
    client = _client()
    for name in ("spending-by-category", "net-worth"):
        response = client.get(reverse(name))
        assert response.status_code == 200
        assert (response.context["range_from"], response.context["range_to"]) == (date(2025, 11, 1), TODAY), name
