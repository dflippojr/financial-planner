
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from .people import Household, Membership, person_for as _person_for


class Category(models.Model):
    class Code(models.TextChoices):
        CUSTOM = "custom", "Custom"
        UNCATEGORIZED = "uncategorized", "Uncategorized"
        TRANSFER = "transfer", "Transfer"

    household = models.ForeignKey(Household, on_delete=models.PROTECT, related_name="categories")
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=13, choices=Code, default=Code.CUSTOM)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            current_households = Membership.objects.filter(
                person=person,
                ended_at__isnull=True,
            ).values("household_id")
            return self.filter(household_id__in=current_households)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("household", "name"), name="category_unique_name_per_household"),
            models.UniqueConstraint(
                fields=("household", "code"),
                condition=~Q(code="custom"),
                name="category_unique_system_code_per_household",
            ),
            models.CheckConstraint(
                condition=Q(code__in=("custom", "uncategorized", "transfer")),
                name="category_code_valid",
            ),
        ]

    def __str__(self):
        return self.name


class Tag(models.Model):
    household = models.ForeignKey(Household, on_delete=models.PROTECT, related_name="tags")
    name = models.CharField(max_length=80)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            current_households = Membership.objects.filter(
                person=person,
                ended_at__isnull=True,
            ).values("household_id")
            return self.filter(household_id__in=current_households)

        def active(self):
            return self.filter(is_archived=False)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                F("household"),
                name="tag_unique_name_per_household",
            ),
        ]

    def __str__(self):
        return self.name

