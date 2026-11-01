"""The synthetic browser seed must hold on every calendar date (issue #607)."""

from calendar import isleap
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import Client

from finance.budget_services import dashboard_budget_summary
from finance.models import Transaction
from tests.mobile_seed import ROWS, row_date, seed_phone_data


def _days(first, last):
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


# A leap year, with the year changes on either side of it.
EVERY_DATE = _days(date(2027, 12, 1), date(2029, 1, 31))
# Seeding takes about 0.2 s, so the whole seed runs on the dates a calendar can break: a new year
# and a whole leap February, which holds every day of the month from 1 to 29.
SEEDED_DATES = _days(date(2027, 12, 31), date(2028, 1, 7)) + _days(date(2028, 2, 1), date(2028, 3, 1))


def _problems(today):
    """What the layout tests need from the seed, checked through the page they open on this date."""
    person = seed_phone_data()
    problems = []
    dates = list(Transaction.objects.filter(source_row_number__gte=100).values_list("transaction_date", flat=True))
    if len(dates) != len(ROWS):
        problems.append(f"{len(dates)} of {len(ROWS)} rows")
    if any(day.replace(day=1) != today.replace(day=1) for day in dates):
        problems.append("a row outside the current month")
    if any(day > today for day in dates):
        problems.append("a row in the future")
    attention = {card.name for card in dashboard_budget_summary(person.user, today=today).attention_cards}
    if attention != {"Dining", "Groceries"}:
        problems.append(f"attention budgets {sorted(attention)}")
    client = Client()
    client.force_login(person.user)
    if b'id="budgets-attention-heading"' not in client.get("/planning/budgets/").content:
        problems.append("no Budgets attention heading")
    return problems


@pytest.mark.parametrize("days_ago", sorted({row[0] for row in ROWS}))
def test_every_row_date_stays_in_the_current_month_and_never_ahead(days_ago):
    assert isleap(2028) and date(2028, 2, 29) in EVERY_DATE and len(EVERY_DATE) >= 366
    for today in EVERY_DATE:
        day = row_date(today, days_ago)
        assert (day.year, day.month) == (today.year, today.month), today
        assert day <= today, today


@pytest.mark.django_db
def test_seed_holds_on_the_dates_a_calendar_can_break():
    """The failure names each broken date, so a red run reads as a scan's result."""
    assert date(2028, 2, 29) in SEEDED_DATES and {day.day for day in SEEDED_DATES} >= set(range(1, 30))
    failures = {}
    for today in SEEDED_DATES:
        with patch("django.utils.timezone.localdate", return_value=today), transaction.atomic():
            problems = _problems(today)
            transaction.set_rollback(True)
        if problems:
            failures[today] = problems
    summary = [f"{len(failures)} broken dates, on days of the month {sorted({day.day for day in failures})}:"]
    summary += [f"{day.isoformat()}: {'; '.join(problems)}" for day, problems in failures.items()]
    assert not failures, "\n".join(summary)
