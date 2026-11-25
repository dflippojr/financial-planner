
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from .base import actor_display_name


class Person(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="person",
    )
    display_name = models.CharField(max_length=150)
    use_shared_local_chat = models.BooleanField(default=False)
    use_shared_local_background = models.BooleanField(default=False)
    sessions_valid_after = models.DateTimeField(null=True, blank=True)
    privacy_policy_declined_version = models.ForeignKey(
        "PrivacyPolicyVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="declined_by",
    )
    privacy_policy_dismissed_version = models.ForeignKey(
        "PrivacyPolicyVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dismissed_by",
    )
    # SHA-256 of the investment-activity notice wording this member dismissed
    # (#699); a different wording shows the notice again. See notice_dismissal.
    investment_notice_dismissed_hash = models.CharField(max_length=64, blank=True, default="")
    require_passkey_after_password = models.BooleanField(default=False)
    # Fingerprint of the charges the last recurring detection ran on; a page
    # view detects again only when it changes. See recurring_services.
    recurring_inputs_signature = models.CharField(max_length=64, blank=True, default="")
    # Merchants the last detection left out because they have too many charges.
    recurring_skipped_merchants = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.display_name


class Household(models.Model):
    name = models.CharField(max_length=150)
    transfer_match_window_days = models.PositiveSmallIntegerField(default=5)
    # Savings-goal funding plan reserve in minor units; null means the default
    # of one month of average actual spending, 0 means no reserve.
    savings_buffer_minor = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(transfer_match_window_days__lte=366),
                name="household_transfer_window_days_range",
            ),
            models.CheckConstraint(
                condition=Q(savings_buffer_minor__isnull=True) | Q(savings_buffer_minor__gte=0),
                name="household_savings_buffer_not_negative",
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


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            return None
    return None


class _PrivateOrHouseholdQuerySet(models.QuerySet):
    """Rows a person owns privately, or shares with a household they belong to now.

    Only for models with owner, scope ("private" or "household") and household
    fields: Account, PlannedItem, SavingsGoal and Budget.
    """

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


class BreakGlassGrant(models.Model):
    """One-time sign-in link issued on the host by `manage.py break_glass_sign_in` (#792)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="break_glass_grants")
    token_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
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
        BREAK_GLASS_ISSUED = "break_glass_issued", "Break-glass sign-in link issued on the host"
        BREAK_GLASS_SIGN_IN = "break_glass_sign_in", "Signed in with a break-glass link"

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
                        "break_glass_issued",
                        "break_glass_sign_in",
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


class PendingSignInSession(models.Model):
    """Index of unauthenticated sessions holding a half-finished sign-in for a user.

    Lets revocation find a user's pending passkey sign-ins without decoding
    every row in django_session.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pending_sign_ins")
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Pending sign-in session {self.pk}"


class Passkey(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="passkeys")
    name = models.CharField(max_length=80)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("member", "-created_at"), name="passkey_member_created_idx"),
        ]

    def __str__(self):
        return self.name



# Public names for the other finance.models modules (#759). The import contract
# (tests/test_import_contract.py) records every `from .x import _name` across
# modules, so siblings import these and keep the private name locally.
person_for = _person_for
PrivateOrHouseholdQuerySet = _PrivateOrHouseholdQuerySet
