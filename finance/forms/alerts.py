"""Alert settings for email and ntfy."""

from decimal import Decimal

from django import forms


from ..money_input import MoneyField, WholePercentField

from ..models import (
    NTFY_TOPIC_VALIDATOR,
)


class AlertSettingsForm(forms.Form):
    sync_enabled = forms.BooleanField(required=False, label="Sync failures and re-linking")
    recurring_price_enabled = forms.BooleanField(required=False, label="Recurring price changes")
    recurring_price_percent = WholePercentField(
        required=False,
        min_value=1,
        max_value=100,
        initial=10,
        label="Recurring price change percent",
        help_text="Flag a recurring charge when it differs from the median of up to 12 earlier charges by more than this percent.",
    )
    recurring_missed_enabled = forms.BooleanField(required=False, label="Missed recurring charges")
    budget_enabled = forms.BooleanField(required=False, label="Budgets near or over the limit")
    large_transaction_enabled = forms.BooleanField(required=False, label="Large transactions")
    monthly_review_enabled = forms.BooleanField(required=False, label="Monthly review")
    monthly_review_ai_enabled = forms.BooleanField(
        required=False,
        label="AI monthly review summary",
        help_text="On by default when an AI backend is connected and the privacy policy is accepted. The facts list stays the source of truth.",
    )
    expected_balance_enabled = forms.BooleanField(
        required=False,
        label="Expected balance below threshold in the next 7 days",
        help_text="Off by default. Uses the accounts and threshold from the bills calendar. Figures are expected, not guaranteed.",
    )
    import_due_enabled = forms.BooleanField(
        required=False,
        label="Accounts due for a CSV import",
        help_text="On by default. For accounts without a SimpleFIN transactions link whose newest import is over a month old.",
    )
    fund_enabled = forms.BooleanField(
        required=False,
        label="Sinking funds",
        help_text="On by default. Each month's suggested set-asides, and a fund account whose balance fell below what its funds hold.",
    )
    unusual_spending_enabled = forms.BooleanField(required=False, label="Unusual spending")
    unusual_spending_ai_enabled = forms.BooleanField(
        required=False,
        label="AI unusual-spending summary",
        help_text="On by default when an AI backend is connected. Phrases this month's flags in two or three sentences. The flags stay the source of truth.",
    )
    unusual_category_percent = WholePercentField(
        required=False,
        min_value=1,
        max_value=1000,
        initial=50,
        label="Category unusual percent",
        help_text="Flag a category when this month exceeds its trailing 6-month median by at least this percent and by the dollar floor.",
    )
    unusual_category_amount = MoneyField(
        required=False,
        min_value=Decimal("0.00"),
        max_digits=18,  # cents must fit the BIGINT column
        decimal_places=2,
        initial=Decimal("50.00"),
        label="Category unusual dollar floor",
        help_text="USD. Combined with the percent: both must be exceeded versus the trailing 6-month median.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    large_transaction_amount = MoneyField(
        required=False,
        min_value=Decimal("0.01"),
        max_digits=18,  # cents must fit the BIGINT column
        decimal_places=2,
        label="Large transaction threshold",
        help_text="USD. Leave blank to keep large-transaction alerts off. Also used for new-merchant unusual flags.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    # The settings page shows the fields grouped by alert kind under one save (issue #339).
    KIND_GROUPS = (
        ("Accounts and balances", ("sync_enabled", "expected_balance_enabled", "import_due_enabled")),
        ("Recurring charges", ("recurring_price_enabled", "recurring_price_percent", "recurring_missed_enabled")),
        ("Budgets", ("budget_enabled",)),
        ("Savings goals", ("fund_enabled",)),
        ("Large transactions", ("large_transaction_enabled", "large_transaction_amount")),
        ("Unusual spending", (
            "unusual_spending_enabled", "unusual_spending_ai_enabled",
            "unusual_category_percent", "unusual_category_amount",
        )),
        ("Monthly review", ("monthly_review_enabled", "monthly_review_ai_enabled")),
    )

    def groups(self):
        return [{"label": label, "fields": [self[name] for name in names]} for label, names in self.KIND_GROUPS]

    def save_payload(self):
        amount = self.cleaned_data.get("large_transaction_amount")
        minor = int(amount * 100) if amount is not None else None
        floor = self.cleaned_data.get("unusual_category_amount")
        if floor is None:
            floor = Decimal("50.00")
        percent = self.cleaned_data.get("unusual_category_percent")
        if percent is None:
            percent = 50
        recurring_percent = self.cleaned_data.get("recurring_price_percent")
        if recurring_percent is None:
            recurring_percent = 10
        return {
            "sync_enabled": self.cleaned_data["sync_enabled"],
            "recurring_price_enabled": self.cleaned_data["recurring_price_enabled"],
            "recurring_price_percent": recurring_percent,
            "recurring_missed_enabled": self.cleaned_data["recurring_missed_enabled"],
            "budget_enabled": self.cleaned_data["budget_enabled"],
            "large_transaction_enabled": self.cleaned_data["large_transaction_enabled"],
            "monthly_review_enabled": self.cleaned_data["monthly_review_enabled"],
            "monthly_review_ai_enabled": self.cleaned_data["monthly_review_ai_enabled"],
            "expected_balance_enabled": self.cleaned_data["expected_balance_enabled"],
            "import_due_enabled": self.cleaned_data["import_due_enabled"],
            "fund_enabled": self.cleaned_data["fund_enabled"],
            "unusual_spending_enabled": self.cleaned_data["unusual_spending_enabled"],
            "unusual_spending_ai_enabled": self.cleaned_data["unusual_spending_ai_enabled"],
            "unusual_category_percent": percent,
            "unusual_category_floor_minor": int(floor * 100),
            "large_transaction_minor": minor,
        }



class AlertNtfySettingsForm(forms.Form):
    ntfy_enabled = forms.BooleanField(required=False, label="Push recurring price changes and the weekly digest to ntfy")
    ntfy_topic = forms.CharField(
        required=False, max_length=64, label="ntfy topic", validators=[NTFY_TOPIC_VALIDATOR],
        help_text="1-64 letters, digits, hyphens or underscores. Pushes name the series and its old and new price, "
                  "never accounts or balances.",
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("ntfy_enabled") and not cleaned.get("ntfy_topic"):
            self.add_error("ntfy_topic", "Enter a topic to enable ntfy notices.")
        return cleaned



class AlertEmailSettingsForm(forms.Form):
    email_enabled = forms.BooleanField(required=False, label="Email me when new alerts arrive")
    notification_email = forms.EmailField(required=False, max_length=254, label="Notification address",
        help_text="Notices contain only alert counts and kinds, with a link to the inbox.")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("email_enabled") and not cleaned.get("notification_email"):
            self.add_error("notification_email", "Enter an address to enable email notices.")
        return cleaned
