"""The edit-permission rule shared by savings goals and budgets."""

from django.core.exceptions import PermissionDenied

from .access import DENIED
from .category_services import current_household


def check_can_edit(person, obj):
    """Allow the owner of a private row and a current member of a household row's household."""
    if obj.scope == obj.Scope.HOUSEHOLD:
        household = current_household(person)
        if household is None or obj.household_id != household.pk:
            raise PermissionDenied(DENIED)
    elif obj.owner_id != person.pk:
        raise PermissionDenied(DENIED)
