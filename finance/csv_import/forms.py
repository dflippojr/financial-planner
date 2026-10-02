from django import forms

from finance.models import ImportBatch

from .parser import DATE_FORMATS, NUMBER_FORMATS, Mapping
from .profiles import GENERIC, PROFILE_CHOICES, normalize_profile


class CsvUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV file",
        error_messages={"required": "Choose a CSV file of at most 5 MB."},
    )
    import_profile = forms.ChoiceField(
        label="Import profile",
        choices=PROFILE_CHOICES,
        required=False,
        initial=GENERIC,
    )

    def clean_import_profile(self):
        return normalize_profile(self.cleaned_data.get("import_profile"))


class HuntingtonImportForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    source = forms.CharField(widget=forms.HiddenInput, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean_source(self):
        return ImportBatch.Source.HUNTINGTON


class AppleCardImportForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    source = forms.CharField(widget=forms.HiddenInput, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean_source(self):
        return ImportBatch.Source.APPLE_CARD


class CsvMappingForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    date_column = forms.ChoiceField()
    description_column = forms.ChoiceField()
    date_format = forms.ChoiceField(choices=[(key, label) for key, (label, _pattern) in DATE_FORMATS.items()])
    number_format = forms.ChoiceField(choices=[(key, label) for key, (label, _decimal, _thousands) in NUMBER_FORMATS.items()])
    amount_mode = forms.ChoiceField(
        choices=(("signed", "One signed amount column"), ("separate", "Separate debit and credit columns")),
        widget=forms.RadioSelect,
    )
    amount_column = forms.ChoiceField(required=False)
    debit_column = forms.ChoiceField(required=False)
    credit_column = forms.ChoiceField(required=False)
    currency_column = forms.ChoiceField(required=False)
    invert_sign = forms.BooleanField(
        required=False,
        help_text="Use when the source shows money out as positive; positive values become negative when stored.",
    )
    source = forms.ChoiceField(choices=ImportBatch.Source.choices, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, headers, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(header, header) for header in headers]
        optional_choices = [("", "Use USD for every row")] + choices
        for field in ("date_column", "description_column", "amount_column", "debit_column", "credit_column"):
            self.fields[field].choices = choices
        self.fields["currency_column"].choices = optional_choices

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("amount_mode")
        if mode == "signed" and not cleaned.get("amount_column"):
            self.add_error("amount_column", "Choose the signed amount column.")
        if mode == "separate":
            if not cleaned.get("debit_column"):
                self.add_error("debit_column", "Choose the debit column.")
            if not cleaned.get("credit_column"):
                self.add_error("credit_column", "Choose the credit column.")
            if cleaned.get("debit_column") == cleaned.get("credit_column") and cleaned.get("debit_column"):
                self.add_error("credit_column", "Debit and credit must use different columns.")
        return cleaned

    def mapping(self):
        return Mapping(
            date_column=self.cleaned_data["date_column"],
            description_column=self.cleaned_data["description_column"],
            date_format=self.cleaned_data["date_format"],
            number_format=self.cleaned_data["number_format"],
            amount_mode=self.cleaned_data["amount_mode"],
            amount_column=self.cleaned_data.get("amount_column", ""),
            debit_column=self.cleaned_data.get("debit_column", ""),
            credit_column=self.cleaned_data.get("credit_column", ""),
            currency_column=self.cleaned_data.get("currency_column", ""),
            invert_sign=self.cleaned_data.get("invert_sign", False),
        )
