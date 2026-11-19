"""Transaction filters, edits, categories, notes, receipts and transfer windows."""

from decimal import Decimal

from django import forms

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.utils import timezone

from ..input_limits import MAX_DESCRIPTION_CHARS
from ..money_input import MoneyField
from ..money_display import format_minor_input
from ..date_bounds import EARLIEST_ACTIVITY_DATE, TOO_EARLY_ERROR, activity_date_error
from ..cash_flow import (
    MAX_REPORT_DATE,
    MAX_REPORT_PERIODS,
    default_date_range,
    home_default_date_range,
    period_count,
)
from ..projection import DEFAULT_HORIZON, HORIZONS

from ..models import (
    Account,
    Category,
    Tag,
)
from .common import ALL_VISIBLE_ACCOUNTS, AMOUNT_RANGE_ERROR, END_DATE_ORDER_ERROR, MAX_SIGNED_BIGINT, MIN_SIGNED_BIGINT
from ..category_services import assignable_categories
from ..manual_entry_services import manual_entry_accounts
from ..access import visible_rows


RECEIPT_ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif,application/pdf"



class TransactionFilterForm(forms.Form):
    # Desktop toolbar fields (issue #330); every other field sits behind More filters.
    TOOLBAR_FIELD_NAMES = ("q", "date_from", "date_to", "account", "category")

    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, empty_label="All accounts")
    category = forms.ChoiceField(
        required=False,
        choices=(("", "All categories"), ("uncategorized", "Uncategorized")),
    )
    q = forms.CharField(required=False, label="Search", max_length=200)
    tag = forms.ModelChoiceField(queryset=Tag.objects.none(), required=False, empty_label="All tags")
    scope = forms.ChoiceField(
        required=False,
        choices=(
            ("", ALL_VISIBLE_ACCOUNTS),
            (Account.Scope.PRIVATE, "Private"),
            (Account.Scope.HOUSEHOLD, "Household"),
        ),
    )
    amount_min = MoneyField(
        max_digits=19,
        decimal_places=2,
        required=False,
        label="Amount min",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    amount_max = MoneyField(
        max_digits=19,
        decimal_places=2,
        required=False,
        label="Amount max",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    amount_mode = forms.ChoiceField(
        required=False,
        choices=(
            ("signed", "Signed (money in or out)"),
            ("absolute", "Absolute value"),
        ),
        initial="signed",
    )
    has_note = forms.ChoiceField(
        required=False,
        choices=(("", "Any notes"), ("1", "Has note")),
    )
    is_split = forms.ChoiceField(
        required=False,
        choices=(("", "Any splits"), ("1", "Split")),
    )
    set_by = forms.ChoiceField(
        required=False,
        choices=(
            ("", "Any category source"),
            ("hand", "Set by hand"),
            ("rule", "Set by rule"),
            ("suggestion", "Set by AI suggestion"),
        ),
    )

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            visible_rows(Account, principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["tag"].queryset = visible_rows(Tag, principal).order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        choices = [("", "All categories"), ("uncategorized", "Uncategorized"), ("transfer", "Transfer")]
        if principal is not None:

            for category in assignable_categories(principal).exclude(code=Category.Code.UNCATEGORIZED):
                choices.append((str(category.pk), category.name))
        self.fields["category"].choices = choices

    def toolbar_fields(self):
        return [self[name] for name in self.TOOLBAR_FIELD_NAMES]

    def more_fields(self):
        return [field for field in self if field.name not in self.TOOLBAR_FIELD_NAMES]

    def clean_amount_min(self):
        return self._cleaned_amount("amount_min")

    def clean_amount_max(self):
        return self._cleaned_amount("amount_max")

    def _cleaned_amount(self, field_name):
        amount = self.cleaned_data.get(field_name)
        if amount is None:
            return None
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", END_DATE_ORDER_ERROR)
        amount_min = cleaned.get("amount_min")
        amount_max = cleaned.get("amount_max")
        mode = cleaned.get("amount_mode") or "signed"
        if amount_min is not None and amount_max is not None:
            if mode == "absolute":
                if abs(amount_min) > abs(amount_max):
                    self.add_error("amount_max", "Maximum amount must be at least the minimum.")
            elif amount_min > amount_max:
                self.add_error("amount_max", "Maximum amount must be at least the minimum.")
        return cleaned



class SavedTransactionFilterNameForm(forms.Form):
    name = forms.CharField(max_length=80, label="Save current filters as")



class CashFlowFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        validators=[MaxValueValidator(MAX_REPORT_DATE)],
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        validators=[MaxValueValidator(MAX_REPORT_DATE)],
    )
    grouping = forms.ChoiceField(
        choices=(
            ("month", "Month"),
            ("week", "Week"),
            ("quarter", "Quarter"),
            ("year", "Year"),
        )
    )
    horizon = forms.TypedChoiceField(
        required=False,
        coerce=int,
        choices=tuple((value, f"{value} months") for value in HORIZONS),
        initial=DEFAULT_HORIZON,
        label="Projection horizon",
    )
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

    def clean(self):
        cleaned = super().clean()
        # Home opens on This month (#700). A missing end is today; a missing start is
        # the first of the end date's month, so an end before this month stays valid.
        _default_from, default_to = home_default_date_range()
        date_to = cleaned.get("date_to") or default_to
        date_from = cleaned.get("date_from") or date_to.replace(day=1)
        cleaned["date_from"] = date_from
        cleaned["date_to"] = date_to
        grouping = cleaned.get("grouping")
        cleaned["horizon"] = cleaned.get("horizon") or DEFAULT_HORIZON
        if date_from > date_to:
            self.add_error("date_to", END_DATE_ORDER_ERROR)
        elif grouping and period_count(date_from, date_to, grouping) > MAX_REPORT_PERIODS:
            self.add_error(
                None,
                f"That range has more than {MAX_REPORT_PERIODS} periods. "
                "Choose a shorter range or a longer grouping.",
            )
        return cleaned



class YearEndFilterForm(forms.Form):
    year = forms.IntegerField(min_value=1, max_value=MAX_REPORT_DATE.year)
    scope = forms.ChoiceField(
        required=False,
        choices=(
            ("", ALL_VISIBLE_ACCOUNTS),
            (Account.Scope.PRIVATE, "Private"),
            (Account.Scope.HOUSEHOLD, "Household"),
        ),
    )



class SpendingFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        validators=[MaxValueValidator(MAX_REPORT_DATE)],
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        validators=[MaxValueValidator(MAX_REPORT_DATE)],
    )
    grouping = forms.ChoiceField(
        choices=(
            ("month", "Month"),
            ("week", "Week"),
            ("quarter", "Quarter"),
            ("year", "Year"),
        ),
        initial="month",
    )
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

    def __init__(self, data=None, *args, principal=None, **kwargs):
        if data is not None and "grouping" not in data:
            data = data.copy()
            data["grouping"] = "month"
        super().__init__(data, *args, **kwargs)
        self.fields["account"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            visible_rows(Account, principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["tag"].queryset = visible_rows(Tag, principal).order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field

    def clean(self):
        cleaned = super().clean()
        default_from, default_to = default_date_range()
        date_from = cleaned.get("date_from") or default_from
        date_to = cleaned.get("date_to") or default_to
        cleaned["date_from"] = date_from
        cleaned["date_to"] = date_to
        grouping = cleaned.get("grouping") or "month"
        cleaned["grouping"] = grouping
        if date_from > date_to:
            self.add_error("date_to", END_DATE_ORDER_ERROR)
        elif period_count(date_from, date_to, grouping) > MAX_REPORT_PERIODS:
            self.add_error(
                None,
                f"That range has more than {MAX_REPORT_PERIODS} periods. "
                "Choose a shorter range or a longer grouping.",
            )
        return cleaned



class TransactionCorrectionForm(forms.Form):
    transaction_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(max_length=MAX_DESCRIPTION_CHARS, widget=forms.Textarea(attrs={"rows": 3}))
    amount = MoneyField(
        max_digits=19,
        decimal_places=2,
        help_text="Negative is money out; positive is money in.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    def clean_transaction_date(self):
        value = self.cleaned_data["transaction_date"]
        error = activity_date_error(value)
        if error:
            raise ValidationError(error)
        return value

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    @classmethod
    def for_transaction(cls, transaction, *args, **kwargs):
        return cls(
            *args,
            initial={
                "transaction_date": transaction.transaction_date,
                "description": transaction.description,
                "amount": format_minor_input(transaction.amount_minor),
            },
            **kwargs,
        )



class ManualTransactionForm(forms.Form):
    DIRECTION_OUT = "out"
    DIRECTION_IN = "in"

    account = forms.ModelChoiceField(queryset=Account.objects.none(), empty_label=None)
    transaction_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    direction = forms.ChoiceField(
        label="Money in or out",
        choices=((DIRECTION_OUT, "Out (spending, payment, withdrawal)"), (DIRECTION_IN, "In (income, deposit, refund)")),
        initial=DIRECTION_OUT,
    )
    amount = MoneyField(
        max_digits=19,
        decimal_places=2,
        min_value=Decimal("0.01"),
        help_text="Enter the amount without a sign; the choice above sets money in or out.",
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )
    description = forms.CharField(max_length=500, help_text="For example the payee, or the check number and payee.")
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, empty_label="Uncategorized")
    note = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["account"].queryset = manual_entry_accounts(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        self.fields["category"].queryset = assignable_categories(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        self.fields["tags"].queryset = visible_rows(Tag, principal).active().order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        if not self.is_bound:
            self.initial.setdefault("transaction_date", timezone.localdate())
        if not self.fields["tags"].queryset.exists():  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField's queryset; pyright types form fields as the base Field
            del self.fields["tags"]

    def clean_transaction_date(self):
        value = self.cleaned_data["transaction_date"]
        if value > timezone.localdate():
            raise ValidationError("Date cannot be in the future. Use a planned item for an expected amount.")
        if value < EARLIEST_ACTIVITY_DATE:
            raise ValidationError(TOO_EARLY_ERROR)
        return value

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if int(amount * 100) > MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def amount_minor(self):
        minor = int(self.cleaned_data["amount"] * 100)
        return -minor if self.cleaned_data["direction"] == self.DIRECTION_OUT else minor



class TransactionCategoryForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, empty_label="Uncategorized")

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = assignable_categories(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field



class CategoryNameForm(forms.Form):
    name = forms.CharField(max_length=80)



class TagNameForm(forms.Form):
    name = forms.CharField(max_length=80)



class TransactionNoteTagsForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Tags",
    )
    new_tag = forms.CharField(required=False, max_length=80, label="Create tag")

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tags"].queryset = visible_rows(Tag, principal).active().order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field

    @classmethod
    def for_transaction(cls, transaction, principal, *args, **kwargs):
        return cls(
            *args,
            principal=principal,
            initial={
                "note": transaction.note,
                "tags": list(transaction.tags.active().values_list("pk", flat=True)),
            },
            **kwargs,
        )



class BulkTransactionEditForm(forms.Form):
    ACTION_CATEGORY = "set_category"
    ACTION_ADD_TAGS = "add_tags"
    ACTION_REMOVE_TAGS = "remove_tags"
    ACTION_APPEND_NOTE = "append_note"
    ACTION_MARK_UNTRACKED = "mark_untracked"
    ACTION_UNMARK_UNTRACKED = "unmark_untracked"

    action = forms.ChoiceField(
        choices=(
            (ACTION_CATEGORY, "Set category"),
            (ACTION_ADD_TAGS, "Add tags"),
            (ACTION_REMOVE_TAGS, "Remove tags"),
            (ACTION_APPEND_NOTE, "Append note"),
            (ACTION_MARK_UNTRACKED, "Mark as transfer to or from an account I don't track"),
            (ACTION_UNMARK_UNTRACKED, "Remove untracked-transfer mark"),
        )
    )
    select_matching = forms.BooleanField(required=False)
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(),
        required=False,
        empty_label="Uncategorized",
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    note_line = forms.CharField(required=False, max_length=2000, label="Note line")

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = assignable_categories(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        self.fields["tags"].queryset = visible_rows(Tag, principal).active().order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field



class TransferWindowForm(forms.Form):
    transfer_match_window_days = forms.IntegerField(min_value=0, max_value=366, label="Match window (days)")



class ReceiptUploadForm(forms.Form):
    receipt = forms.FileField(
        label="Receipt photo or PDF",
        widget=forms.FileInput(
            attrs={
                "accept": RECEIPT_ACCEPT,
                "capture": "environment",
            }
        ),
        help_text="JPEG, PNG, WebP, HEIC, or PDF. Up to 10 MB, 5 per transaction.",
    )
