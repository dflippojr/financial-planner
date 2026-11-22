from ..input_limits import AUTO_NOTE_MAX_CHARS
from ..money_display import format_minor
from fractions import Fraction

from django.db import models
from django.db.models import Q

from .base import sha256_validator, validate_reason_list
from .people import Membership, Person, person_for as _person_for
from .accounts import Account
from .ledger import Transaction


def monthly_from_annual_minor(annual_minor):
    """A monthly amount from an annual one, in minor units: annual / 12 rounded half-even (#507)."""
    # round() on a Fraction is exact and rounds half to even.
    return round(Fraction(annual_minor, 12))


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
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # A one-line note added to each new charge that joins a confirmed series (#684).
    # note_updated_at is when it was last set: only charges imported after it get the
    # line on their own, so setting or editing it never changes past notes.
    note = models.CharField(max_length=AUTO_NOTE_MAX_CHARS, blank=True, default="")
    note_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            visible_transactions = Transaction.objects.visible_to(person).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            hidden_members = RecurringSeriesMember.objects.filter(
                series_id=models.OuterRef("pk"),
            ).exclude(transaction_id__in=visible_transactions)
            return self.filter(person=person).exclude(models.Exists(hidden_members))

        def shared_with(self, principal, *, linkers=False):
            """Confirmed series another current household member owns, when every charge sits in a household account.

            Every member may edit shared accounts, so a bill built only from
            household charges is the household's to cancel or resume. A series
            with any private charge stays with its owner.
            """
            person = _person_for(principal)
            if person is None:
                return self.none()
            household_ids = Membership.objects.filter(person=person, ended_at__isnull=True).values("household_id")
            member_ids = Membership.objects.filter(
                household_id__in=household_ids, ended_at__isnull=True, person__isnull=False
            ).values("person_id")
            household_accounts = Account.objects.filter(
                scope=Account.Scope.HOUSEHOLD, household_id__in=household_ids
            ).values("pk")
            visible_transactions = Transaction.objects.visible_to(person).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            outside = RecurringSeriesMember.objects.filter(series_id=models.OuterRef("pk")).exclude(
                transaction__account_id__in=household_accounts, transaction_id__in=visible_transactions
            )
            # A stale `is_active` flag on another member's row is not trusted:
            # the bill must still have a charge on an active account.
            has_members = RecurringSeriesMember.objects.filter(
                series_id=models.OuterRef("pk"),
                transaction__status=Transaction.Status.ACTIVE,
                transaction__account__status=Account.Status.ACTIVE,
            )
            if linkers:
                # Inactive and archived-account copies still link a bill's
                # histories, as in the projection; they are never offered.
                has_members = RecurringSeriesMember.objects.filter(series_id=models.OuterRef("pk"))
            return (
                self.filter(person_id__in=member_ids, status=RecurringSeries.Status.CONFIRMED)
                .exclude(person=person)
                .filter(models.Exists(has_members))
                .exclude(models.Exists(outside))
            )

        def actionable_by(self, principal, *, linkers=False):
            """Series the viewer may cancel or resume: their own, plus the household's shared ones.

            With `linkers`, shared series with no live charge are included too,
            for finding which copies make up one bill.
            """
            return self.filter(
                Q(pk__in=self.visible_to(principal).values("pk"))
                | Q(pk__in=self.shared_with(principal, linkers=linkers).values("pk"))
            )

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
        return format_minor(self.typical_amount_minor, self.currency)

    @property
    def annual_minor(self):
        return abs(self.typical_amount_minor) * PERIODS_PER_YEAR[self.cadence]

    @property
    def monthly_minor(self):
        return monthly_from_annual_minor(self.annual_minor)

    @property
    def monthly_display(self):
        return format_minor(self.monthly_minor, self.currency)

    @property
    def annual_display(self):
        return format_minor(self.annual_minor, self.currency)


# Each cadence's step, defined once for detection, projection and totals (#597).
# A cadence steps by days or by calendar months, never both. PlannedItem.Cadence
# adds only ONE_TIME, which has no step.
WEEK_STEP_DAYS: dict[str, int] = {
    RecurringSeries.Cadence.WEEKLY: 7,
    RecurringSeries.Cadence.BIWEEKLY: 14,
}


MONTH_STEPS: dict[str, int] = {
    RecurringSeries.Cadence.MONTHLY: 1,
    RecurringSeries.Cadence.QUARTERLY: 3,
    RecurringSeries.Cadence.ANNUAL: 12,
}


PERIODS_PER_YEAR: dict[str, int] = {
    RecurringSeries.Cadence.WEEKLY: 52,
    RecurringSeries.Cadence.BIWEEKLY: 26,
    RecurringSeries.Cadence.MONTHLY: 12,
    RecurringSeries.Cadence.QUARTERLY: 4,
    RecurringSeries.Cadence.ANNUAL: 1,
}


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
        return f"Series {self.series_id} txn {self.transaction_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class RecurringPriceAcknowledgement(models.Model):
    """The latest charge amount one member dismissed as a series' price-change flag.

    Kept per member, so dismissing a household bill's flag leaves every other
    member's flag and alerts in place (#523). `transaction` is the charge that
    was flagged, when known; dismissals from before it was recorded have none.
    """

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="recurring_price_acknowledgements")
    series = models.ForeignKey(RecurringSeries, on_delete=models.CASCADE, related_name="price_acknowledgements")
    amount_minor = models.BigIntegerField()
    transaction = models.ForeignKey(Transaction, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "series"), name="recurring_price_ack_person_series"),
        ]

    def __str__(self):
        return f"Price acknowledgement person {self.person_id} series {self.series_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


class RecurringExclusion(models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="recurring_exclusions")
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="recurring_exclusions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "transaction"), name="recurring_exclusion_person_transaction"),
        ]

    def __str__(self):
        return f"Exclusion person {self.person_id} txn {self.transaction_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute

