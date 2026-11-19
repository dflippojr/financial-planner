"""Sheet comparison filters, uploads, column mapping and tolerances."""

from decimal import Decimal

from django import forms

from django.core.exceptions import ValidationError

from ..money_input import MoneyField

from ..models import (
    Account,
    Tag,
    SheetComparisonSettings,
)
from .common import ALL_VISIBLE_ACCOUNTS
from ..access import visible_rows


class SheetComparisonFilterForm(forms.Form):
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False)
    tag = forms.ModelChoiceField(queryset=Tag.objects.none(), required=False, empty_label="All tags")
    scope = forms.ChoiceField(
        required=False,
        choices=(
            ("", ALL_VISIBLE_ACCOUNTS),
            (Account.Scope.PRIVATE, "Private"),
            (Account.Scope.HOUSEHOLD, "Household"),
        ),
    )

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            visible_rows(Account, principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["tag"].queryset = visible_rows(Tag, principal).order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field



class SheetCsvUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="Google Sheet CSV",
        help_text="One row per month. The file is not kept after the totals are stored.",
        error_messages={"required": "Choose a CSV file of at most 5 MB."},
    )



class SheetColumnMappingForm(forms.Form):
    month_column = forms.ChoiceField(label="Month column")
    income_column = forms.ChoiceField(label="Income column")
    spending_column = forms.ChoiceField(label="Spending column")
    spending_sign = forms.ChoiceField(
        label="Spending numbers",
        choices=SheetComparisonSettings.SpendingSign.choices,
        initial=SheetComparisonSettings.SpendingSign.UNSIGNED,
        help_text="Unsigned columns are positive spending. Signed columns use negative for spending.",
    )

    def __init__(self, *args, headers=(), **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(name, name) for name in headers]
        for name in ("month_column", "income_column", "spending_column"):
            self.fields[name].choices = choices

    def clean(self):
        cleaned = super().clean()
        columns = [cleaned.get("month_column"), cleaned.get("income_column"), cleaned.get("spending_column")]
        if all(columns) and len(set(columns)) != 3:
            raise ValidationError("Choose three different columns.")
        return cleaned



class SheetToleranceForm(forms.Form):
    tolerance = MoneyField(
        min_value=Decimal("0.00"),
        max_value=Decimal("1000000.00"),
        max_digits=12,
        decimal_places=2,
        label="Match tolerance",
        help_text="Recent months match when the net difference is this amount or less. Default is 1.00.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    def tolerance_minor(self):
        return int(self.cleaned_data["tolerance"] * 100)



class SheetMonthNoteForm(forms.Form):
    month = forms.DateField(widget=forms.HiddenInput)
    note = forms.CharField(required=False, max_length=2000, widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}))
