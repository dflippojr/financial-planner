import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from ..ai_types import UNKNOWN_BACKEND_LABEL, backend_display_name
from .people import Person, person_for as _person_for
from .categories import Category
from .ledger import Transaction


class AiProviderConnection(models.Model):
    class Kind(models.TextChoices):
        AGENT_HARNESS = "agent_harness", "Agent Harness"
        ANTHROPIC_API = "anthropic_api", "Anthropic API"
        OPENAI_API = "openai_api", "OpenAI API"

    owner = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_connections")
    kind = models.CharField(max_length=32, choices=Kind, default=Kind.AGENT_HARNESS)
    # Empty for the API-key kinds, whose encrypted_token is the member's API key.
    base_url = models.CharField(max_length=255, blank=True, default="")
    encrypted_token = models.BinaryField()
    # API-key kinds only: this connection answers chat or background jobs.
    use_for_chat = models.BooleanField(default=False)
    use_for_background = models.BooleanField(default=False)
    harness_project = models.CharField(max_length=80, blank=True, default="")
    chat_backend = models.CharField(max_length=32, blank=True, default="")
    background_backend = models.CharField(max_length=32, blank=True, default="")
    chat_model = models.CharField(max_length=80, blank=True, default="")
    background_model = models.CharField(max_length=80, blank=True, default="")
    offer_local_to_household = models.BooleanField(default=False)
    # The host offers the tower's local model for their own chat. Off until the owner has verified it.
    offer_local_chat = models.BooleanField(default=False)
    # Household members may link their own Claude or Codex plan through this connection.
    offer_plan_links = models.BooleanField(default=False)
    connected_at = models.DateTimeField(default=timezone.now)
    last_status = models.CharField(max_length=80, blank=True, default="")

    class QuerySet(models.QuerySet):
        def owned_by(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(owner=person)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("owner", "kind"), name="ai_connection_unique_owner_kind"),
            models.CheckConstraint(
                condition=Q(kind__in=("agent_harness", "anthropic_api", "openai_api")),
                name="ai_connection_kind_valid",
            ),
        ]

    def __str__(self):
        return f"AI connection {self.pk}"


class AiPlanLink(models.Model):
    """A member's own Claude or Codex plan, linked through an Agent Harness connection.

    The login itself lives in the harness; this row only records that the member
    linked one and which connection it was linked through.
    """

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_plan_links")
    backend = models.CharField(max_length=16)
    connection = models.ForeignKey(AiProviderConnection, on_delete=models.CASCADE, related_name="plan_links")
    # The harness refused a request because the login lapsed; show the Link prompt.
    needs_login = models.BooleanField(default=False)
    linked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("person", "backend"), name="ai_plan_link_unique_person_backend"),
            models.CheckConstraint(condition=Q(backend__in=("claude", "codex")), name="ai_plan_link_backend_valid"),
        ]

    def __str__(self):
        return f"AI plan link {self.pk}"


class AiJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        WAITING_MODEL = "waiting_model", "Waiting for model"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_jobs")
    feature = models.CharField(max_length=40)
    backend = models.CharField(max_length=32, blank=True, default="")
    input_refs = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    audit_run_id = models.UUIDField(default=uuid.uuid4, editable=False)
    harness_session_id = models.CharField(max_length=120, blank=True, default="")
    next_attempt_at = models.DateTimeField(default=timezone.now)
    result_ref = models.CharField(max_length=120, blank=True, default="")
    failure_code = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=("status", "next_attempt_at"), name="ai_job_due_idx"),
            models.Index(fields=("status", "updated_at"), name="ai_job_stale_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=("queued", "waiting_model", "running", "succeeded", "failed")
                ),
                name="ai_job_status_valid",
            ),
        ]

    def __str__(self):
        return f"AI job {self.pk}"


class AiUsageEvent(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_usage_events")
    provider = models.CharField(max_length=32)
    backend = models.CharField(max_length=32)
    feature = models.CharField(max_length=40)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=40)
    # True when this event polled a session started earlier, not a new request.
    resumed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"AI usage {self.pk}"


class AiConversation(models.Model):
    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_conversations")
    harness_session_id = models.CharField(max_length=120, blank=True, default="")
    harness_connection = models.CharField(max_length=120, blank=True, default="")
    backend = models.CharField(max_length=32, blank=True, default="")
    title = models.CharField(max_length=120, blank=True, default="")
    used_account_ids = models.JSONField(default=list)
    used_planned_item_ids = models.JSONField(default=list)
    used_budget_ids = models.JSONField(default=list)
    used_goal_ids = models.JSONField(default=list)
    used_rule_ids = models.JSONField(default=list)
    # Providers (chat backend and connection, chat_attachments.provider_key) the chat has warned
    # will receive this conversation's files (#644, owner decision 5): the provider named at
    # upload, and each provider whose switch warning the member saw before asking again. Only
    # these may read the files or be resent earlier turns. Kept after a file is removed.
    attachment_providers = models.JSONField(default=list)
    # Whether the saved harness session was started with read_attachment among its tools.
    # A session started before #644 cannot call it, so a turn with files starts a new one.
    harness_session_reads_attachments = models.BooleanField(default=False)
    turn_count = models.PositiveIntegerField(default=0)
    tool_call_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person, expires_at__gt=timezone.now())

        def owned_by(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(member=person)

    objects = QuerySet.as_manager()

    def __str__(self):
        return f"AI conversation {self.pk}"


class AiConversationMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        ERROR = "error", "Error"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    conversation = models.ForeignKey(AiConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role)
    content = models.TextField()
    backend = models.CharField(max_length=32, blank=True, default="")
    figures = models.JSONField(default=list)
    # Charts the make_chart tool computed for this reply (#646): the validated spec
    # and the server's series. Never pinned elsewhere; deleted with the conversation.
    charts = models.JSONField(default=list)
    notices = models.JSONField(default=list)
    page_context = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    # A pending assistant turn is answered by the chat runner, never by a web request.
    status = models.CharField(max_length=8, choices=Status, default=Status.DONE)
    reply_to = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    claim_token = models.CharField(max_length=64, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    # Set at claim time: past it the turn is failed even while its runner still sends heartbeats.
    deadline_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(conversation__member=person)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=("user", "assistant", "error")),
                name="ai_conversation_message_role_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "done", "failed")),
                name="ai_conversation_message_status_valid",
            ),
        ]
        indexes = [models.Index(fields=("status", "claimed_at"), name="ai_chat_turn_status_idx")]

    def __str__(self):
        return f"AI message {self.pk}"


class AiChatAttachment(models.Model):
    """A file a member attached to one conversation (#644), parsed to text at upload.

    It exists only in that conversation: it cascades with the conversation, so
    every way a conversation is deleted (the member's delete, expiry, losing
    sight of a row it used, account or member deletion) deletes the file bytes
    and their parsed text with it. Both live only in this row, never on disk.
    """

    class Kind(models.TextChoices):
        CSV = "csv", "CSV"
        XLSX = "xlsx", "XLSX"
        PDF = "pdf", "PDF"

    conversation = models.ForeignKey(AiConversation, on_delete=models.CASCADE, related_name="attachments")
    # The question the file was sent with, for showing it in the thread.
    message = models.ForeignKey(
        AiConversationMessage, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments"
    )
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=8, choices=Kind)
    size_bytes = models.PositiveIntegerField()
    # Provenance: the provider named in the upload warning (chat backend and connection,
    # chat_attachments.provider_key). Who may read the file is decided by the conversation's
    # attachment_providers, which a warned provider switch extends (owner decision 5).
    provider = models.CharField(max_length=120)
    data = models.BinaryField()
    # {"sheets": [{"name", "rows": [[row number, [cells]]]}]} or {"pages": [text]}.
    content = models.JSONField(default=dict)
    # {"sheets": [{"name", "rows", "columns"}]} or {"pages", "truncated"}: listed without loading content.
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            # Access follows the conversation: whoever may open it, and nobody else.
            return self.filter(conversation__in=AiConversation.QuerySet(AiConversation).visible_to(principal))

    objects = QuerySet.as_manager()

    class Meta:
        ordering = ("pk",)
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=("csv", "xlsx", "pdf")), name="ai_chat_attachment_kind_valid"),
            models.CheckConstraint(
                condition=Q(size_bytes__gt=0, size_bytes__lte=10 * 1024 * 1024),
                name="ai_chat_attachment_size_within_cap",
            ),
        ]

    def __str__(self):
        return f"Chat attachment {self.pk}"


class AiProposal(models.Model):
    """A change the chat model suggested. Nothing is written until the member presses Apply."""

    class Kind(models.TextChoices):
        SET_CATEGORY = "set_category", "Set category"
        CREATE_RULE = "create_rule", "Create rule"
        SET_BUDGET = "set_budget", "Set budget"
        ADD_TAGS = "add_tags", "Add tags"
        # Item cards (#659): one card, a checkbox per item, a result per item.
        PLANNED_ITEMS = "planned_items", "Planned items"
        SAVINGS_GOALS = "savings_goals", "Savings goals"
        SET_BUDGETS = "set_budgets", "Set budgets"
        # Cleanup cards (#661), the same checkbox card.
        RECURRING = "recurring", "Recurring charges"
        TRANSFERS = "transfers", "Transfers"
        AI_SUGGESTIONS = "ai_suggestions", "AI category suggestions"
        NOTES = "notes", "Notes"
        SPLITS = "splits", "Splits"
        # Balance cards (#660), the same checkbox card.
        BALANCES = "balances", "Balances"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPLIED = "applied", "Applied"
        DISMISSED = "dismissed", "Dismissed"
        STALE = "stale", "No longer applies"

    # Cascades with the conversation, so proposals expire and are deleted with it.
    conversation = models.ForeignKey(AiConversation, on_delete=models.CASCADE, related_name="proposals")
    message = models.ForeignKey(AiConversationMessage, on_delete=models.CASCADE, related_name="proposals")
    kind = models.CharField(max_length=16, choices=Kind)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            return self.filter(conversation__member=person, conversation__expires_at__gt=timezone.now())

    objects = QuerySet.as_manager()

    class Meta:
        ordering = ("pk",)
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    kind__in=(
                        "set_category", "create_rule", "set_budget", "add_tags",
                        "planned_items", "savings_goals", "set_budgets",
                        "recurring", "transfers", "ai_suggestions", "notes", "splits",
                        "balances",
                    )
                ),
                name="ai_proposal_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "applied", "dismissed", "stale")),
                name="ai_proposal_status_valid",
            ),
        ]

    def __str__(self):
        return f"AI proposal {self.pk}"


class CategorySuggestion(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    member = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="ai_category_suggestions")
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="ai_suggestions")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="ai_suggestions")
    provider = models.CharField(max_length=32)
    backend = models.CharField(max_length=32)
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)
    snapshot_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class QuerySet(models.QuerySet):
        def visible_to(self, principal):
            person = _person_for(principal)
            if person is None:
                return self.none()
            visible = Transaction.objects.visible_to(person).values("pk")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other visible_to calls in the baseline
            return self.filter(member=person, transaction_id__in=visible)

    objects = QuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("member", "transaction"), name="category_suggestion_unique_member_txn"),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "accepted", "rejected")),
                name="category_suggestion_status_valid",
            ),
        ]

    def __str__(self):
        return f"AI category suggestion {self.pk}"

    @property
    def display_label(self):
        kind = "Agent Harness" if self.provider == "agent_harness" else (self.provider or "AI")
        return f"AI · {kind} · {backend_display_name(self.backend, UNKNOWN_BACKEND_LABEL)}"

