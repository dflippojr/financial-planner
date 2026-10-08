"""Metadata-only audit storage. Write through audit_services, never from forms."""
import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


CHANGED_FIELDS = frozenset({"scope", "share_mode", "status"})
APPEND_ONLY_ERROR = "Audit events are append-only."


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
            | (Q(account__isnull=True, action="account_deleted")
               & (Q(private_owner=person) | Q(household_id__in=households)))
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

    class TargetType(models.TextChoices):
        ACCOUNT = "account", "Account"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    action = models.CharField(max_length=24, choices=Action)
    outcome = models.CharField(max_length=12, choices=Outcome, default=Outcome.SUCCEEDED)
    actor_kind = models.CharField(max_length=12, choices=ActorKind)
    actor = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_actions")
    effective_member = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="effective_audit_actions")
    target_type = models.CharField(max_length=12, choices=TargetType, default=TargetType.ACCOUNT)
    target_id = models.PositiveBigIntegerField()
    account = models.ForeignKey("Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    private_owner = models.ForeignKey("Person", null=True, blank=True, on_delete=models.CASCADE, related_name="private_audit_events")
    household = models.ForeignKey("Household", null=True, blank=True, on_delete=models.CASCADE, related_name="audit_events")
    source = models.CharField(max_length=20, choices=Source)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    changed_fields = models.JSONField(default=list, validators=[validate_changed_fields], blank=True)
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
        elif self.private_owner_id is not None or self.household_id is not None:
            raise ValidationError("Surviving events use the target's current audience.")
        if self.actor_kind == self.ActorKind.MEMBER and self.actor_id is None:
            raise ValidationError("Member actor is required for new events.")
        if self.actor_kind != self.ActorKind.MEMBER and self.actor_id is not None:
            raise ValidationError("System actors cannot claim a member identity.")
        if self.account_id is None or self.account_id != self.target_id:
            raise ValidationError("Audit target must be a surviving account at append time.")

    def calculated_checksum(self):
        # Mutable visibility/actor references are intentionally excluded: deletion
        # anonymizes them. The checksum is an accident detector, not a signature.
        payload = [str(self.pk), self.occurred_at.isoformat(), self.action, self.outcome,
                   self.actor_kind, self.target_type, self.target_id, self.source,
                   str(self.correlation_id), self.changed_fields]
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
