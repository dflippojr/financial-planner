import uuid
from ..input_limits import MAX_DESCRIPTION_CHARS
from ..money_display import format_minor

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from .base import (
    actor_display_name,
    ArchivableModel,
    receipt_stored_name_validator,
    sha256_validator,
    validate_bulk_edit_snapshot,
    validate_json_object,
    validate_reason_list,
    validate_saved_filter_query,
)
from .people import Person, person_for as _person_for
from .categories import Category, Tag
from .accounts import Account, ImportBatch


class Transaction(ArchivableModel):
    class Kind(models.TextChoices):
        CASH_FLOW = "cash_flow", "Cash flow"
        INVESTMENT_ACTIVITY = "investment_activity", "Investment activity (neutral)"

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="transactions")
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="transactions")
    transaction_date = models.DateField()
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    description = models.TextField(max_length=MAX_DESCRIPTION_CHARS)
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
    # A transfer to or from an account the planner doesn't track (#642). Set on
    # the row, so it holds for every viewer; left out of income and spending
    # through `exclusion_exists_for`, like a marked transfer pair.
    untracked_transfer = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    UNTRACKED_TRANSFER_LABEL = "Transfer (untracked account)"

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_accounts = Account.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(account_id__in=visible_accounts)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("account", "transaction_date"), name="txn_account_date_idx"),
            models.Index(fields=("amount_minor", "transaction_date"),
                         condition=Q(status="active"), name="txn_transfer_candidate_idx"),
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
            # Investment activity is already neutral, so it is never marked (#642).
            models.CheckConstraint(
                condition=Q(untracked_transfer=False) | ~Q(kind="investment_activity"),
                name="transaction_untracked_not_investment",
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
        if self.account_id and self.import_batch_id and self.import_batch.account_id != self.account_id:  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
            raise ValidationError({"import_batch": "Import batch must belong to the transaction account."})

    def __str__(self):
        return f"{self.transaction_date}: {self.amount_minor} {self.currency}"

    @property
    def is_manual_entry(self):
        return self.import_batch.source == ImportBatch.Source.MANUAL

    @property
    def amount_display(self):
        return format_minor(self.amount_minor, self.currency)

    @property
    def category_display(self):
        if self.untracked_transfer:
            return self.UNTRACKED_TRANSFER_LABEL
        if self.is_excluded_transfer:
            return "Transfer"
        if self.category_source == self.CategorySource.SPLIT:
            count = len(self.splits.all())  # pyright: ignore[reportAttributeAccessIssue] - TransactionSplit.transaction reverse accessor, not visible to pyright
            return f"Split ({count})"
        # A view may prefetch only categories the viewer can still see.
        if self.category_id is None or self.category is None:  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
            return "Uncategorized"
        return self.category.name

    @property
    def is_excluded_transfer(self):
        # A pair counts only for a viewer who can see both legs, so the state must
        # come from a viewer-scoped `_excluded=exclusion_exists_for(person)`
        # annotation; there is deliberately no unscoped fallback.
        annotated = getattr(self, "_excluded", None)
        if annotated is None:
            raise ImproperlyConfigured(
                "Transaction transfer state needs a viewer-scoped _excluded annotation."
            )
        return bool(annotated)


class Receipt(models.Model):
    class ContentType(models.TextChoices):
        JPEG = "image/jpeg", "JPEG"
        PNG = "image/png", "PNG"
        WEBP = "image/webp", "WebP"
        HEIC = "image/heic", "HEIC"
        PDF = "application/pdf", "PDF"

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="receipts")
    original_name = models.CharField(max_length=255)
    stored_name = models.CharField(max_length=32, unique=True, validators=(receipt_stored_name_validator,))
    content_type = models.CharField(max_length=16, choices=ContentType)
    size_bytes = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_receipts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(transaction_id__in=visible_transactions)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(content_type__in=("image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf")),
                name="receipt_content_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(size_bytes__gt=0, size_bytes__lte=10 * 1024 * 1024),
                name="receipt_size_within_cap",
            ),
        ]

    def __str__(self):
        return f"Receipt {self.pk}"


class TransactionTag(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="transaction_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("transaction", "tag"), name="transaction_tag_unique"),
        ]

    def __str__(self):
        return f"{self.transaction_id}:{self.tag_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class SavedTransactionFilter(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="saved_transaction_filters")
    name = models.CharField(max_length=80)
    query = models.JSONField(validators=(validate_saved_filter_query,))
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
            models.UniqueConstraint(
                Lower("name"),
                F("member"),
                name="saved_txn_filter_unique_name_per_member",
            ),
        ]

    def __str__(self):
        return self.name


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
        DELETED = "deleted", "Deleted"
        UNTRACKED = "untracked", "Untracked transfer"

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
    # Listed only to this member, such as a refund link dropped for them alone (#570).
    # Deleting the member deletes the rows no one else may see.
    only_for = models.ForeignKey(Person, on_delete=models.CASCADE, null=True, blank=True, related_name="+")

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            # An exclusion entry describes a pair, so it is listed only when the
            # viewer can see every pair the transaction belongs to.
            pair_with_hidden_leg = TransferPair.objects.filter(
                Q(leg_a_id=models.OuterRef("transaction_id")) | Q(leg_b_id=models.OuterRef("transaction_id"))
            ).exclude(leg_a_id__in=visible_transactions, leg_b_id__in=visible_transactions)
            return self.filter(
                Q(only_for__isnull=True) | Q(only_for=_person_for(principal)),
                transaction_id__in=visible_transactions,
            ).exclude(Q(field_name=self.model.Field.EXCLUSION) & models.Exists(pair_with_hidden_leg))

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
                        field_name__in=(
                            "description", "category", "exclusion", "refund_link", "note", "tags", "deleted",
                            "untracked",
                        ),
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
        return format_minor(minor, self.currency)

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
            self.Field.DELETED,
            self.Field.UNTRACKED,
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
            self.Field.DELETED,
            self.Field.UNTRACKED,
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
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
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
        return f"Transfer pair {self.leg_a_id}/{self.leg_b_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute

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
        return format_minor(self.amount_minor, self.transaction.currency)


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
            visible_transactions = Transaction.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(refund_id__in=visible_transactions, original_id__in=visible_transactions).exclude(
                pk__in=RefundLinkDrop.objects.filter(person=_person_for(principal)).values("link_id")
            )

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(refund=F("original")), name="refund_link_distinct_transactions"),
        ]

    def __str__(self):
        return f"Refund {self.refund_id} for {self.original_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class RefundLinkDrop(models.Model):
    """A refund link that stopped counting for one member only (#570).

    The original became a leg of `pair`, a marked transfer pair that this member
    sees both legs of, while another viewer of the refund does not, so the pair
    is excluded only for this member (`exclusion_exists_for`). Everyone else
    keeps the link. The member's "linked" -> "unlinked" history row is `only_for`
    them, so it stays hidden from others even after the link itself is deleted.
    Unmarking the pair deletes its drops, so the link counts again for the member
    once no other marked pair holds a drop on it. Unsharing an account or leaving
    the household leaves drops in place (#579).
    """

    link = models.ForeignKey(RefundLink, on_delete=models.CASCADE, related_name="drops")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="refund_link_drops")
    pair = models.ForeignKey(TransferPair, on_delete=models.CASCADE, related_name="refund_link_drops")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("link", "person", "pair"), name="refund_link_drop_unique_person_pair"),
        ]

    def __str__(self):
        return f"Refund link drop {self.pk}"


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

