from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


sha256_validator = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="Enter a lowercase hexadecimal SHA-256 digest.",
)


def validate_json_object(value):
    if not isinstance(value, dict):
        raise ValidationError("Original imported fields must be a JSON object.")


def validate_reason_list(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("Reasons must be a list of strings.")


class ArchivableModel(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    status = models.CharField(max_length=8, choices=Status, default=Status.ACTIVE)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Person(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="person",
    )
    display_name = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.display_name


class Household(models.Model):
    name = models.CharField(max_length=150)
    transfer_match_window_days = models.PositiveSmallIntegerField(default=5)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(transfer_match_window_days__lte=366),
                name="household_transfer_window_days_range",
            ),
        ]

    def __str__(self):
        return self.name


class Membership(models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="memberships")
    household = models.ForeignKey(Household, on_delete=models.PROTECT, related_name="memberships")
    joined_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("person",),
                condition=Q(ended_at__isnull=True),
                name="one_current_household_per_person",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("joined_at")),
                name="membership_end_not_before_join",
            ),
        ]

    def __str__(self):
        return f"{self.person} in {self.household}"


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


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            return None
    return None


class AccountQuerySet(models.QuerySet):
    def visible_to(self, principal):
        person = _person_for(principal)
        if person is None:
            return self.none()
        current_households = Membership.objects.filter(
            person=person,
            ended_at__isnull=True,
        ).values("household_id")
        return self.filter(
            Q(owner=person, scope="private")
            | Q(scope="household", household_id__in=current_households)
        )


class Account(ArchivableModel):
    class Type(models.TextChoices):
        CHECKING = "checking", "Checking"
        SAVINGS = "savings", "Savings"
        CREDIT_CARD = "credit_card", "Credit card"
        INVESTMENT = "investment", "Investment"

    class Scope(models.TextChoices):
        PRIVATE = "private", "Private"
        HOUSEHOLD = "household", "Household"

    class ShareMode(models.TextChoices):
        CO_OWNED = "co_owned", "Co-owned"
        LENT = "lent", "Lent"

    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=11, choices=Type)
    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="owned_accounts")
    scope = models.CharField(max_length=9, choices=Scope, default=Scope.PRIVATE)
    share_mode = models.CharField(max_length=8, choices=ShareMode, blank=True, default="")
    household = models.ForeignKey(
        Household,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accounts",
    )
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = AccountQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(scope="private", household__isnull=True) | Q(scope="household", household__isnull=False)),
                name="account_scope_matches_household",
            ),
            models.CheckConstraint(
                condition=(
                    Q(scope="private", share_mode="")
                    | Q(scope="household", share_mode__in=("co_owned", "lent"))
                ),
                name="account_share_mode_matches_scope",
            ),
            models.CheckConstraint(condition=Q(currency="USD"), name="account_currency_usd"),
            models.CheckConstraint(
                condition=Q(account_type__in=("checking", "savings", "credit_card", "investment")),
                name="account_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="account_archive_state_consistent",
            ),
        ]

    def __str__(self):
        return self.name


class ImportBatch(ArchivableModel):
    class Source(models.TextChoices):
        HUNTINGTON = "huntington", "Huntington Bank"
        CAPITAL_ONE = "capital_one", "Capital One"
        APPLE_CARD = "apple_card", "Apple Card"
        VANGUARD = "vanguard", "Vanguard"

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="import_batches")
    imported_by = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="import_batches")
    source = models.CharField(max_length=32, choices=Source)
    source_file_sha256 = models.CharField(max_length=64, validators=(sha256_validator,))
    date_range_start = models.DateField()
    date_range_end = models.DateField()
    imported_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_accounts = Account.objects.visible_to(principal).values("pk")
            return self.filter(account_id__in=visible_accounts)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(source__in=("huntington", "capital_one", "apple_card", "vanguard")),
                name="import_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(date_range_end__gte=F("date_range_start")),
                name="import_date_range_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="import_batch_archive_state_consistent",
            ),
        ]

    def __str__(self):
        return f"{self.get_source_display()} import {self.pk}"


class Transaction(ArchivableModel):
    class Kind(models.TextChoices):
        CASH_FLOW = "cash_flow", "Cash flow"
        INVESTMENT_ACTIVITY = "investment_activity", "Investment activity (neutral)"

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="transactions")
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="transactions")
    transaction_date = models.DateField()
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    description = models.TextField()
    kind = models.CharField(max_length=19, choices=Kind, default=Kind.CASH_FLOW)
    source_row_number = models.PositiveIntegerField()
    source_transaction_id = models.CharField(max_length=255, blank=True)
    fingerprint = models.CharField(max_length=64, validators=(sha256_validator,), db_index=True)
    original_fields = models.JSONField(validators=(validate_json_object,))
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    class CategorySource(models.TextChoices):
        UNSET = "", "Unset"
        MANUAL = "manual", "Manual"
        RULE = "rule", "Rule"
        INHERITED = "inherited", "Inherited"

    category_source = models.CharField(
        max_length=9,
        choices=CategorySource,
        default=CategorySource.UNSET,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_accounts = Account.objects.visible_to(principal).values("pk")
            return self.filter(account_id__in=visible_accounts)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("account", "transaction_date"), name="txn_account_date_idx"),
            models.Index(fields=("account", "source_transaction_id"), name="txn_account_source_id_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(currency="USD"), name="transaction_currency_usd"),
            models.CheckConstraint(
                condition=Q(kind__in=("cash_flow", "investment_activity")),
                name="transaction_kind_valid",
            ),
            models.CheckConstraint(condition=Q(source_row_number__gt=0), name="transaction_source_row_positive"),
            models.CheckConstraint(
                condition=Q(category_source__in=("", "manual", "rule", "inherited")),
                name="transaction_category_source_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="transaction_archive_state_consistent",
            ),
        ]

    def clean(self):
        super().clean()
        if self.account_id and self.import_batch_id and self.import_batch.account_id != self.account_id:
            raise ValidationError({"import_batch": "Import batch must belong to the transaction account."})

    def __str__(self):
        return f"{self.transaction_date}: {self.amount_minor} {self.currency}"

    @property
    def amount_display(self):
        amount = Decimal(self.amount_minor) / Decimal(100)
        return f"{amount:,.2f} {self.currency}"

    @property
    def category_display(self):
        if self.is_excluded_transfer:
            return "Transfer"
        if self.category_id is None:
            return "Uncategorized"
        return self.category.name

    @property
    def is_excluded_transfer(self):
        annotated = getattr(self, "_excluded", None)
        if annotated is not None:
            return bool(annotated)
        pairs = getattr(self, "_prefetched_exclusion_pairs", None)
        if pairs is not None:
            return any(
                pair.is_active_exclusion
                and pair.leg_a.status == Transaction.Status.ACTIVE
                and pair.leg_b.status == Transaction.Status.ACTIVE
                for pair in pairs
            )
        return TransferPair.objects.excluding_income_and_spending().filter(
            Q(leg_a=self) | Q(leg_b=self)
        ).exists()


class TransactionCorrectionHistory(models.Model):
    """Append-only record of one corrected field. Never log these values."""

    class Field(models.TextChoices):
        TRANSACTION_DATE = "transaction_date", "Date"
        DESCRIPTION = "description", "Description"
        AMOUNT_MINOR = "amount_minor", "Amount"
        CATEGORY = "category", "Category"
        EXCLUSION = "exclusion", "Transfer exclusion"
        REFUND_LINK = "refund_link", "Refund link"

    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.PROTECT,
        related_name="correction_history",
    )
    actor = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="transaction_correction_history")
    recorded_at = models.DateTimeField(default=timezone.now)
    field_name = models.CharField(max_length=16, choices=Field)
    previous_date = models.DateField(null=True, blank=True)
    new_date = models.DateField(null=True, blank=True)
    previous_description = models.TextField(blank=True, default="")
    new_description = models.TextField(blank=True, default="")
    previous_amount_minor = models.BigIntegerField(null=True, blank=True)
    new_amount_minor = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")
            return self.filter(transaction_id__in=visible_transactions)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("transaction", "recorded_at"), name="txn_corr_hist_txn_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        field_name="transaction_date",
                        previous_date__isnull=False,
                        new_date__isnull=False,
                        previous_description="",
                        new_description="",
                        previous_amount_minor__isnull=True,
                        new_amount_minor__isnull=True,
                        currency="",
                    )
                    | Q(
                        field_name__in=("description", "category", "exclusion", "refund_link"),
                        previous_date__isnull=True,
                        new_date__isnull=True,
                        previous_amount_minor__isnull=True,
                        new_amount_minor__isnull=True,
                        currency="",
                    )
                    | Q(
                        field_name="amount_minor",
                        previous_date__isnull=True,
                        new_date__isnull=True,
                        previous_description="",
                        new_description="",
                        previous_amount_minor__isnull=False,
                        new_amount_minor__isnull=False,
                        currency="USD",
                    )
                ),
                name="correction_history_value_shape",
            ),
        ]

    def __str__(self):
        return f"Correction of {self.field_name}"

    def _amount_display(self, minor):
        amount = Decimal(minor) / Decimal(100)
        return f"{amount:,.2f} {self.currency}"

    @property
    def previous_display(self):
        if self.field_name == self.Field.TRANSACTION_DATE:
            return str(self.previous_date)
        if self.field_name in (
            self.Field.DESCRIPTION,
            self.Field.CATEGORY,
            self.Field.EXCLUSION,
            self.Field.REFUND_LINK,
        ):
            return self.previous_description
        return self._amount_display(self.previous_amount_minor)

    @property
    def new_display(self):
        if self.field_name == self.Field.TRANSACTION_DATE:
            return str(self.new_date)
        if self.field_name in (
            self.Field.DESCRIPTION,
            self.Field.CATEGORY,
            self.Field.EXCLUSION,
            self.Field.REFUND_LINK,
        ):
            return self.new_description
        return self._amount_display(self.new_amount_minor)


class TransferPair(models.Model):
    class Status(models.TextChoices):
        SUGGESTED = "suggested", "Suggested"
        AUTO_MARKED = "auto_marked", "Auto-marked"
        CONFIRMED = "confirmed", "Confirmed"
        DISMISSED = "dismissed", "Dismissed"
        UNDONE = "undone", "Undone"

    class Kind(models.TextChoices):
        TRANSFER = "transfer", "Transfer"
        CARD_PAYMENT = "card_payment", "Credit-card payment"

    class Confidence(models.TextChoices):
        HIGH = "high", "High"
        LOW = "low", "Low"

    leg_a = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="transfer_pairs_as_a")
    leg_b = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="transfer_pairs_as_b")
    status = models.CharField(max_length=11, choices=Status)
    kind = models.CharField(max_length=12, choices=Kind)
    confidence = models.CharField(max_length=4, choices=Confidence)
    reasons = models.JSONField(validators=(validate_reason_list,))
    leg_a_category_id_at_mark = models.PositiveIntegerField(null=True, blank=True)
    leg_b_category_id_at_mark = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")
            return self.filter(leg_a_id__in=visible_transactions, leg_b_id__in=visible_transactions)

        def excluding_income_and_spending(self):
            return self.filter(
                status__in=(TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED),
                leg_a__status=Transaction.Status.ACTIVE,
                leg_b__status=Transaction.Status.ACTIVE,
            )

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(leg_a_id__lt=F("leg_b_id")), name="transfer_pair_leg_order"),
            models.UniqueConstraint(fields=("leg_a", "leg_b"), name="transfer_pair_unique_legs"),
            models.CheckConstraint(
                condition=Q(status__in=("suggested", "auto_marked", "confirmed", "dismissed", "undone")),
                name="transfer_pair_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=("transfer", "card_payment")),
                name="transfer_pair_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(confidence__in=("high", "low")),
                name="transfer_pair_confidence_valid",
            ),
        ]

    def __str__(self):
        return f"Transfer pair {self.leg_a_id}/{self.leg_b_id}"

    @property
    def is_active_exclusion(self):
        return self.status in (self.Status.AUTO_MARKED, self.Status.CONFIRMED)


class RefundLink(models.Model):
    refund = models.OneToOneField(Transaction, on_delete=models.PROTECT, related_name="refund_link")
    original = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="refunds")
    created_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")
            return self.filter(refund_id__in=visible_transactions, original_id__in=visible_transactions)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(refund=F("original")), name="refund_link_distinct_transactions"),
        ]

    def __str__(self):
        return f"Refund {self.refund_id} for {self.original_id}"


class RecurringSeries(models.Model):
    class Status(models.TextChoices):
        POSSIBLE = "possible", "Possible"
        SUGGESTED = "suggested", "Suggested"
        CONFIRMED = "confirmed", "Confirmed"
        DISMISSED = "dismissed", "Dismissed"

    class Cadence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Biweekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        ANNUAL = "annual", "Annual"

    class Confidence(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="recurring_series")
    merchant_key = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    cadence = models.CharField(max_length=9, choices=Cadence)
    typical_amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=10, choices=Status)
    confidence = models.CharField(max_length=6, choices=Confidence)
    reasons = models.JSONField(validators=(validate_reason_list,))
    fingerprint = models.CharField(max_length=64, validators=(sha256_validator,))
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            visible_transactions = Transaction.objects.visible_to(person).values("pk")
            hidden_members = RecurringSeriesMember.objects.filter(
                series_id=models.OuterRef("pk"),
            ).exclude(transaction_id__in=visible_transactions)
            return self.filter(person=person).exclude(models.Exists(hidden_members))

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "fingerprint"), name="recurring_series_person_fingerprint"),
            models.CheckConstraint(condition=Q(currency="USD"), name="recurring_series_currency_usd"),
            models.CheckConstraint(
                condition=Q(status__in=("possible", "suggested", "confirmed", "dismissed")),
                name="recurring_series_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(cadence__in=("weekly", "biweekly", "monthly", "quarterly", "annual")),
                name="recurring_series_cadence_valid",
            ),
            models.CheckConstraint(
                condition=Q(confidence__in=("high", "medium", "low")),
                name="recurring_series_confidence_valid",
            ),
            models.CheckConstraint(
                condition=Q(typical_amount_minor__lt=0),
                name="recurring_series_amount_is_charge",
            ),
        ]

    def __str__(self):
        return f"{self.display_name} {self.cadence}"

    @property
    def amount_display(self):
        amount = Decimal(self.typical_amount_minor) / Decimal(100)
        return f"{amount:,.2f} {self.currency}"

    @property
    def annual_minor(self):
        per_year = {
            self.Cadence.WEEKLY: 52,
            self.Cadence.BIWEEKLY: 26,
            self.Cadence.MONTHLY: 12,
            self.Cadence.QUARTERLY: 4,
            self.Cadence.ANNUAL: 1,
        }[self.cadence]
        return abs(self.typical_amount_minor) * per_year

    @property
    def monthly_minor(self):
        return self.annual_minor // 12

    @property
    def monthly_display(self):
        amount = Decimal(self.monthly_minor) / Decimal(100)
        return f"{amount:,.2f} {self.currency}"

    @property
    def annual_display(self):
        amount = Decimal(self.annual_minor) / Decimal(100)
        return f"{amount:,.2f} {self.currency}"


class RecurringSeriesMember(models.Model):
    series = models.ForeignKey(RecurringSeries, on_delete=models.CASCADE, related_name="members")
    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="recurring_memberships")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("series", "transaction"), name="recurring_member_unique"),
        ]

    def __str__(self):
        return f"Series {self.series_id} txn {self.transaction_id}"


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
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="category_rules")
    priority = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)
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
        ]

    def __str__(self):
        return self.description_contains


class RuleApplication(models.Model):
    rule = models.ForeignKey(CategoryRule, on_delete=models.PROTECT, related_name="applications")
    applied_by = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="rule_applications")
    applied_at = models.DateTimeField(default=timezone.now)
    reversed_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_rules = CategoryRule.objects.visible_to(principal).values("pk")
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

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")
            return self.filter(transaction_id__in=visible_transactions)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("application", "transaction"), name="rule_application_entry_unique_txn"),
            models.CheckConstraint(
                condition=Q(previous_category_source__in=("", "manual", "rule", "inherited")),
                name="rule_application_prev_source_valid",
            ),
        ]

    def __str__(self):
        return f"Entry {self.pk}"


class Invitation(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="invitations")
    invited_by = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="invitations_created")
    token_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class RecoveryCode(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_codes")
    code_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)


class LoginThrottle(models.Model):
    key_digest = models.CharField(max_length=64, unique=True)
    failure_count = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True, blank=True)
