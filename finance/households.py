"""Household membership lookups."""

from .models import Membership


def current_household(person):
    membership = (
        Membership.objects.filter(person=person, ended_at__isnull=True)
        .select_related("household")
        .first()
    )
    return None if membership is None else membership.household
