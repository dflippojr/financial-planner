import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone


sha256_validator = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="Enter a lowercase hexadecimal SHA-256 digest.",
)


def validate_json_object(value):
    if not isinstance(value, dict):
        raise ValidationError("Original imported fields must be a JSON object.")


def validate_bulk_edit_snapshot(value):
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        raise ValidationError("Bulk edit snapshot must be an object with a row list.")


def validate_reason_list(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("Reasons must be a list of strings.")


FORMER_MEMBER_LABEL = "former member"


def actor_display_name(person):
    if person is None:
        return FORMER_MEMBER_LABEL
    return person.display_name


def validate_header_name_list(value):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValidationError("Column names must be a list of non-empty strings.")


def validate_stored_csv_headers(value):
    validate_header_name_list(value)
    if not value:
        raise ValidationError("A saved mapping must store the file's header list.")


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
    sessions_valid_after = models.DateTimeField(null=True, blank=True)
    privacy_policy_declined_version = models.ForeignKey(
        "PrivacyPolicyVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="declined_by",
    )
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
    person = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
    )
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
            models.CheckConstraint(
                condition=Q(person__isnull=False) | Q(ended_at__isnull=False),
                name="membership_person_required_while_current",
            ),
        ]

    def __str__(self):
        return f"{actor_display_name(self.person)} in {self.household}"


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


class SavedCsvMapping(ArchivableModel):
    household = models.ForeignKey(Household, on_delete=models.PROTECT, related_name="saved_csv_mappings")
    name = models.CharField(max_length=80)
    headers = models.JSONField(validators=(validate_stored_csv_headers,))
    date_column = models.CharField(max_length=255)
    description_column = models.CharField(max_length=255)
    date_format = models.CharField(max_length=16)
    number_format = models.CharField(max_length=16)
    amount_mode = models.CharField(max_length=8)
    amount_column = models.CharField(max_length=255, blank=True, default="")
    debit_column = models.CharField(max_length=255, blank=True, default="")
    credit_column = models.CharField(max_length=255, blank=True, default="")
    currency_column = models.CharField(max_length=255, blank=True, default="")
    invert_sign = models.BooleanField(default=False)
    description_mode = models.CharField(max_length=12, default="column")
    payee_column = models.CharField(max_length=255, blank=True, default="")
    memo_column = models.CharField(max_length=255, blank=True, default="")
    source_id_column = models.CharField(max_length=255, blank=True, default="")
    excluded_original_columns = models.JSONField(default=list, validators=(validate_header_name_list,))
    created_by = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="created_csv_mappings")
    locked_at = models.DateTimeField(null=True, blank=True)
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
            models.UniqueConstraint(
                fields=("household", "name"),
                condition=Q(status="active"),
                name="saved_csv_mapping_unique_active_name_per_household",
            ),
            models.CheckConstraint(
                condition=Q(amount_mode__in=("signed", "separate")),
                name="saved_csv_mapping_amount_mode_valid",
            ),
            models.CheckConstraint(
                condition=Q(description_mode__in=("column", "payee_memo")),
                name="saved_csv_mapping_description_mode_valid",
            ),
            models.CheckConstraint(
                condition=Q(date_format__in=("mdy_slash_4", "mdy_slash_2", "dmy_slash_4", "iso")),
                name="saved_csv_mapping_date_format_valid",
            ),
            models.CheckConstraint(
                condition=Q(number_format__in=("dot_comma", "comma_dot", "dot_none", "comma_none")),
                name="saved_csv_mapping_number_format_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", archived_at__isnull=True)
                    | Q(status="archived", archived_at__isnull=False)
                ),
                name="saved_csv_mapping_archive_state_consistent",
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


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            return None
    return None


def loan_asset_pairing_allowed(loan, asset):
    """True when a loan and asset share scope so an equity line cannot leak."""
    if loan is None or asset is None:
        return False
    if loan.account_type != Account.Type.LOAN:
        return False
    if asset.account_type not in Account.PHYSICAL_ASSET_TYPES:
        return False
    if loan.pk is not None and asset.pk is not None and loan.pk == asset.pk:
        return False
    if loan.scope != asset.scope:
        return False
    if loan.scope == Account.Scope.PRIVATE:
        return loan.owner_id == asset.owner_id
    return loan.household_id is not None and loan.household_id == asset.household_id


def clear_invalid_loan_pairings(account):
    """Drop pairings that no longer share scope after a share or unshare."""
    if account.secured_asset_id and not loan_asset_pairing_allowed(account, account.secured_asset):
        account.secured_asset = None
        account.save(update_fields=("secured_asset", "updated_at"))
    for loan in Account.objects.filter(secured_asset=account).select_related("secured_asset"):
        if not loan_asset_pairing_allowed(loan, account):
            loan.secured_asset = None
            loan.save(update_fields=("secured_asset", "updated_at"))


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

    def for_cash_flow(self):
        return self.exclude(account_type__in=Account.NON_CASH_FLOW_TYPES)


class Account(ArchivableModel):
    class Type(models.TextChoices):
        CHECKING = "checking", "Checking"
        SAVINGS = "savings", "Savings"
        CREDIT_CARD = "credit_card", "Credit card"
        INVESTMENT = "investment", "Investment"
        REAL_ESTATE = "real_estate", "Real estate"
        VEHICLE = "vehicle", "Vehicle"
        PRECIOUS_METALS = "precious_metals", "Precious metals"
        OTHER_ASSET = "other_asset", "Other asset"
        LOAN = "loan", "Loan"

    PHYSICAL_ASSET_TYPES = (
        Type.REAL_ESTATE,
        Type.VEHICLE,
        Type.PRECIOUS_METALS,
        Type.OTHER_ASSET,
    )
    LIABILITY_TYPES = (Type.CREDIT_CARD, Type.LOAN)
    NON_CASH_FLOW_TYPES = PHYSICAL_ASSET_TYPES + (Type.LOAN,)
    SIMPLEFIN_TYPES = (
        Type.CHECKING,
        Type.SAVINGS,
        Type.CREDIT_CARD,
        Type.INVESTMENT,
        Type.LOAN,
    )
    PAIRING_REJECTED = "A loan can only be paired with an asset of the same scope."

    class Scope(models.TextChoices):
        PRIVATE = "private", "Private"
        HOUSEHOLD = "household", "Household"

    class ShareMode(models.TextChoices):
        CO_OWNED = "co_owned", "Co-owned"
        LENT = "lent", "Lent"

    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=16, choices=Type)
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
    secured_asset = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="securing_loans",
    )
    currency = models.CharField(max_length=3, default="USD")
    default_saved_csv_mapping = models.ForeignKey(
        SavedCsvMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_for_accounts",
    )
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
                condition=Q(
                    account_type__in=(
                        "checking",
                        "savings",
                        "credit_card",
                        "investment",
                        "real_estate",
                        "vehicle",
                        "precious_metals",
                        "other_asset",
                        "loan",
                    )
                ),
                name="account_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(secured_asset__isnull=True) | Q(account_type="loan"),
                name="account_secured_asset_requires_loan",
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

    def is_physical_asset(self):
        return self.account_type in self.PHYSICAL_ASSET_TYPES

    def is_liability(self):
        return self.account_type in self.LIABILITY_TYPES

    def accepts_csv_import(self):
        return self.account_type not in self.NON_CASH_FLOW_TYPES

    def accepts_simplefin(self):
        return self.account_type in self.SIMPLEFIN_TYPES

    def validate_secured_asset(self):
        if self.secured_asset_id is None:
            return
        if self.account_type != self.Type.LOAN:
            raise ValidationError(self.PAIRING_REJECTED)
        asset = self.secured_asset
        if not loan_asset_pairing_allowed(self, asset):
            raise ValidationError(self.PAIRING_REJECTED)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "secured_asset" in update_fields:
            self.validate_secured_asset()
        super().save(*args, **kwargs)


class ImportBatch(ArchivableModel):
    class Source(models.TextChoices):
        HUNTINGTON = "huntington", "Huntington Bank"
        CAPITAL_ONE = "capital_one", "Capital One"
        APPLE_CARD = "apple_card", "Apple Card"
        VANGUARD = "vanguard", "Vanguard"
        SIMPLEFIN = "simplefin", "SimpleFIN"

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="import_batches")
    imported_by = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_batches",
    )
    source = models.CharField(max_length=32, choices=Source)
    source_file_sha256 = models.CharField(max_length=64, validators=(sha256_validator,))
    # Set for SimpleFIN sync batches so IDs are unique per remote account, not globally.
    simplefin_account_id = models.CharField(max_length=255, blank=True, default="")
    date_range_start = models.DateField()
    date_range_end = models.DateField()
    imported_at = models.DateTimeField(auto_now_add=True)
    saved_csv_mapping = models.ForeignKey(
        SavedCsvMapping,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="import_batches",
    )

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_accounts = Account.objects.visible_to(principal).values("pk")
            return self.filter(account_id__in=visible_accounts)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    source__in=("huntington", "capital_one", "apple_card", "vanguard", "simplefin")
                ),
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
    note = models.CharField(max_length=2000, blank=True, default="")
    kind = models.CharField(max_length=19, choices=Kind, default=Kind.CASH_FLOW)
    tags = models.ManyToManyField(
        Tag,
        through="TransactionTag",
        related_name="tagged_transactions",
        blank=True,
    )
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
        SPLIT = "split", "Split"

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
                condition=Q(category_source__in=("", "manual", "rule", "inherited", "split")),
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
        if self.category_source == self.CategorySource.SPLIT:
            count = len(self.splits.all())
            return f"Split ({count})"
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


class TransactionTag(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="transaction_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("transaction", "tag"), name="transaction_tag_unique"),
        ]

    def __str__(self):
        return f"{self.transaction_id}:{self.tag_id}"


class TransactionCorrectionHistory(models.Model):
    """Append-only record of one corrected field. Never log these values."""

    class Field(models.TextChoices):
        TRANSACTION_DATE = "transaction_date", "Date"
        DESCRIPTION = "description", "Description"
        AMOUNT_MINOR = "amount_minor", "Amount"
        CATEGORY = "category", "Category"
        EXCLUSION = "exclusion", "Transfer exclusion"
        REFUND_LINK = "refund_link", "Refund link"
        NOTE = "note", "Note"
        TAGS = "tags", "Tags"

    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.PROTECT,
        related_name="correction_history",
    )
    actor = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transaction_correction_history",
    )
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

    @property
    def actor_label(self):
        return actor_display_name(self.actor)

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
                        field_name__in=("description", "category", "exclusion", "refund_link", "note", "tags"),
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
            self.Field.NOTE,
            self.Field.TAGS,
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
            self.Field.NOTE,
            self.Field.TAGS,
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


class TransactionSplit(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="splits")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="transaction_splits")
    amount_minor = models.BigIntegerField()
    position = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "pk")
        constraints = [
            models.UniqueConstraint(fields=("transaction", "position"), name="transaction_split_unique_position"),
            models.CheckConstraint(condition=~Q(amount_minor=0), name="transaction_split_amount_nonzero"),
        ]

    def __str__(self):
        return f"{self.category.name} {self.amount_display}"

    @property
    def amount_display(self):
        amount = Decimal(self.amount_minor) / Decimal(100)
        return f"{amount:,.2f} {self.transaction.currency}"


class RefundLink(models.Model):
    refund = models.OneToOneField(Transaction, on_delete=models.PROTECT, related_name="refund_link")
    original = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="refunds")
    original_part = models.ForeignKey(
        TransactionSplit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refund_links",
    )
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
    cancelled_at = models.DateTimeField(null=True, blank=True)
    acknowledged_amount_minor = models.BigIntegerField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
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
    class Source(models.TextChoices):
        DETECTED = "detected", "Detected"
        MANUAL = "manual", "Manual"

    series = models.ForeignKey(RecurringSeries, on_delete=models.CASCADE, related_name="members")
    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="recurring_memberships")
    source = models.CharField(max_length=8, choices=Source, default=Source.DETECTED)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("series", "transaction"), name="recurring_member_unique"),
            models.CheckConstraint(
                condition=Q(source__in=("detected", "manual")),
                name="recurring_member_source_valid",
            ),
        ]

    def __str__(self):
        return f"Series {self.series_id} txn {self.transaction_id}"


class RecurringExclusion(models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="recurring_exclusions")
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="recurring_exclusions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "transaction"), name="recurring_exclusion_person_transaction"),
        ]

    def __str__(self):
        return f"Exclusion person {self.person_id} txn {self.transaction_id}"


class SimpleFinConnection(models.Model):
    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="simplefin_connections")
    encrypted_access_url = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_result = models.CharField(max_length=500, blank=True)
    disabled = models.BooleanField(default=False)

    class QuerySet(models.QuerySet):
        def owned_by(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(owner=person)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"SimpleFIN connection {self.pk}"


class AccountLink(models.Model):
    class Mode(models.TextChoices):
        TRANSACTIONS = "transactions", "Transactions"
        BALANCES_ONLY = "balances_only", "Balances only"

    connection = models.ForeignKey(SimpleFinConnection, on_delete=models.CASCADE, related_name="links")
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="simplefin_link")
    simplefin_account_id = models.CharField(max_length=255)
    cutover_date = models.DateField()
    mode = models.CharField(max_length=15, choices=Mode)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("connection", "simplefin_account_id"),
                name="account_link_unique_simplefin_account",
            ),
            models.CheckConstraint(
                condition=Q(mode__in=("transactions", "balances_only")),
                name="account_link_mode_valid",
            ),
        ]

    def __str__(self):
        return f"Link {self.pk}"


class BalanceSnapshot(models.Model):
    class Source(models.TextChoices):
        SIMPLEFIN = "simplefin", "SimpleFIN"
        MANUAL = "manual", "Manual"

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="balance_snapshots")
    snapshot_date = models.DateField()
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    source = models.CharField(max_length=16, choices=Source)
    note = models.CharField(max_length=200, blank=True, default="")
    net_contribution_minor = models.BigIntegerField(null=True, blank=True)
    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="balance_snapshots",
    )

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_accounts = Account.objects.visible_to(principal).values("pk")
            return self.filter(account_id__in=visible_accounts)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("account", "snapshot_date", "source"),
                name="balance_snapshot_unique_account_date_source",
            ),
            models.CheckConstraint(condition=Q(currency="USD"), name="balance_snapshot_currency_usd"),
            models.CheckConstraint(
                condition=Q(source__in=("simplefin", "manual")),
                name="balance_snapshot_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(net_contribution_minor__isnull=True) | Q(source="manual"),
                name="balance_snapshot_contribution_requires_manual",
            ),
        ]

    def __str__(self):
        return f"Balance {self.snapshot_date} account {self.account_id}"


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
                Q(owner=person, scope="private")
                | Q(scope="household", household_id__in=current_households)
            )

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
        amount = Decimal(self.amount_minor) / Decimal(100)
        return f"{amount:,.2f} {self.currency}"


class SavingsGoalQuerySet(models.QuerySet):
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
    target_date = models.DateField()
    linked_account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="savings_goals",
    )
    manual_amount_minor = models.BigIntegerField(null=True, blank=True)
    manual_amount_date = models.DateField(null=True, blank=True)
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
                condition=(
                    Q(manual_amount_minor__isnull=True, manual_amount_date__isnull=True)
                    | Q(manual_amount_minor__isnull=False, manual_amount_date__isnull=False)
                ),
                name="savings_goal_manual_amount_paired_with_date",
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
    # Set when the member confirms the preview with "Apply"; cleared on edit.
    # Only confirmed rules apply automatically to imports and syncs.
    confirmed_at = models.DateTimeField(null=True, blank=True)
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
    reversed_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")
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


class Invitation(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="invitations")
    invited_by = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations_created",
    )
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


class PrivacyPolicyVersion(models.Model):
    version = models.PositiveIntegerField(unique=True)
    body = models.TextField()
    is_material = models.BooleanField()
    published_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-version",)

    def __str__(self):
        kind = "material" if self.is_material else "non-material"
        return f"Privacy policy v{self.version} ({kind})"


class PrivacyPolicyAcceptance(models.Model):
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="privacy_policy_acceptances",
    )
    policy_version = models.ForeignKey(
        PrivacyPolicyVersion,
        on_delete=models.PROTECT,
        related_name="acceptances",
    )
    accepted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("person", "policy_version"),
                name="privacy_acceptance_unique_person_version",
            ),
        ]

    def __str__(self):
        return f"{self.person} accepted v{self.policy_version.version}"


class AiProviderConnection(models.Model):
    class Kind(models.TextChoices):
        AGENT_HARNESS = "agent_harness", "Agent Harness"

    owner = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_connections")
    kind = models.CharField(max_length=32, choices=Kind, default=Kind.AGENT_HARNESS)
    base_url = models.CharField(max_length=255)
    encrypted_token = models.BinaryField()
    harness_project = models.CharField(max_length=80, blank=True, default="")
    chat_backend = models.CharField(max_length=32, blank=True, default="")
    background_backend = models.CharField(max_length=32, blank=True, default="")
    chat_model = models.CharField(max_length=80, blank=True, default="")
    background_model = models.CharField(max_length=80, blank=True, default="")
    connected_at = models.DateTimeField(default=timezone.now)
    last_status = models.CharField(max_length=80, blank=True, default="")

    class QuerySet(models.QuerySet):
        def owned_by(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(owner=person)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("owner", "kind"), name="ai_connection_unique_owner_kind"),
            models.CheckConstraint(
                condition=Q(kind__in=("agent_harness",)),
                name="ai_connection_kind_valid",
            ),
        ]

    def __str__(self):
        return f"AI connection {self.pk}"


class AiJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        WAITING_MODEL = "waiting_model", "Waiting for model"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_jobs")
    feature = models.CharField(max_length=40)
    backend = models.CharField(max_length=32, blank=True, default="")
    input_refs = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    harness_session_id = models.CharField(max_length=120, blank=True, default="")
    next_attempt_at = models.DateTimeField(default=timezone.now)
    result_ref = models.CharField(max_length=120, blank=True, default="")
    failure_code = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

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
                condition=Q(
                    status__in=("queued", "waiting_model", "running", "succeeded", "failed")
                ),
                name="ai_job_status_valid",
            ),
        ]

    def __str__(self):
        return f"AI job {self.pk}"


class AiUsageEvent(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_usage_events")
    provider = models.CharField(max_length=32)
    backend = models.CharField(max_length=32)
    feature = models.CharField(max_length=40)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"AI usage {self.pk}"


class AiConversation(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_conversations")
    harness_session_id = models.CharField(max_length=120, blank=True, default="")
    harness_connection = models.CharField(max_length=120, blank=True, default="")
    backend = models.CharField(max_length=32, blank=True, default="")
    title = models.CharField(max_length=120, blank=True, default="")
    used_account_ids = models.JSONField(default=list)
    turn_count = models.PositiveIntegerField(default=0)
    tool_call_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person, expires_at__gt=timezone.now())

        def owned_by(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"AI conversation {self.pk}"


class AiConversationMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        ERROR = "error", "Error"

    conversation = models.ForeignKey(AiConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role)
    content = models.TextField()
    backend = models.CharField(max_length=32, blank=True, default="")
    figures = models.JSONField(default=list)
    notices = models.JSONField(default=list)
    page_context = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(conversation__member=person)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=("user", "assistant", "error")),
                name="ai_conversation_message_role_valid",
            ),
        ]

    def __str__(self):
        return f"AI message {self.pk}"


class CategorySuggestion(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_category_suggestions")
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="ai_suggestions")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="ai_suggestions")
    provider = models.CharField(max_length=32)
    backend = models.CharField(max_length=32)
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)
    snapshot_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            visible = Transaction.objects.visible_to(person).values("pk")
            return self.filter(member=person, transaction_id__in=visible)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("member", "transaction"), name="category_suggestion_unique_member_txn"),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "accepted", "rejected")),
                name="category_suggestion_status_valid",
            ),
        ]

    def __str__(self):
        return f"AI category suggestion {self.pk}"

    @property
    def display_label(self):
        kind = "Agent Harness" if self.provider == "agent_harness" else (self.provider or "AI")
        labels = {
            "local": "Local model",
            "claude": "Claude",
            "codex": "Codex",
            "cursor": "Cursor",
        }
        return f"AI · {kind} · {labels.get(self.backend, self.backend or 'Unknown backend')}"


class BudgetQuerySet(models.QuerySet):
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
        if self.category_id is None:
            return "Overall spending"
        return self.category.name


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
        return f"Budget {self.budget_id} from {self.effective_month}"


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
        return f"Rollover reset {self.budget_id} {self.month}"


class Alert(models.Model):
    class Kind(models.TextChoices):
        SYNC = "sync", "Sync"
        RECURRING_PRICE = "recurring_price", "Recurring price change"
        RECURRING_MISSED = "recurring_missed", "Missed recurring charge"
        BUDGET = "budget", "Budget"
        LARGE_TRANSACTION = "large_transaction", "Large transaction"
        MONTHLY_REVIEW = "monthly_review", "Monthly review"
        BACKUP = "backup", "Backup"
        EXPECTED_BALANCE = "expected_balance", "Expected balance"

    recipient = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="alerts")
    kind = models.CharField(max_length=20, choices=Kind)
    title = models.CharField(max_length=200)
    link = models.CharField(max_length=500)
    dedupe_key = models.CharField(max_length=200)
    account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alerts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("recipient", "dedupe_key"), name="alert_unique_recipient_dedupe"),
            models.CheckConstraint(
                condition=Q(
                    kind__in=(
                        "sync",
                        "recurring_price",
                        "recurring_missed",
                        "budget",
                        "large_transaction",
                        "monthly_review",
                        "backup",
                        "expected_balance",
                    )
                ),
                name="alert_kind_valid",
            ),
        ]
        indexes = [
            models.Index(fields=("recipient", "created_at"), name="alert_recipient_created_idx"),
        ]

    def __str__(self):
        return self.title


class AlertSettings(models.Model):
    person = models.OneToOneField(Person, on_delete=models.PROTECT, related_name="alert_settings")
    sync_enabled = models.BooleanField(default=True)
    recurring_price_enabled = models.BooleanField(default=True)
    recurring_missed_enabled = models.BooleanField(default=True)
    budget_enabled = models.BooleanField(default=True)
    large_transaction_enabled = models.BooleanField(default=True)
    monthly_review_enabled = models.BooleanField(default=True)
    monthly_review_ai_enabled = models.BooleanField(default=True)
    expected_balance_enabled = models.BooleanField(default=False)
    large_transaction_minor = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="USD threshold in minor units. Empty means large-transaction alerts are off.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(large_transaction_minor__isnull=True) | Q(large_transaction_minor__gte=0),
                name="alert_settings_large_threshold_non_negative",
            ),
        ]

    def __str__(self):
        return f"Alert settings for {self.person_id}"


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
        return f"Bills calendar settings for {self.person_id}"


class MonthlyReview(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="monthly_reviews")
    month = models.DateField()
    visibility_key = models.CharField(max_length=64, validators=(sha256_validator,))
    facts = models.JSONField()
    generated_at = models.DateTimeField()
    ai_paragraph = models.TextField(blank=True, default="")
    ai_backend = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "month"), name="monthly_review_person_month"),
            models.CheckConstraint(condition=Q(month__day=1), name="monthly_review_month_start"),
        ]

    def __str__(self):
        return f"Monthly review {self.person_id} {self.month}"


class MemberSecurityEvent(models.Model):
    class EventType(models.TextChoices):
        SIGN_IN_SUCCESS = "sign_in_success", "Signed in"
        SIGN_IN_FAILURE = "sign_in_failure", "Sign-in failed"
        SIGN_OUT = "sign_out", "Signed out"
        RECOVERY_CODE_USED = "recovery_code_used", "Recovery code used"
        PASSKEY_ADDED = "passkey_added", "Passkey added"
        PASSKEY_REMOVED = "passkey_removed", "Passkey removed"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        AI_CONNECTION_CHANGED = "ai_connection_changed", "AI connection changed"
        SIMPLEFIN_CONNECTION_CHANGED = "simplefin_connection_changed", "SimpleFIN connection changed"
        MEMBER_DATA_EXPORT = "member_data_export", "Data exported"
        POLICY_ACCEPTANCE = "policy_acceptance", "Policy accepted"

    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="security_events")
    event_type = models.CharField(max_length=40, choices=EventType)
    occurred_at = models.DateTimeField(default=timezone.now)
    ip_address = models.CharField(max_length=45, blank=True, default="")
    user_agent = models.CharField(max_length=200, blank=True, default="")

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("member", "-occurred_at"), name="sec_event_member_occurred_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    event_type__in=(
                        "sign_in_success",
                        "sign_in_failure",
                        "sign_out",
                        "recovery_code_used",
                        "passkey_added",
                        "passkey_removed",
                        "password_changed",
                        "ai_connection_changed",
                        "simplefin_connection_changed",
                        "member_data_export",
                        "policy_acceptance",
                    )
                ),
                name="member_security_event_type_valid",
            ),
        ]

    def __str__(self):
        return f"Security event {self.pk}"


class MemberSession(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="member_sessions")
    session_key = models.CharField(max_length=40, unique=True)
    ip_address = models.CharField(max_length=45, blank=True, default="")
    user_agent = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    last_activity_at = models.DateTimeField(default=timezone.now)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("member", "-last_activity_at"), name="member_session_activity_idx"),
        ]

    def __str__(self):
        return f"Member session {self.pk}"


class BulkEditUndo(models.Model):
    """Prior values for one bulk edit, held for a short undo window."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="bulk_edit_undos")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    undone_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(validators=(validate_bulk_edit_snapshot,))

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(actor=person)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("actor", "expires_at"), name="bulk_edit_undo_actor_exp_idx"),
        ]

    def __str__(self):
        return f"Bulk edit undo {self.pk}"
