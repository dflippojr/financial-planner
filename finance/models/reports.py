
from django.db import models
from django.db.models import Q

from .base import sha256_validator
from .people import Person, person_for as _person_for
from .accounts import Account


class ReconciliationRun(models.Model):
    """When the scheduled balance reconciliation last ran; never its results."""

    ran_at = models.DateTimeField()

    def __str__(self):
        return f"Reconciliation run {self.pk}"


class BillsCalendarSettings(models.Model):
    person = models.OneToOneField(Person, on_delete=models.PROTECT, related_name="bills_calendar_settings")
    threshold_minor = models.BigIntegerField(null=True, blank=True)
    accounts = models.ManyToManyField(Account, blank=True, related_name="bills_calendar_settings")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(threshold_minor__isnull=True) | Q(threshold_minor__gte=0),
                name="bills_calendar_threshold_non_negative",
            ),
        ]

    def __str__(self):
        return f"Bills calendar settings for {self.person_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class MonthlyReview(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="monthly_reviews")
    month = models.DateField()
    visibility_key = models.CharField(max_length=64, validators=(sha256_validator,))
    facts = models.JSONField()
    generated_at = models.DateTimeField()
    ai_paragraph = models.TextField(blank=True, default="")
    ai_backend = models.CharField(max_length=32, blank=True, default="")
    unusual_ai_paragraph = models.TextField(blank=True, default="")
    unusual_ai_backend = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "month"), name="monthly_review_person_month"),
            models.CheckConstraint(condition=Q(month__day=1), name="monthly_review_month_start"),
        ]

    def __str__(self):
        return f"Monthly review {self.person_id} {self.month}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class SheetComparisonSettings(models.Model):
    class SpendingSign(models.TextChoices):
        UNSIGNED = "unsigned", "Unsigned (positive spending)"
        SIGNED = "signed", "Signed (negative is spending)"

    member = models.OneToOneField(
        Person,
        on_delete=models.CASCADE,
        related_name="sheet_comparison_settings",
    )
    month_column = models.CharField(max_length=255)
    income_column = models.CharField(max_length=255)
    spending_column = models.CharField(max_length=255)
    spending_sign = models.CharField(
        max_length=16,
        choices=SpendingSign.choices,
        default=SpendingSign.UNSIGNED,
    )
    tolerance_minor = models.PositiveIntegerField(default=100)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(spending_sign__in=("unsigned", "signed")),
                name="sheet_comparison_spending_sign_valid",
            ),
        ]

    def __str__(self):
        return f"Sheet comparison settings {self.member_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class SheetMonthTotal(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="sheet_month_totals")
    month = models.DateField()
    income_minor = models.BigIntegerField()
    spending_minor = models.BigIntegerField()
    source = models.CharField(max_length=255)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("member", "month"), name="sheet_month_total_unique_member_month"),
            models.CheckConstraint(condition=Q(month__day=1), name="sheet_month_total_month_start"),
        ]

    def __str__(self):
        return f"Sheet month {self.member_id} {self.month}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute

