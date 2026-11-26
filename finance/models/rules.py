from ..input_limits import AUTO_NOTE_MAX_CHARS

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from .base import actor_display_name
from .people import Household, Membership, Person, person_for
from .categories import Category
from .accounts import Account
from .ledger import Transaction


class CategoryRule(models.Model):
    owner_person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="category_rules",
    )
    owner_household = models.ForeignKey(
        Household,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="category_rules",
    )
    description_contains = models.CharField(max_length=200)
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="category_rules",
    )
    min_amount_minor = models.BigIntegerField(null=True, blank=True)
    max_amount_minor = models.BigIntegerField(null=True, blank=True)

    class Action(models.TextChoices):
        CATEGORIZE = "categorize", "Set category"
        UNTRACKED_TRANSFER = "untracked_transfer", "Mark as untracked transfer"

    # A rule either sets a category or marks rows as transfers to or from an
    # untracked account (#642); it never assigns the system Transfer category.
    action = models.CharField(max_length=18, choices=Action, default=Action.CATEGORIZE)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, null=True, blank=True, related_name="category_rules"
    )
    # The household whose accounts the rule may reach. A categorize rule takes it
    # from its category; an untracked-transfer rule has no category, so it is stored.
    scope_household = models.ForeignKey(
        Household, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    priority = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)
    # Set when the member confirms the preview with "Apply"; cleared on edit.
    # Only confirmed rules apply automatically to imports and syncs.
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # A one-line note added to each new transaction the rule applies to (#684).
    note = models.CharField(max_length=AUTO_NOTE_MAX_CHARS, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = person_for(principal)
            if person is None:
                return self.none()
            current_households = Membership.objects.filter(
                person=person,
                ended_at__isnull=True,
            ).values("household_id")
            return self.filter(
                Q(owner_person=person)
                | Q(owner_household_id__in=current_households)
            )

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("owner_person", "priority"), name="cat_rule_person_priority_idx"),
            models.Index(fields=("owner_household", "priority"), name="cat_rule_hh_priority_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_person__isnull=False, owner_household__isnull=True)
                    | Q(owner_person__isnull=True, owner_household__isnull=False)
                ),
                name="category_rule_exactly_one_owner",
            ),
            models.CheckConstraint(
                condition=~Q(description_contains=""),
                name="category_rule_description_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(min_amount_minor__isnull=True)
                    | Q(max_amount_minor__isnull=True)
                    | Q(min_amount_minor__lte=F("max_amount_minor"))
                ),
                name="category_rule_amount_range_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    Q(action="categorize", category__isnull=False, scope_household__isnull=True)
                    | Q(action="untracked_transfer", category__isnull=True, scope_household__isnull=False)
                ),
                name="category_rule_action_target",
            ),
        ]

    def __str__(self):
        return self.description_contains

    @property
    def marks_untracked_transfer(self):
        return self.action == self.Action.UNTRACKED_TRANSFER

    @property
    def rule_household_id(self):
        """The household a personal rule's household-account matches must belong to."""
        # Django adds each foreign key's <name>_id attribute at runtime; pyright runs without Django stubs.
        category = self.category
        if category is not None:
            return category.household_id  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        return self.scope_household_id  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class RuleApplication(models.Model):
    rule = models.ForeignKey(CategoryRule, on_delete=models.PROTECT, related_name="applications")
    applied_by = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rule_applications",
    )
    applied_at = models.DateTimeField(default=timezone.now)
    reversed_at = models.DateTimeField(null=True, blank=True)

    @property
    def applied_by_label(self):
        return actor_display_name(self.applied_by)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_rules = CategoryRule.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(rule_id__in=visible_rules)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"Application {self.pk}"


class RuleApplicationEntry(models.Model):
    application = models.ForeignKey(RuleApplication, on_delete=models.PROTECT, related_name="entries")
    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="rule_application_entries")
    previous_category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    previous_category_source = models.CharField(max_length=9, blank=True, default="")
    # True when this application marked the row as an untracked transfer (#642)
    # rather than changing its category; reversing it removes the mark.
    marked_untracked = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(transaction_id__in=visible_transactions)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("application", "transaction"), name="rule_application_entry_unique_txn"),
            models.CheckConstraint(
                condition=Q(previous_category_source__in=("", "manual", "rule", "inherited", "split")),
                name="rule_application_prev_source_valid",
            ),
        ]

    def __str__(self):
        return f"Entry {self.pk}"

