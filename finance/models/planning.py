from ..input_limits import BUDGET_MONTH_FLOOR
from ..money_display import format_minor

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from .base import ArchivableModel
from .people import Household, Person, PrivateOrHouseholdQuerySet as _PrivateOrHouseholdQuerySet
from .categories import Category
from .accounts import Account
from .ledger import Transaction
from .recurring import RecurringSeries


class PlannedItem(models.Model):
    class Scope(models.TextChoices):
        PRIVATE = "private", "Private"
        HOUSEHOLD = "household", "Household"

    class Kind(models.TextChoices):
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    class Cadence(models.TextChoices):
        ONE_TIME = "one_time", "One-time"
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        ANNUAL = "annual", "Annual"

    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="planned_items")
    scope = models.CharField(max_length=9, choices=Scope, default=Scope.PRIVATE)
    household = models.ForeignKey(
        Household,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="planned_items",
    )
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=7, choices=Kind)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    cadence = models.CharField(max_length=9, choices=Cadence)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_items",
    )
    replaces_series = models.ForeignKey(
        RecurringSeries,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replaced_by_planned_items",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(_PrivateOrHouseholdQuerySet):
        pass

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(scope="private", household__isnull=True) | Q(scope="household", household__isnull=False)),
                name="planned_item_scope_matches_household",
            ),
            models.CheckConstraint(condition=Q(currency="USD"), name="planned_item_currency_usd"),
            models.CheckConstraint(condition=Q(kind__in=("income", "expense")), name="planned_item_kind_valid"),
            models.CheckConstraint(
                condition=Q(cadence__in=("one_time", "weekly", "biweekly", "monthly", "quarterly", "annual")),
                name="planned_item_cadence_valid",
            ),
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="planned_item_amount_positive"),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="planned_item_end_on_or_after_start",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def amount_display(self):
        return format_minor(self.amount_minor, self.currency)


class SavingsGoalQuerySet(_PrivateOrHouseholdQuerySet):
    pass


class SavingsGoal(ArchivableModel):
    class Scope(models.TextChoices):
        PRIVATE = "private", "Private"
        HOUSEHOLD = "household", "Household"

    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="savings_goals")
    scope = models.CharField(max_length=9, choices=Scope, default=Scope.PRIVATE)
    household = models.ForeignKey(
        Household,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="savings_goals",
    )
    name = models.CharField(max_length=150)
    target_amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    target_date = models.DateField(null=True, blank=True)
    # Wishlist rank, 1 = first; null sorts after every ranked goal.
    priority = models.PositiveIntegerField(null=True, blank=True)
    depends_on = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dependents",
    )
    time_sensitive = models.BooleanField(default=False)
    linked_account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="savings_goals",
    )
    manual_amount_minor = models.BigIntegerField(null=True, blank=True)
    manual_amount_date = models.DateField(null=True, blank=True)
    # Sinking fund (#670): the goal holds a share of linked_account. Its current
    # amount is the sum of its FundEntry rows, not the account's whole balance.
    # A share goal whose account was deleted keeps its entries, so the account
    # requirement lives in the service, not a constraint.
    share_of_account = models.BooleanField(default=False)
    funds_planned_item = models.OneToOneField(
        PlannedItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="funding_goal",
    )
    # Only transactions on or after this date can draw the fund down.
    planned_item_linked_on = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SavingsGoalQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(scope="private", household__isnull=True) | Q(scope="household", household__isnull=False)),
                name="savings_goal_scope_matches_household",
            ),
            models.CheckConstraint(condition=Q(currency="USD"), name="savings_goal_currency_usd"),
            models.CheckConstraint(condition=Q(target_amount_minor__gt=0), name="savings_goal_target_positive"),
            models.CheckConstraint(
                condition=Q(priority__isnull=True) | Q(priority__gte=1),
                name="savings_goal_priority_positive",
            ),
            models.CheckConstraint(
                condition=Q(depends_on__isnull=True) | ~Q(depends_on=models.F("pk")),
                name="savings_goal_not_self_dependent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(manual_amount_minor__isnull=True, manual_amount_date__isnull=True)
                    | Q(manual_amount_minor__isnull=False, manual_amount_date__isnull=False)
                ),
                name="savings_goal_manual_amount_paired_with_date",
            ),
            models.CheckConstraint(
                condition=Q(share_of_account=False) | Q(manual_amount_minor__isnull=True),
                name="savings_goal_share_has_no_manual_amount",
            ),
            models.CheckConstraint(
                condition=Q(funds_planned_item__isnull=True) | Q(share_of_account=True),
                name="savings_goal_planned_item_requires_share",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="savings_goal_archive_state_consistent",
            ),
        ]

    def __str__(self):
        return self.name


def _detach_draw_downs(collector, field, sub_objs, using):
    """A draw-down's transaction is deleted (with its account): keep the draw-down, unlinked.

    One whose transaction was already archived (an undone import) did not count,
    so it is marked undone first; otherwise losing the link would make it count again.
    """
    archived = Transaction.objects.using(using).filter(
        pk__in=[entry.transaction_id for entry in sub_objs if entry.transaction_id is not None],  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        status=Transaction.Status.ARCHIVED,
    ).values_list("pk", flat=True)
    archived = set(archived)
    stale = [entry for entry in sub_objs if entry.transaction_id in archived and entry.undone_at is None]  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
    if stale:
        collector.add_field_update(FundEntry._meta.get_field("undone_at"), timezone.now(), stale)
    models.SET_NULL(collector, field, sub_objs, using)


class FundEntry(models.Model):
    """One change to a sinking fund's assigned amount (#670); the rows are its history.

    Assignments add (positive), releases and draw-downs take out (negative). A
    draw-down records the transaction that paid the linked planned item; undoing
    it keeps the row, so the same transaction never draws the fund down again.
    """

    class Kind(models.TextChoices):
        ASSIGN = "assign", "Assigned"
        RELEASE = "release", "Released"
        DRAW_DOWN = "draw_down", "Drawn down"

    class Reason(models.TextChoices):
        MANUAL = "manual", "By hand"
        MONTHLY = "monthly", "Monthly set-aside"
        BALANCE_DROP = "balance_drop", "Balance dropped below assigned"
        PLANNED = "planned", "Planned expense posted"

    goal = models.ForeignKey(SavingsGoal, on_delete=models.CASCADE, related_name="fund_entries")
    kind = models.CharField(max_length=9, choices=Kind)
    reason = models.CharField(max_length=12, choices=Reason)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    entry_date = models.DateField()
    # First of the month a monthly set-aside was confirmed for.
    month = models.DateField(null=True, blank=True)
    transaction = models.ForeignKey(
        Transaction,
        on_delete=_detach_draw_downs,
        null=True,
        blank=True,
        related_name="fund_draw_downs",
    )
    # The planned item, and its occurrence, the draw-down paid. A fund relinked
    # to another item starts a fresh set of occurrences.
    planned_item = models.ForeignKey(
        PlannedItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="fund_draw_downs"
    )
    occurrence_date = models.DateField(null=True, blank=True)
    actor = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, blank=True, related_name="fund_entries"
    )
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, blank=True, related_name="undone_fund_entries"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def counted_filter():
        """Entries that make up the assigned amount.

        An undone draw-down, or one whose transaction was archived (an undone
        import), no longer counts. A draw-down whose transaction was deleted
        with its account still counts: the payment happened.
        """
        return Q(undone_at__isnull=True) & (Q(transaction__isnull=True) | Q(transaction__status="active"))

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(currency="USD"), name="fund_entry_currency_usd"),
            models.CheckConstraint(
                condition=Q(kind__in=("assign", "release", "draw_down")), name="fund_entry_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(reason__in=("manual", "monthly", "balance_drop", "planned")),
                name="fund_entry_reason_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="assign", amount_minor__gt=0, reason__in=("manual", "monthly"))
                    | Q(kind="release", amount_minor__lt=0, reason__in=("manual", "balance_drop"))
                    # A payment that found the fund empty draws nothing but is still recorded.
                    | Q(kind="draw_down", amount_minor__lte=0, reason="planned")
                ),
                name="fund_entry_kind_amount_reason",
            ),
            models.CheckConstraint(
                condition=Q(month__isnull=True) | Q(reason="monthly"),
                name="fund_entry_month_only_monthly",
            ),
            models.CheckConstraint(
                condition=Q(reason="monthly", month__isnull=False) | ~Q(reason="monthly"),
                name="fund_entry_monthly_has_month",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="draw_down", occurrence_date__isnull=False)
                    | Q(occurrence_date__isnull=True, transaction__isnull=True, planned_item__isnull=True,
                        undone_at__isnull=True)
                    & ~Q(kind="draw_down")
                ),
                name="fund_entry_draw_down_fields",
            ),
            # One draw-down per transaction ever, undone or not.
            models.UniqueConstraint(
                fields=("transaction",),
                condition=Q(transaction__isnull=False),
                name="fund_entry_unique_transaction",
            ),
            models.UniqueConstraint(
                fields=("goal", "month"),
                condition=Q(month__isnull=False),
                name="fund_entry_unique_goal_month",
            ),
            models.UniqueConstraint(
                fields=("goal", "planned_item", "occurrence_date"),
                condition=Q(kind="draw_down", undone_at__isnull=True, planned_item__isnull=False),
                name="fund_entry_unique_live_occurrence",
            ),
        ]
        indexes = [models.Index(fields=("goal", "entry_date"), name="fund_entry_goal_date_idx")]

    def __str__(self):
        return f"Fund entry {self.pk}"

    @property
    def amount_display(self):
        return format_minor(self.amount_minor, self.currency)


class BudgetQuerySet(_PrivateOrHouseholdQuerySet):
    pass


class Budget(ArchivableModel):
    class Scope(models.TextChoices):
        PRIVATE = "private", "Private"
        HOUSEHOLD = "household", "Household"

    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="budgets")
    scope = models.CharField(max_length=9, choices=Scope, default=Scope.PRIVATE)
    household = models.ForeignKey(
        Household,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="budgets",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="budgets",
    )
    rollover_enabled = models.BooleanField(default=False)
    rollover_started_month = models.DateField(null=True, blank=True)
    rollover_enabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BudgetQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(scope="private", household__isnull=True) | Q(scope="household", household__isnull=False)),
                name="budget_scope_matches_household",
            ),
            models.CheckConstraint(
                condition=(
                    Q(rollover_enabled=False, rollover_started_month__isnull=True)
                    | Q(rollover_enabled=True, rollover_started_month__isnull=False)
                ),
                name="budget_rollover_start_when_enabled",
            ),
            models.CheckConstraint(
                condition=Q(rollover_started_month__isnull=True) | Q(rollover_started_month__gte=BUDGET_MONTH_FLOOR),
                name="budget_rollover_month_floor",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="budget_archive_state_consistent",
            ),
            models.UniqueConstraint(
                fields=("owner", "category"),
                condition=Q(scope="private", status="active", category__isnull=False),
                name="budget_one_active_private_category",
            ),
            models.UniqueConstraint(
                fields=("owner",),
                condition=Q(scope="private", status="active", category__isnull=True),
                name="budget_one_active_private_total",
            ),
            models.UniqueConstraint(
                fields=("household", "category"),
                condition=Q(scope="household", status="active", category__isnull=False),
                name="budget_one_active_household_category",
            ),
            models.UniqueConstraint(
                fields=("household",),
                condition=Q(scope="household", status="active", category__isnull=True),
                name="budget_one_active_household_total",
            ),
        ]

    def __str__(self):
        if self.category_id is None:  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
            return "Overall spending"
        return self.category.name  # pyright: ignore[reportOptionalMemberAccess] - category_id is checked just above, which pyright cannot connect to category


class BudgetAmount(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="amounts")
    effective_month = models.DateField()
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("budget", "effective_month"), name="budget_amount_unique_month"),
            models.CheckConstraint(condition=Q(currency="USD"), name="budget_amount_currency_usd"),
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="budget_amount_positive"),
            models.CheckConstraint(condition=Q(effective_month__day=1), name="budget_amount_month_start"),
        ]

    def __str__(self):
        return f"Budget {self.budget_id} from {self.effective_month}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class BudgetRolloverReset(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="rollover_resets")
    month = models.DateField()
    actor = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_rollover_resets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("budget", "month"), name="budget_rollover_reset_unique_month"),
            models.CheckConstraint(condition=Q(month__day=1), name="budget_rollover_reset_month_start"),
        ]

    def __str__(self):
        return f"Rollover reset {self.budget_id} {self.month}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute



# Public name for finance/models/__init__.py, which re-exports the private name
# for historical migrations (#759); see the people module's note.
detach_draw_downs = _detach_draw_downs
