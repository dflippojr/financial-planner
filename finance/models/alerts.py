
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from .people import Person
from .accounts import Account


NTFY_TOPIC_PATTERN = r"\A[A-Za-z0-9_-]{1,64}\Z"


NTFY_TOPIC_VALIDATOR = RegexValidator(NTFY_TOPIC_PATTERN, "Use 1-64 letters, digits, hyphens or underscores.")


class Alert(models.Model):
    class Kind(models.TextChoices):
        SYNC = "sync", "Sync"
        RECURRING_PRICE = "recurring_price", "Recurring price change"
        RECURRING_MISSED = "recurring_missed", "Missed recurring charge"
        BUDGET = "budget", "Budget"
        LARGE_TRANSACTION = "large_transaction", "Large transaction"
        MONTHLY_REVIEW = "monthly_review", "Monthly review"
        UNUSUAL_SPENDING = "unusual_spending", "Unusual spending"
        BACKUP = "backup", "Backup"
        EXPECTED_BALANCE = "expected_balance", "Expected balance"
        IMPORT_DUE = "import_due", "Import due"
        RECONCILIATION = "reconciliation", "Balance reconciliation"
        FUND = "fund", "Sinking funds"

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
    # Every account whose data the title quotes. The recipient sees the alert
    # only while they can see all of them.
    source_accounts = models.ManyToManyField(Account, blank=True, related_name="sourced_alerts")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    email_notice_sent_at = models.DateTimeField(null=True, blank=True)
    ntfy_notice_sent_at = models.DateTimeField(null=True, blank=True)

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
                        "unusual_spending",
                        "backup",
                        "expected_balance",
                        "import_due",
                        "reconciliation",
                        "fund",
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
    email_enabled = models.BooleanField(default=False)
    notification_email = models.EmailField(blank=True, default="")
    ntfy_enabled = models.BooleanField(default=False)
    ntfy_topic = models.CharField(max_length=64, blank=True, default="", validators=[NTFY_TOPIC_VALIDATOR])
    # Monday of the last weekly digest pushed over ntfy, so each week is sent once.
    ntfy_digest_week = models.DateField(null=True, blank=True)
    # "account:start:end" of each balance reconciliation mismatch already alerted
    # (operators only), so dedupe outlives the alert row's retention purge.
    reconciliation_alerted_windows = models.JSONField(default=list, blank=True)
    person = models.OneToOneField(Person, on_delete=models.PROTECT, related_name="alert_settings")
    sync_enabled = models.BooleanField(default=True)
    recurring_price_enabled = models.BooleanField(default=True)
    recurring_price_percent = models.PositiveIntegerField(default=10)
    recurring_missed_enabled = models.BooleanField(default=True)
    budget_enabled = models.BooleanField(default=True)
    large_transaction_enabled = models.BooleanField(default=True)
    monthly_review_enabled = models.BooleanField(default=True)
    monthly_review_ai_enabled = models.BooleanField(default=True)
    expected_balance_enabled = models.BooleanField(default=False)
    import_due_enabled = models.BooleanField(default=True)
    fund_enabled = models.BooleanField(default=True)
    unusual_spending_enabled = models.BooleanField(default=True)
    unusual_spending_ai_enabled = models.BooleanField(default=True)
    unusual_category_percent = models.PositiveIntegerField(default=50)
    unusual_category_floor_minor = models.BigIntegerField(default=5_000)
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
            models.CheckConstraint(
                condition=Q(unusual_category_percent__gte=1, unusual_category_percent__lte=1000),
                name="alert_settings_unusual_percent_range",
            ),
            models.CheckConstraint(
                condition=Q(unusual_category_floor_minor__gte=0),
                name="alert_settings_unusual_floor_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(recurring_price_percent__gte=1, recurring_price_percent__lte=100),
                name="alert_settings_recurring_price_percent_range",
            ),

        ]

    def __str__(self):
        return f"Alert settings for {self.person_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute

