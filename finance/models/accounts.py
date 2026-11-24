
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from .base import (
    ArchivableModel,
    sha256_validator,
    validate_header_name_list,
    validate_json_object,
    validate_stored_csv_headers,
)
from .people import (
    Household,
    Membership,
    Person,
    person_for as _person_for,
    PrivateOrHouseholdQuerySet as _PrivateOrHouseholdQuerySet,
)


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


class AccountQuerySet(_PrivateOrHouseholdQuerySet):
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
    apr_percent = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    minimum_payment_minor = models.BigIntegerField(null=True, blank=True)
    payment_day = models.PositiveSmallIntegerField(null=True, blank=True)
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
                condition=Q(apr_percent__isnull=True) | Q(apr_percent__gte=0),
                name="account_apr_percent_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(minimum_payment_minor__isnull=True) | Q(minimum_payment_minor__gte=0),
                name="account_minimum_payment_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(payment_day__isnull=True) | Q(payment_day__gte=1, payment_day__lte=31),
                name="account_payment_day_range",
            ),
            models.CheckConstraint(
                condition=Q(payment_day__isnull=True) | Q(account_type="loan"),
                name="account_payment_day_requires_loan",
            ),
            models.CheckConstraint(
                condition=(
                    Q(account_type__in=("loan", "credit_card"))
                    | Q(apr_percent__isnull=True, minimum_payment_minor__isnull=True)
                ),
                name="account_debt_terms_require_liability",
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
        if self.secured_asset_id is None:  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
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
        OFX = "ofx", "OFX / QFX"
        MANUAL = "manual", "Manual"

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
    # A SimpleFIN sync batch whose window Bridge capped: the first day the link
    # asked for. Nothing covers the days from here to date_range_start, and the
    # missing-import warning counts the account's history from here (#640).
    requested_from = models.DateField(null=True, blank=True)
    # A file imported while the account's transactions-mode SimpleFIN link was
    # failing (#735, #743): the last day it owns. The sync skips SimpleFIN rows
    # posted on or before the latest value among active batches. Null for every
    # other batch, and cleared when the batch is undone.
    owns_simplefin_through = models.DateField(null=True, blank=True)
    # The valid rows this file import skipped as duplicates (#796): fingerprint
    # to count. Their transactions stay on the batches that stored them; undoing
    # one of those batches moves the rows this file also holds here. Empty for
    # batches committed before migration 0093, and for manual and SimpleFIN ones.
    skipped_duplicates = models.JSONField(default=dict, blank=True, validators=(validate_json_object,))
    # Not stored: undo_import_batch sets it on the batch it returns, the rows it moved to later batches.
    rehomed_count = 0
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
            visible_accounts = Account.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(account_id__in=visible_accounts)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    source__in=("huntington", "capital_one", "apple_card", "vanguard", "simplefin", "ofx", "manual")
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
        return f"{self.get_source_display()} import {self.pk}"  # pyright: ignore[reportAttributeAccessIssue] - Django adds get_FOO_display for choices fields at runtime


class AccountSortOrder(models.TextChoices):
    NAME_ASC = "name_asc", "Name A–Z"
    NAME_DESC = "name_desc", "Name Z–A"
    BALANCE_DESC = "balance_desc", "Balance high→low"
    BALANCE_ASC = "balance_asc", "Balance low→high"


# Blank means the page's own order.
_ACCOUNT_SORT_VALUES = ("", *AccountSortOrder.values)


class AccountSortSettings(models.Model):
    """How one member sorts the balance-by-account lists (#655); blank keeps the page's own order."""

    Order = AccountSortOrder

    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name="account_sort_settings")
    net_worth_sort = models.CharField(max_length=16, blank=True, default="", choices=AccountSortOrder.choices)
    accounts_sort = models.CharField(max_length=16, blank=True, default="", choices=AccountSortOrder.choices)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(net_worth_sort__in=_ACCOUNT_SORT_VALUES),
                name="account_sort_net_worth_valid",
            ),
            models.CheckConstraint(
                condition=Q(accounts_sort__in=_ACCOUNT_SORT_VALUES),
                name="account_sort_accounts_valid",
            ),
        ]

    def __str__(self):
        return f"Account sort settings {self.pk}"



# Public name for finance/models/__init__.py, which keeps the private name
# importable from finance.models (#759); see the people module's note.
ACCOUNT_SORT_VALUES = _ACCOUNT_SORT_VALUES
