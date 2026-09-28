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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
        ).distinct()


class Account(ArchivableModel):
    class Type(models.TextChoices):
        CHECKING = "checking", "Checking"
        SAVINGS = "savings", "Savings"
        CREDIT_CARD = "credit_card", "Credit card"
        INVESTMENT = "investment", "Investment"

    class Scope(models.TextChoices):
        PRIVATE = "private", "Private"
        HOUSEHOLD = "household", "Household"

    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=11, choices=Type)
    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="owned_accounts")
    scope = models.CharField(max_length=9, choices=Scope, default=Scope.PRIVATE)
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
