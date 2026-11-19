"""Projections, debt planning, planned items and bills."""

from types import SimpleNamespace
from decimal import Decimal

from django import forms

from django.core.exceptions import ValidationError
from django.db.models import Q

from ..money_input import MoneyField

from ..models import (
    Account,
    Category,
    PlannedItem,
    RecurringSeries,
)
from .common import AMOUNT_RANGE_ERROR, END_DATE_ORDER_ERROR, MAX_SIGNED_BIGINT
from ..scenario import CHANGE_ADD, CHANGE_AMOUNT, CHANGE_ONEOFF, CHANGE_PAUSE
from ..bills_calendar import deposit_accounts
from ..access import visible_rows


class ScenarioChangeForm(forms.Form):
    change_type = forms.ChoiceField(
        choices=(
            ("add", "New income or expense"),
            ("amount", "Change a planned item amount"),
            ("pause", "Pause a recurring series"),
            ("oneoff", "One-off amount on a date"),
        ),
        label="Change",
    )
    name = forms.CharField(max_length=150, required=False)
    kind = forms.ChoiceField(choices=PlannedItem.Kind.choices, required=False)
    amount = MoneyField(
        required=False,
        min_value=Decimal("0.01"),
        max_digits=19,
        decimal_places=2,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    cadence = forms.ChoiceField(choices=PlannedItem.Cadence.choices, required=False)
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    planned_item = forms.ModelChoiceField(queryset=PlannedItem.objects.none(), required=False)
    series = forms.ModelChoiceField(
        queryset=RecurringSeries.objects.none(),
        required=False,
        label="Recurring series",
    )
    pause_from = forms.DateField(
        required=False,
        label="Pause from",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, planned_items=None, series=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["planned_item"].queryset = planned_items if planned_items is not None else PlannedItem.objects.none()  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        self.fields["series"].queryset = series if series is not None else RecurringSeries.objects.none()  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is None:
            return amount
        minor_units = int(amount * 100)
        if minor_units <= 0 or minor_units > MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean(self):
        cleaned = super().clean()
        change_type = cleaned.get("change_type")
        if change_type in ("add", "oneoff"):
            for field in ("name", "kind", "amount", "start_date"):
                if not cleaned.get(field):
                    self.add_error(field, "This field is required.")
            if change_type == "add" and not cleaned.get("cadence"):
                self.add_error("cadence", "This field is required.")
            start_date = cleaned.get("start_date")
            end_date = cleaned.get("end_date")
            if start_date and end_date and end_date < start_date:
                self.add_error("end_date", END_DATE_ORDER_ERROR)
        elif change_type == "amount":
            if cleaned.get("planned_item") is None:
                self.add_error("planned_item", "This field is required.")
            if cleaned.get("amount") is None:
                self.add_error("amount", "This field is required.")
        elif change_type == "pause":
            if cleaned.get("series") is None:
                self.add_error("series", "This field is required.")
            if not cleaned.get("pause_from"):
                self.add_error("pause_from", "This field is required.")
        return cleaned

    def to_change(self):

        cleaned = self.cleaned_data
        change_type = cleaned["change_type"]
        amount_minor = int(cleaned["amount"] * 100) if cleaned.get("amount") is not None else None
        if change_type == "add":
            return SimpleNamespace(
                type=CHANGE_ADD,
                name=cleaned["name"],
                kind=cleaned["kind"],
                amount_minor=amount_minor,
                cadence=cleaned["cadence"],
                start=cleaned["start_date"],
                end=cleaned.get("end_date"),
            )
        if change_type == "oneoff":
            return SimpleNamespace(
                type=CHANGE_ONEOFF,
                name=cleaned["name"],
                kind=cleaned["kind"],
                amount_minor=amount_minor,
                cadence="one_time",
                start=cleaned["start_date"],
                end=None,
            )
        if change_type == "amount":
            return SimpleNamespace(
                type=CHANGE_AMOUNT,
                source_id=cleaned["planned_item"].pk,
                amount_minor=amount_minor,
            )
        return SimpleNamespace(
            type=CHANGE_PAUSE,
            source_id=cleaned["series"].pk,
            pause_from=cleaned["pause_from"],
        )



class DebtPlannerForm(forms.Form):
    strategy = forms.ChoiceField(
        choices=(
            ("minimums", "Minimums only"),
            ("snowball", "Snowball (smallest balance first)"),
            ("avalanche", "Avalanche (highest APR first)"),
            ("custom", "Custom order"),
        )
    )
    extra = MoneyField(
        label="Extra monthly payment",
        required=False,
        min_value=0,
        max_digits=19,
        decimal_places=2,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
        help_text="Applied after minimums, toward the current target debt.",
    )

    def clean_extra(self):
        amount = self.cleaned_data.get("extra")
        if amount is None:
            return None
        minor_units = int(amount * 100)
        if not 0 <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def extra_minor(self):
        amount = self.cleaned_data.get("extra")
        if amount is None:
            return 0
        return int(amount * 100)



class PlannedItemForm(forms.Form):
    name = forms.CharField(max_length=150)
    kind = forms.ChoiceField(choices=PlannedItem.Kind.choices)
    amount = MoneyField(
        min_value=Decimal("0.01"),
        max_digits=19,
        decimal_places=2,
        help_text="Amount in dollars. The sign comes from income or expense.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    cadence = forms.ChoiceField(choices=PlannedItem.Cadence.choices)
    scope = forms.ChoiceField(choices=((PlannedItem.Scope.PRIVATE, "Private"),))
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    replaces_series = forms.ModelChoiceField(
        queryset=RecurringSeries.objects.none(),
        required=False,
        label="Replaces recurring series",
    )

    def __init__(
        self, *args, principal=None, has_household=False, household_only=False, current_series_id=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = visible_rows(Category, principal).order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        # Keep the item's current series selectable even if it has since gone
        # inactive, so saving the form does not silently drop the link.
        offered = Q(status=RecurringSeries.Status.CONFIRMED, is_active=True, cancelled_at__isnull=True)
        if current_series_id is not None:
            offered |= Q(pk=current_series_id)
        self.fields["replaces_series"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            visible_rows(RecurringSeries, principal).filter(offered).order_by("display_name", "pk")
        )
        if household_only:
            # Only an item's owner can take a household item private.
            self.fields["scope"].choices = ((PlannedItem.Scope.HOUSEHOLD, PlannedItem.Scope.HOUSEHOLD.label),)
        elif has_household:
            self.fields["scope"].choices = PlannedItem.Scope.choices

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        minor_units = int(amount * 100)
        if minor_units <= 0 or minor_units > MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", END_DATE_ORDER_ERROR)
        return cleaned

    def save_payload(self):
        amount = self.cleaned_data["amount"]
        return {
            "name": self.cleaned_data["name"],
            "kind": self.cleaned_data["kind"],
            "amount_minor": int(amount * 100),
            "start_date": self.cleaned_data["start_date"],
            "end_date": self.cleaned_data.get("end_date"),
            "cadence": self.cleaned_data["cadence"],
            "scope": self.cleaned_data["scope"],
            "category": self.cleaned_data.get("category"),
            "replaces_series": self.cleaned_data.get("replaces_series"),
        }



class BillsCalendarForm(forms.Form):
    accounts = forms.ModelMultipleChoiceField(
        queryset=Account.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "checkbox"}),
        label="Checking and savings accounts",
        help_text="Expected balance starts from these accounts' latest snapshots.",
    )
    threshold_amount = MoneyField(
        required=False,
        min_value=Decimal("0.00"),
        max_digits=18,  # cents must fit the BIGINT column
        decimal_places=2,
        label="Highlight below",
        help_text="USD. Days whose expected balance is below this amount are highlighted. Leave blank for no highlight.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["accounts"].queryset = deposit_accounts(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field

    def save_payload(self):
        amount = self.cleaned_data.get("threshold_amount")
        minor = int(amount * 100) if amount is not None else None
        return {
            "account_ids": [account.pk for account in self.cleaned_data["accounts"]],
            "threshold_minor": minor,
        }
