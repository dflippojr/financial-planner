"""Budgets."""

from decimal import Decimal

from django import forms

from django.core.exceptions import ValidationError

from ..money_input import MoneyField

from ..models import (
    Budget,
    Category,
)
from .common import AMOUNT_RANGE_ERROR, MAX_SIGNED_BIGINT
from ..budget_services import parse_month
from ..access import visible_rows


class BudgetForm(forms.Form):
    scope = forms.ChoiceField(choices=((Budget.Scope.PRIVATE, "Private"),))
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(),
        required=False,
        empty_label="Overall monthly total",
        help_text="Leave blank for an overall spending total.",
    )
    amount = MoneyField(
        min_value=Decimal("0.01"),
        max_digits=19,
        decimal_places=2,
        label="Monthly amount",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    effective_month = forms.CharField(
        label="Effective from",
        widget=forms.TextInput(attrs={"type": "month"}),
        help_text="Amount changes apply from this month onward.",
    )
    rollover_enabled = forms.BooleanField(
        required=False,
        initial=False,
        label="Rollover leftover and overspending",
    )

    def __init__(self, *args, principal=None, has_household=False, edit=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            visible_rows(Category, principal).exclude(code=Category.Code.TRANSFER).order_by("name", "pk")
        )
        if has_household:
            self.fields["scope"].choices = Budget.Scope.choices
        if edit:
            self.fields["scope"].disabled = True
            self.fields["category"].disabled = True
            self.fields.pop("rollover_enabled")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        minor_units = int(amount * 100)
        if minor_units <= 0 or minor_units > MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean_effective_month(self):

        return parse_month(self.cleaned_data["effective_month"])

    def save_payload(self):
        payload = {
            "scope": self.cleaned_data["scope"],
            "category": self.cleaned_data.get("category"),
            "amount_minor": int(self.cleaned_data["amount"] * 100),
            "effective_month": self.cleaned_data["effective_month"],
        }
        if "rollover_enabled" in self.cleaned_data:
            payload["rollover_enabled"] = self.cleaned_data["rollover_enabled"]
        return payload
