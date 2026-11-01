"""The synthetic browser seed must hold on every calendar date (issue #607)."""

from calendar import isleap
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import Client

from finance.budget_services import dashboard_budget_summary
from finance.models import Transaction
from tests.mobile_seed import ROWS, seed_phone_data

pytestmark = pytest.mark.django_db

# Every day of the leap year 2028, with the year changes on either side of it.
FIRST_DAY = date(2027, 12, 1)
LAST_DAY = date(2029, 1, 31)


def _problems(today):
    """What the layout tests need from the seed, checked through the pages they open on this date."""
    person = seed_phone_data()
    problems = []
    dates = list(
        Transaction.objects.filter(source_row_number__gte=100).values_list("transaction_date", flat=True)
    )
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


def test_seed_holds_on_every_date():
    """Scan every date: the failure names each broken date, so a red run reads as the scan's result."""
    assert isleap(2028) and (LAST_DAY - FIRST_DAY).days + 1 >= 366
    failures = {}
    for offset in range((LAST_DAY - FIRST_DAY).days + 1):
        today = FIRST_DAY + timedelta(days=offset)
        with patch("django.utils.timezone.localdate", return_value=today), transaction.atomic():
            problems = _problems(today)
            transaction.set_rollback(True)
        if problems:
            failures[today] = problems
    days_of_month = sorted({day.day for day in failures})
    summary = [f"{len(failures)} broken dates, on days of the month {days_of_month}:"]
    summary += [f"{day.isoformat()}: {'; '.join(problems)}" for day, problems in failures.items()]
    assert not failures, "\n".join(summary)
