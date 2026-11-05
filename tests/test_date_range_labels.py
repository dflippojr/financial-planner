"""Date range labels name the start year when it differs from the end year (#665)."""

import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.templatetags.ui import date_range
from tests.test_recurring_review import make_account, make_household, make_person, make_transaction

_real_localdate = timezone.localdate


def pinned_localdate(today):
    """Pin only "today"; converting a stored timestamp keeps working."""

    def fake(value=None, timezone=None):
        if value is None:
            return today
        return _real_localdate(value, timezone)

    return fake


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 11, 2), date(2026, 11, 5), "Nov 2 – Nov 5, 2026"),
        (date(2025, 11, 1), date(2026, 11, 5), "Nov 1, 2025 – Nov 5, 2026"),
        (date(2025, 12, 29), date(2026, 1, 1), "Dec 29, 2025 – Jan 1, 2026"),
        (date(2026, 1, 1), date(2026, 12, 31), "Jan 1 – Dec 31, 2026"),
    ],
)
def test_date_range_adds_the_start_year_only_when_years_differ(start, end, expected):
    assert date_range(start, end) == expected


@pytest.mark.parametrize(("start", "end"), [(None, date(2026, 11, 5)), (date(2026, 11, 1), None), (None, None)])
def test_date_range_is_empty_when_either_end_is_missing(start, end):
    assert date_range(start, end) == ""


def home_content(owner, today, params=None):
    client = Client()
    client.force_login(owner.user)
    with patch("finance.views.timezone.localdate", side_effect=pinned_localdate(today)):
        response = client.get(reverse("home"), params or {})
    assert response.status_code == 200
    assert response.context["report"] is not None
    return response.content.decode()


def make_owner_with_activity(day):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, transaction_date=day, amount_minor=-2_500, description="Synthetic Groceries")
    return owner


@pytest.mark.django_db
def test_home_summary_default_range_shows_both_years():
    today = date(2026, 11, 5)
    owner = make_owner_with_activity(date(2026, 3, 4))

    content = home_content(owner, today)

    assert "Nov 1, 2025 – Nov 5, 2026 · " in content
    assert "Nov 1 – Nov 5, 2026" not in content


@pytest.mark.django_db
def test_home_summary_this_month_keeps_the_short_form():
    today = date(2026, 11, 5)
    owner = make_owner_with_activity(date(2026, 11, 3))

    content = home_content(owner, today, {"date_from": "2026-11-01", "date_to": "2026-11-05", "grouping": "month"})

    assert "Nov 1 – Nov 5, 2026 · " in content


@pytest.mark.django_db
def test_weekly_digest_across_new_year_shows_both_years():
    # Thursday Jan 1, 2026: the current digest week runs Mon Dec 29, 2025 - Thu Jan 1, 2026.
    today = date(2026, 1, 1)
    owner = make_owner_with_activity(date(2025, 12, 30))

    content = home_content(owner, today)

    assert "Dec 29, 2025 – Jan 1, 2026 · " in content


# A date printed without a year right before a range dash, e.g. {{ start|date:"M j" }} – {{ end|date:"M j, Y" }}.
YEARLESS_RANGE_START = re.compile(r"""\|\s*date\s*:\s*(["'])[^"'Y]*\1\s*\}\}\s*(–|&ndash;|&#8211;|-|to\b)""")


def test_no_template_prints_a_range_start_without_its_year():
    offenders = []
    for root in settings.TEMPLATES[0]["DIRS"]:
        for path in sorted(Path(root).rglob("*.html")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if YEARLESS_RANGE_START.search(line):
                    offenders.append(f"{path}:{number}")
    assert offenders == [], "use {{ start|date_range:end }} for date ranges: " + ", ".join(offenders)


def test_guard_pattern_catches_the_old_shape():
    assert YEARLESS_RANGE_START.search('{{ range_from|date:"M j" }} – {{ range_to|date:"M j, Y" }}')
    assert YEARLESS_RANGE_START.search("{{ start|date:'D M j' }}&ndash;{{ end|date:'M j, Y' }}")
    assert not YEARLESS_RANGE_START.search('{{ start|date:"M j, Y" }} – {{ end|date:"M j, Y" }}')
    assert not YEARLESS_RANGE_START.search('{{ row.date|date:"D, M j" }} · planned')
