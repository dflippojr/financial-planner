"""Category rules and automatic notes."""


from django import forms

from django.core.exceptions import ValidationError

from ..input_limits import AUTO_NOTE_MAX_CHARS
from ..money_input import MoneyField

from ..models import (
    Account,
    Category,
    CategoryRule,
)
from .common import AMOUNT_RANGE_ERROR, MAX_SIGNED_BIGINT, MIN_SIGNED_BIGINT
from ..category_services import assignable_categories
from ..access import visible_rows


class CategoryRuleForm(forms.Form):
    owner_kind = forms.ChoiceField(
        choices=(("personal", "Personal"), ("household", "Household")),
        label="Rule type",
    )
    description_contains = forms.CharField(max_length=200, label="Description contains")
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, empty_label="Any accessible account")
    min_amount = MoneyField(
        max_digits=19,
        decimal_places=2,
        required=False,
        label="Minimum amount",
        help_text="Optional. Negative is money out; positive is money in.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    max_amount = MoneyField(
        max_digits=19,
        decimal_places=2,
        required=False,
        label="Maximum amount",
        help_text="Optional. Negative is money out; positive is money in.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    # Not "action": the rule pages already post that name for save, apply and reverse.
    rule_action = forms.ChoiceField(
        choices=CategoryRule.Action.choices,
        initial=CategoryRule.Action.CATEGORIZE,
        required=False,
        label="Then",
        help_text=(
            "Mark as untracked transfer leaves matching rows out of income and spending, "
            "for money moved to or from an account the planner doesn't track. Their category is kept."
        ),
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(),
        required=False,
        help_text="Required to set a category; left empty to mark as untracked transfer.",
    )
    priority = forms.IntegerField(
        initial=0,
        min_value=-(2**31),
        max_value=2**31 - 1,
        help_text="Lower numbers run first within personal or household rules.",
    )
    enabled = forms.BooleanField(required=False, initial=True)
    note = forms.CharField(
        required=False,
        max_length=AUTO_NOTE_MAX_CHARS,
        label="Note for new transactions",
        help_text=(
            "Optional. One line added below the note of each new transaction this rule applies to, "
            "visible to everyone who can see that transaction. Existing transactions get it only "
            "when you add it to them from the rule's page."
        ),
    )

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["account"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            visible_rows(Account, principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["category"].queryset = assignable_categories(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field

    def clean(self):
        cleaned = super().clean()
        # A form posted without the field (older pages, chat proposals) sets a category.
        cleaned["rule_action"] = cleaned.get("rule_action") or CategoryRule.Action.CATEGORIZE
        if cleaned["rule_action"] == CategoryRule.Action.UNTRACKED_TRANSFER:
            cleaned["category"] = None
        elif "category" in cleaned and cleaned.get("category") is None:
            self.add_error("category", "Choose a category, or mark as untracked transfer.")
        return cleaned

    def clean_min_amount(self):
        amount = self.cleaned_data.get("min_amount")
        if amount is None:
            return None
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean_max_amount(self):
        amount = self.cleaned_data.get("max_amount")
        if amount is None:
            return None
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount



class AutoNoteForm(forms.Form):
    """A recurring series' note (#684); the service trims it to one line."""

    note = forms.CharField(
        required=False,
        max_length=AUTO_NOTE_MAX_CHARS,
        label="Note for new charges",
        help_text=(
            "One line added below the note of each new charge that joins this series, visible to "
            "everyone who can see that charge. Leave empty to stop adding a note. Existing charges "
            "get it only when you add it to them below."
        ),
    )
