
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .people import Person, person_for as _person_for
from .accounts import Account, ImportBatch


class SimpleFinConnection(models.Model):
    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="simplefin_connections")
    encrypted_access_url = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_result = models.CharField(max_length=500, blank=True)
    disabled = models.BooleanField(default=False)
    # What the newest /accounts read saw (#694), so Settings -> Connections
    # renders without a live request: {"at", "accounts", "banks", "messages"}.
    # Bank names and sanitized provider messages only; never the access URL.
    last_read = models.JSONField(default=dict, blank=True)
    # ISO times of /accounts requests in the last 24 hours: the shared budget.
    request_log = models.JSONField(default=list, blank=True)
    # conn_id -> the health state ("ok", "stale", "error") last pushed over ntfy.
    health_alerts = models.JSONField(default=dict, blank=True)
    # The open Fix or Add pop-up flow (finance.simplefin_flow), or {}.
    flow = models.JSONField(default=dict, blank=True)

    class QuerySet(models.QuerySet):
        def owned_by(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(owner=person)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"SimpleFIN connection {self.pk}"


class SimpleFinClaim(models.Model):
    """A setup token claim in flight, so a double-submitted Connect never sends it twice.

    Holds only a hash of the claim URL, and only while the claim runs.
    """

    owner = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="+")
    claim_url_sha256 = models.CharField(max_length=64)
    started_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("owner", "claim_url_sha256"), name="simplefin_claim_owner_token"),
        ]

    def __str__(self):
        return f"SimpleFIN claim {self.pk}"


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
        # Derived from transactions, working back from the earliest SimpleFIN or
        # manual balance (#636). Stored in the SimpleFIN protocol sign: an amount
        # owed on a credit card is negative. Ranks below both other sources.
        CALCULATED = "calculated", "Calculated"

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
    # A balance converted from another currency keeps what the provider sent
    # and the ECB rate used (#441); all three are empty for a USD balance.
    original_amount = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    original_currency = models.CharField(max_length=3, blank=True, default="")
    fx_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    fx_rate_date = models.DateField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            visible_accounts = Account.objects.visible_to(principal).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
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
                condition=(
                    Q(original_amount__isnull=True, original_currency="", fx_rate__isnull=True, fx_rate_date__isnull=True)
                    | (
                        Q(original_amount__isnull=False, fx_rate__isnull=False, fx_rate_date__isnull=False)
                        & ~Q(original_currency="")
                    )
                ),
                name="balance_snapshot_original_complete",
            ),
            models.CheckConstraint(
                condition=Q(source__in=("simplefin", "manual", "calculated")),
                name="balance_snapshot_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(net_contribution_minor__isnull=True) | Q(source="manual"),
                name="balance_snapshot_contribution_requires_manual",
            ),
        ]

    def __str__(self):
        return f"Balance {self.snapshot_date} account {self.account_id}"  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute

