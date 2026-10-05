from django import forms

from finance.models import ImportBatch

from .parser import DATE_FORMATS, NUMBER_FORMATS, Mapping
from .profiles import GENERIC, PROFILE_CHOICES, normalize_profile
from .saved_mappings import parse_saved_profile, saved_profile_key

# Hand-entered rows are batches too, but never a file import source.
FILE_IMPORT_SOURCE_CHOICES = [choice for choice in ImportBatch.Source.choices if choice[0] != ImportBatch.Source.MANUAL]


class CsvUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV, OFX or QFX file",
        help_text="Upload .csv, .ofx, .qfx or .qbo, at most 5 MB. Choose OFX / QFX for statement files.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.ofx,.qfx,.qbo"}),
        error_messages={"required": "Choose a CSV, OFX or QFX file of at most 5 MB."},
    )
    import_profile = forms.ChoiceField(
        label="Import profile",
        choices=PROFILE_CHOICES,
        required=False,
        initial=GENERIC,
    )

    def __init__(self, *args, saved_mappings=(), default_profile=GENERIC, **kwargs):
        super().__init__(*args, **kwargs)
        extra = [(saved_profile_key(item), item.name) for item in saved_mappings]
        self.fields["import_profile"].choices = [*PROFILE_CHOICES, *extra]
        self._saved_ids = {item.pk for item in saved_mappings}
        if not self.is_bound:
            self.initial.setdefault("import_profile", default_profile)
            self.fields["import_profile"].initial = default_profile

    def clean_import_profile(self):
        value = self.cleaned_data.get("import_profile")
        saved_id = parse_saved_profile(value)
        if saved_id is not None:
            if saved_id not in self._saved_ids:
                raise forms.ValidationError("Choose a mapping you can use.")
            return value
        return normalize_profile(value)


class HuntingtonImportForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    source = forms.CharField(widget=forms.HiddenInput, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean_source(self):
        return ImportBatch.Source.HUNTINGTON


class CapitalOneImportForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    source = forms.CharField(widget=forms.HiddenInput, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean_source(self):
        return ImportBatch.Source.CAPITAL_ONE


class AppleCardImportForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    source = forms.CharField(widget=forms.HiddenInput, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean_source(self):
        return ImportBatch.Source.APPLE_CARD


class OfxImportForm(HuntingtonImportForm):
    def clean_source(self):
        return ImportBatch.Source.OFX


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
    source = forms.ChoiceField(choices=FILE_IMPORT_SOURCE_CHOICES, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    save_mapping_as = forms.CharField(
        required=False,
        max_length=80,
        label="Save this mapping as",
        help_text="Stores the column mapping and this file's headers for the household. Cell values are never stored.",
    )
    set_as_account_default = forms.BooleanField(
        required=False,
        label="Use as the default mapping for this account",
    )

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


class SavedMappingImportForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    source = forms.ChoiceField(choices=FILE_IMPORT_SOURCE_CHOICES, required=False)
    date_range_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_range_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))


class SavedCsvMappingEditForm(forms.Form):
    name = forms.CharField(max_length=80)
    date_column = forms.ChoiceField()
    description_column = forms.ChoiceField()
    date_format = forms.ChoiceField(choices=[(key, label) for key, (label, _pattern) in DATE_FORMATS.items()])
    number_format = forms.ChoiceField(
        choices=[(key, label) for key, (label, _decimal, _thousands) in NUMBER_FORMATS.items()]
    )
    amount_mode = forms.ChoiceField(
        choices=(("signed", "One signed amount column"), ("separate", "Separate debit and credit columns")),
        widget=forms.RadioSelect,
    )
    amount_column = forms.ChoiceField(required=False)
    debit_column = forms.ChoiceField(required=False)
    credit_column = forms.ChoiceField(required=False)
    currency_column = forms.ChoiceField(required=False)
    invert_sign = forms.BooleanField(required=False)
    default_accounts = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Default for accounts",
    )

    def __init__(self, *args, headers, account_choices, locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(header, header) for header in headers]
        optional_choices = [("", "Use USD for every row")] + choices
        for field in ("date_column", "description_column", "amount_column", "debit_column", "credit_column"):
            self.fields[field].choices = choices
        self.fields["currency_column"].choices = optional_choices
        self.fields["default_accounts"].choices = account_choices
        if locked:
            for field in (
                "date_column",
                "description_column",
                "date_format",
                "number_format",
                "amount_mode",
                "amount_column",
                "debit_column",
                "credit_column",
                "currency_column",
                "invert_sign",
            ):
                self.fields[field].disabled = True

    def clean(self):
        cleaned = super().clean()
        if self.fields["date_column"].disabled:
            return cleaned
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
            amount_column=self.cleaned_data.get("amount_column", "") or "",
            debit_column=self.cleaned_data.get("debit_column", "") or "",
            credit_column=self.cleaned_data.get("credit_column", "") or "",
            currency_column=self.cleaned_data.get("currency_column", "") or "",
            invert_sign=self.cleaned_data.get("invert_sign", False),
        )
