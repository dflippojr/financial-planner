"""Metadata-only audit storage. Write through audit_services, never from forms."""
import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


CHANGED_FIELDS = frozenset({
    "scope", "share_mode", "status", "name", "owner", "apr_percent", "minimum_payment", "payment_day",
    "secured_asset", "require_passkey", "session", "chat_backend", "background_backend", "chat_model",
    "background_model", "use_for_chat", "use_for_background", "use_shared_local_chat",
    "use_shared_local_background", "offer_local_to_household", "offer_local_chat", "offer_plan_links",
    "alert_toggles", "thresholds", "notification_email", "email_enabled", "threshold", "accounts",
    "account_links", "cutover",
})
# Audience kinds: "account" follows the target account's current access; "deletion"
# snapshots the owner/household; "personal" is the acting member only; "household"
# is the household's current members.
AUDIENCE_ACCOUNT, AUDIENCE_DELETION, AUDIENCE_PERSONAL, AUDIENCE_HOUSEHOLD = "account", "deletion", "personal", "household"
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
        ACCOUNT_CREATED = "account_created", "Account created"
        ACCOUNT_RENAMED = "account_renamed", "Account renamed"
        ACCOUNT_OWNER_CHANGED = "account_owner_changed", "Account ownership changed"
        DEBT_TERMS_CHANGED = "debt_terms_changed", "Debt terms changed"
        LOAN_PAIRING_CHANGED = "loan_pairing_changed", "Loan pairing changed"
        HOUSEHOLD_CREATED = "household_created", "Household created"
        INVITATION_CREATED = "invitation_created", "Invitation created"
        INVITATION_ACCEPTED = "invitation_accepted", "Invitation accepted"
        MEMBER_LEFT = "member_left", "Member left household"
        GOOGLE_CONNECTED = "google_connected", "Google sign-in connected"
        GOOGLE_DISCONNECTED = "google_disconnected", "Google sign-in disconnected"
        PASSWORD_ADDED = "password_added", "Password added"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        PASSWORD_REMOVED = "password_removed", "Password removed"
        PASSKEY_REQUIREMENT_CHANGED = "passkey_requirement_changed", "Passkey requirement changed"
        SESSION_REVOKED = "session_revoked", "Session revoked"
        OTHER_SESSIONS_REVOKED = "other_sessions_revoked", "Other sessions revoked"
        PRIVACY_ACCEPTED = "privacy_accepted", "Privacy policy accepted"
        PRIVACY_DECLINED = "privacy_declined", "Privacy policy declined"
        SIMPLEFIN_CONNECTED = "simplefin_connected", "SimpleFIN connected"
        SIMPLEFIN_DISCONNECTED = "simplefin_disconnected", "SimpleFIN disconnected"
        SIMPLEFIN_LINKS_CHANGED = "simplefin_links_changed", "SimpleFIN account links changed"
        AI_HARNESS_CONNECTED = "ai_harness_connected", "AI harness connected"
        AI_HARNESS_DISCONNECTED = "ai_harness_disconnected", "AI harness disconnected"
        AI_KEY_CONNECTED = "ai_key_connected", "AI key connected"
        AI_KEY_REPLACED = "ai_key_replaced", "AI key replaced"
        AI_KEY_DISCONNECTED = "ai_key_disconnected", "AI key disconnected"
        AI_DEFAULTS_CHANGED = "ai_defaults_changed", "AI defaults changed"
        AI_LOCAL_OFFER_CHANGED = "ai_local_offer_changed", "Local-model offer changed"
        AI_SHARED_LOCAL_CHANGED = "ai_shared_local_changed", "Shared local-model choice changed"
        PLAN_LINK_OFFER_CHANGED = "plan_link_offer_changed", "Plan-link offer changed"
        NOTIFICATION_PREFERENCES_CHANGED = "notification_preferences_changed", "Notification preferences changed"
        NOTIFICATION_ADDRESS_CHANGED = "notification_address_changed", "Notification address changed"
        BILLS_CALENDAR_CHANGED = "bills_calendar_changed", "Bill calendar settings changed"

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
        MEMBER = "member", "Member"
        HOUSEHOLD = "household", "Household"
        INVITATION = "invitation", "Invitation"
        SESSION = "session", "Session"
        CONNECTION = "connection", "Connection"
        SETTING = "setting", "Setting"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    action = models.CharField(max_length=40, choices=Action)
    outcome = models.CharField(max_length=12, choices=Outcome, default=Outcome.SUCCEEDED)
    actor_kind = models.CharField(max_length=12, choices=ActorKind)
    actor = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_actions")
    effective_member = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="effective_audit_actions")
    target_type = models.CharField(max_length=16, choices=TargetType, default=TargetType.ACCOUNT)
    affected_member = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="affected_audit_events")
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
        audience = ACTION_AUDIENCE.get(self.action)
        if audience == AUDIENCE_DELETION:
            if bool(self.private_owner_id) == bool(self.household_id):
                raise ValidationError("Deletion event requires one audience.")
        elif audience == AUDIENCE_PERSONAL:
            if self.private_owner_id is None or self.household_id is not None:
                raise ValidationError("Personal events require one member audience.")
        elif audience == AUDIENCE_HOUSEHOLD:
            if self.household_id is None or self.private_owner_id is not None:
                raise ValidationError("Household events require one household audience.")
        elif self.private_owner_id is not None or self.household_id is not None:
            raise ValidationError("Surviving events use the target's current audience.")
        if self.actor_kind == self.ActorKind.MEMBER and self.actor_id is None:
            raise ValidationError("Member actor is required for new events.")
        if self.actor_kind != self.ActorKind.MEMBER and self.actor_id is not None:
            raise ValidationError("System actors cannot claim a member identity.")
        if audience in (AUDIENCE_ACCOUNT, AUDIENCE_DELETION) and (self.account_id is None or self.account_id != self.target_id):
            raise ValidationError("Audit target must be a surviving account at append time.")
        if audience in (AUDIENCE_PERSONAL, AUDIENCE_HOUSEHOLD) and self.account_id is not None:
            raise ValidationError("Non-account events have no account.")

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


_A = AuditEvent.Action
_T = AuditEvent.TargetType
_ACCOUNT_SCOPE = ("scope", "share_mode")
# action -> (target type, audience, allowed changed field names). The single source
# of truth for the coverage matrix; append_event rejects anything outside it.
ACTION_SPECS = {
    _A.ACCOUNT_SHARED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, set(_ACCOUNT_SCOPE)),
    _A.ACCOUNT_UNSHARED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, set(_ACCOUNT_SCOPE)),
    _A.SHARE_MODE_CHANGED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, {"share_mode"}),
    _A.ACCOUNT_ARCHIVED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, {"status"}),
    _A.ACCOUNT_DELETED: (_T.ACCOUNT, AUDIENCE_DELETION, set()),
    _A.ACCOUNT_CREATED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, set(_ACCOUNT_SCOPE)),
    _A.ACCOUNT_RENAMED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, {"name"}),
    _A.ACCOUNT_OWNER_CHANGED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, {"owner"}),
    _A.DEBT_TERMS_CHANGED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, {"apr_percent", "minimum_payment", "payment_day"}),
    _A.LOAN_PAIRING_CHANGED: (_T.ACCOUNT, AUDIENCE_ACCOUNT, {"secured_asset"}),
    _A.HOUSEHOLD_CREATED: (_T.HOUSEHOLD, AUDIENCE_HOUSEHOLD, set()),
    _A.INVITATION_CREATED: (_T.INVITATION, AUDIENCE_HOUSEHOLD, set()),
    _A.INVITATION_ACCEPTED: (_T.INVITATION, AUDIENCE_HOUSEHOLD, set()),
    _A.MEMBER_LEFT: (_T.MEMBER, AUDIENCE_HOUSEHOLD, set()),
    _A.GOOGLE_CONNECTED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.GOOGLE_DISCONNECTED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.PASSWORD_ADDED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.PASSWORD_CHANGED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.PASSWORD_REMOVED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.PASSKEY_REQUIREMENT_CHANGED: (_T.SETTING, AUDIENCE_PERSONAL, {"require_passkey"}),
    _A.SESSION_REVOKED: (_T.SESSION, AUDIENCE_PERSONAL, set()),
    _A.OTHER_SESSIONS_REVOKED: (_T.MEMBER, AUDIENCE_PERSONAL, {"session"}),
    _A.PRIVACY_ACCEPTED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.PRIVACY_DECLINED: (_T.MEMBER, AUDIENCE_PERSONAL, set()),
    _A.SIMPLEFIN_CONNECTED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.SIMPLEFIN_DISCONNECTED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.SIMPLEFIN_LINKS_CHANGED: (_T.CONNECTION, AUDIENCE_PERSONAL, {"account_links", "cutover"}),
    _A.AI_HARNESS_CONNECTED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.AI_HARNESS_DISCONNECTED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.AI_KEY_CONNECTED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.AI_KEY_REPLACED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.AI_KEY_DISCONNECTED: (_T.CONNECTION, AUDIENCE_PERSONAL, set()),
    _A.AI_DEFAULTS_CHANGED: (_T.CONNECTION, AUDIENCE_PERSONAL, {
        "chat_backend", "background_backend", "chat_model", "background_model",
        "use_for_chat", "use_for_background", "use_shared_local_chat", "use_shared_local_background"}),
    _A.AI_LOCAL_OFFER_CHANGED: (_T.CONNECTION, AUDIENCE_HOUSEHOLD, {"offer_local_to_household", "offer_local_chat"}),
    _A.AI_SHARED_LOCAL_CHANGED: (_T.SETTING, AUDIENCE_PERSONAL, {"use_shared_local_chat", "use_shared_local_background"}),
    _A.PLAN_LINK_OFFER_CHANGED: (_T.CONNECTION, AUDIENCE_HOUSEHOLD, {"offer_plan_links"}),
    _A.NOTIFICATION_PREFERENCES_CHANGED: (_T.SETTING, AUDIENCE_PERSONAL, {"alert_toggles", "thresholds"}),
    _A.NOTIFICATION_ADDRESS_CHANGED: (_T.SETTING, AUDIENCE_PERSONAL, {"notification_email", "email_enabled"}),
    _A.BILLS_CALENDAR_CHANGED: (_T.SETTING, AUDIENCE_PERSONAL, {"threshold", "accounts"}),
}
ACTION_AUDIENCE = {action: spec[1] for action, spec in ACTION_SPECS.items()}
assert set(ACTION_SPECS) == set(AuditEvent.Action), "Every audit action needs a coverage-matrix entry."
