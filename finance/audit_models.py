"""Metadata-only audit storage. Write through audit_services, never from forms."""
import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


CHANGED_FIELDS = frozenset({
    "scope", "share_mode", "status",
    # Field names only, never values: the model/form field that changed.
    "name", "amount", "date", "note", "account", "category", "kind", "cadence", "period",
    "rollover", "enabled", "archived", "target", "contribution", "tags", "mapping", "default",
    "match", "priority", "color", "description", "owner", "currency", "interval", "price",
})
METADATA_INT_KEYS = frozenset({
    "batch_id", "new_count", "duplicate_count", "invalid_count", "row_count", "history_id",
    "bulk_edit_id", "rule_application_id", "surviving_id", "source_id", "proposal_id",
    "transaction_id", "account_id", "undo_id",
})
METADATA_ENUM_KEYS = {
    "format": frozenset({"huntington", "capital_one", "apple_card", "vanguard", "simplefin", "ofx", "manual"}),
    "section": frozenset({"cash-flow", "spending", "income", "accounts", "tags", "recurring", "net-worth", "all"}),
    "export_kind": frozenset({"data_zip", "year_end_csv", "receipt"}),
}
METADATA_UUID_KEYS = frozenset({"operation_id"})
MAX_METADATA_INT = 10**15
APPEND_ONLY_ERROR = "Audit events are append-only."


def validate_metadata(value):
    if not isinstance(value, dict):
        raise ValidationError("Unsupported audit metadata.")
    for key, item in value.items():
        if key in METADATA_INT_KEYS:
            ok = isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= MAX_METADATA_INT
        elif key in METADATA_ENUM_KEYS:
            ok = isinstance(item, str) and item in METADATA_ENUM_KEYS[key]
        elif key in METADATA_UUID_KEYS:
            try:
                ok = isinstance(item, str) and str(uuid.UUID(item)) == item
            except ValueError:
                ok = False
        else:
            ok = False
        if not ok:
            raise ValidationError("Unsupported audit metadata.")


def validate_changed_fields(value):
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in CHANGED_FIELDS for item in value):
        raise ValidationError("Unsupported audit field names.")
    if len(value) != len(set(value)):
        raise ValidationError("Duplicate audit field names.")


class AuditQuerySet(models.QuerySet):
    def visible_to(self, principal):
        from .models import Account, Membership, _person_for

        person = _person_for(principal)
        if person is None:
            return self.none()
        households = Membership.objects.filter(person=person, ended_at__isnull=True).values("household_id")
        return self.filter(
            Q(account_id__in=Account.objects.visible_to(person).values("pk"))
            | (Q(account__isnull=True) & (Q(private_owner=person) | Q(household_id__in=households)))
        )

    def update(self, **kwargs):
        raise ValidationError(APPEND_ONLY_ERROR)

    def delete(self):
        raise ValidationError(APPEND_ONLY_ERROR)

    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Use the audit append service.")


class AuditEvent(models.Model):
    class Action(models.TextChoices):
        ACCOUNT_SHARED = "account_shared", "Account shared"
        ACCOUNT_UNSHARED = "account_unshared", "Account unshared"
        SHARE_MODE_CHANGED = "share_mode_changed", "Sharing mode changed"
        ACCOUNT_ARCHIVED = "account_archived", "Account archived"
        ACCOUNT_DELETED = "account_deleted", "Account deleted"
        RECORD_CREATED = "record_created", "Created"
        RECORD_EDITED = "record_edited", "Edited"
        RECORD_DELETED = "record_deleted", "Deleted"
        RECORD_ARCHIVED = "record_archived", "Archived"
        RECORD_RESTORED = "record_restored", "Restored"
        RECORD_ENABLED = "record_enabled", "Enabled"
        RECORD_DISABLED = "record_disabled", "Disabled"
        RECORD_COMPLETED = "record_completed", "Completed"
        DEFAULT_CHANGED = "default_changed", "Default changed"
        ROLLOVER_TOGGLED = "rollover_toggled", "Rollover toggled"
        TAGS_CHANGED = "tags_changed", "Tags changed"
        IMPORT_COMMITTED = "import_committed", "Import committed"
        IMPORT_NO_NEW_ROWS = "import_no_new_rows", "Import completed, no new rows"
        IMPORT_UNDONE = "import_undone", "Import undone"
        TRANSACTION_CORRECTED = "transaction_corrected", "Transaction corrected"
        BULK_APPLIED = "bulk_applied", "Bulk edit applied"
        BULK_UNDONE = "bulk_undone", "Bulk edit undone"
        RULE_APPLIED = "rule_applied", "Rule applied"
        RULE_REVERSED = "rule_reversed", "Rule application reversed"
        RECURRING_CONFIRMED = "recurring_confirmed", "Recurring confirmed"
        RECURRING_DISMISSED = "recurring_dismissed", "Recurring dismissed"
        RECURRING_MERGED = "recurring_merged", "Recurring merged"
        RECURRING_ADDED = "recurring_added", "Recurring charge added"
        RECURRING_REMOVED = "recurring_removed", "Recurring charge removed"
        RECURRING_CANCELED = "recurring_canceled", "Recurring canceled"
        RECURRING_RESUMED = "recurring_resumed", "Recurring resumed"
        RECURRING_PRICE_ACKNOWLEDGED = "recurring_price_ack", "Recurring price change acknowledged"
        RECEIPT_ATTACHED = "receipt_attached", "Receipt attached"
        RECEIPT_REMOVED = "receipt_removed", "Receipt removed"
        DOWNLOAD_PREPARED = "download_prepared", "Download response prepared"

    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        STARTED = "started", "Started"
        FAILED = "failed", "Failed"

    class ActorKind(models.TextChoices):
        MEMBER = "member", "Member"
        SCHEDULER = "scheduler", "Scheduler"
        OPERATOR = "operator", "Operator"

    class Source(models.TextChoices):
        UI = "ui", "UI"
        JOB = "job", "Job"
        CLI = "cli", "CLI"
        CHAT = "chat_confirmation", "Chat confirmation"
        BULK = "bulk", "Bulk edit"
        RULE = "rule", "Automatic rule"

    class TargetType(models.TextChoices):
        ACCOUNT = "account", "Account"
        IMPORT_BATCH = "import_batch", "Import batch"
        TRANSACTION = "transaction", "Transaction"
        BALANCE = "balance", "Balance or valuation"
        BUDGET = "budget", "Budget"
        PLANNED_ITEM = "planned_item", "Planned item"
        GOAL = "goal", "Savings goal"
        CATEGORY = "category", "Category"
        TAG = "tag", "Tag"
        CSV_MAPPING = "csv_mapping", "Saved CSV mapping"
        RULE = "rule", "Category rule"
        RECURRING = "recurring", "Recurring series"
        RECEIPT = "receipt", "Receipt"
        EXPORT = "export", "Data download"
        BULK_EDIT = "bulk_edit", "Bulk edit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    action = models.CharField(max_length=32, choices=Action)
    outcome = models.CharField(max_length=12, choices=Outcome, default=Outcome.SUCCEEDED)
    actor_kind = models.CharField(max_length=12, choices=ActorKind)
    actor = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_actions")
    effective_member = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="effective_audit_actions")
    target_type = models.CharField(max_length=16, choices=TargetType, default=TargetType.ACCOUNT)
    target_id = models.PositiveBigIntegerField()
    account = models.ForeignKey("Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    private_owner = models.ForeignKey("Person", null=True, blank=True, on_delete=models.CASCADE, related_name="private_audit_events")
    household = models.ForeignKey("Household", null=True, blank=True, on_delete=models.CASCADE, related_name="audit_events")
    source = models.CharField(max_length=20, choices=Source)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    changed_fields = models.JSONField(default=list, validators=[validate_changed_fields], blank=True)
    metadata = models.JSONField(default=dict, validators=[validate_metadata], blank=True)
    checksum = models.CharField(max_length=64, editable=False)

    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ("-occurred_at", "-id")
        indexes = [models.Index(fields=("occurred_at", "id"), name="audit_time_id_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(target_id__gt=0), name="audit_positive_target"),
            models.CheckConstraint(
                condition=(Q(account__isnull=False, private_owner__isnull=True, household__isnull=True)
                           | Q(private_owner__isnull=False, household__isnull=True)
                           | Q(private_owner__isnull=True, household__isnull=False)),
                name="audit_target_or_deletion_audience",
            ),
        ]

    def clean(self):
        super().clean()
        if self.action == self.Action.ACCOUNT_DELETED:
            if bool(self.private_owner_id) == bool(self.household_id):
                raise ValidationError("Deletion event requires one audience.")
        elif (self.account_id is not None) + (self.private_owner_id is not None) + (self.household_id is not None) != 1:
            raise ValidationError("Events use exactly one current audience.")
        if self.actor_kind == self.ActorKind.MEMBER and self.actor_id is None:
            raise ValidationError("Member actor is required for new events.")
        if self.actor_kind != self.ActorKind.MEMBER and self.actor_id is not None:
            raise ValidationError("System actors cannot claim a member identity.")
        if self.target_type == self.TargetType.ACCOUNT and (self.account_id is None or self.account_id != self.target_id):
            raise ValidationError("Audit target must be a surviving account at append time.")

    def calculated_checksum(self):
        # Mutable visibility/actor references are intentionally excluded: deletion
        # anonymizes them. The checksum is an accident detector, not a signature.
        payload = [str(self.pk), self.occurred_at.isoformat(), self.action, self.outcome,
                   self.actor_kind, self.target_type, self.target_id, self.source,
                   str(self.correlation_id), self.changed_fields]
        if self.metadata:
            payload.append(self.metadata)
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(APPEND_ONLY_ERROR)
        try:
            self.full_clean(exclude=("checksum",))
        except ValidationError:
            raise ValidationError("Invalid audit metadata.") from None
        self.checksum = self.calculated_checksum()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(APPEND_ONLY_ERROR)
