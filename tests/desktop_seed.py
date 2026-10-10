"""Synthetic data for the desktop-layout checks and screenshots (issue #340). No real figures."""

from datetime import timedelta

from django.utils import timezone

from finance.models import PlannedItem, SavingsGoal
from tests.mobile_seed import seed_phone_data


def seed_desktop_data():
    """The phone seed plus savings goals and planned items, so every page has a first row; return the person."""
    person = seed_phone_data()
    for index, name in enumerate(("Synthetic emergency fund", "Synthetic laptop", "Synthetic trip")):
        SavingsGoal.objects.create(owner=person, name=name, target_amount_minor=150_000, priority=index + 1)
    today = timezone.localdate()
    PlannedItem.objects.create(
        owner=person,
        name="Synthetic bonus",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=300_000,
        start_date=today + timedelta(days=60),
        cadence=PlannedItem.Cadence.ONE_TIME,
    )
    PlannedItem.objects.create(
        owner=person,
        name="Synthetic daycare",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=115_000,
        start_date=today + timedelta(days=90),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    return person
