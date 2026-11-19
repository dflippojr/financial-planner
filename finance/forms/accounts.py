"""Accounts, balances, sharing, debts, loans, bank connections and deleting my data."""


from django import forms

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.utils import timezone

from ..money_input import MoneyField, PercentField
from ..date_bounds import EARLIEST_ACTIVITY_DATE, TOO_EARLY_ERROR
from ..cash_flow import (
    MAX_REPORT_DATE,
    MAX_REPORT_PERIODS,
    default_date_range,
    period_count,
)
from ..account_sort import CHOICES as ACCOUNT_SORT_CHOICES, PAGE_FIELDS as ACCOUNT_SORT_PAGES

from ..models import (
    Account,
)
from .common import ALL_VISIBLE_ACCOUNTS, AMOUNT_RANGE_ERROR, BALANCE_RANGE_ERROR, END_DATE_ORDER_ERROR, MAX_BALANCE_MINOR, MAX_SIGNED_BIGINT
from ..lifecycle_services import LENT_DELETE, LENT_HANDOVER
from ..access import visible_rows


class NetWorthFilterForm(forms.Form):
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
    scope = forms.ChoiceField(
        required=False,
        label="Household",
        choices=(
            ("", ALL_VISIBLE_ACCOUNTS),
            (Account.Scope.HOUSEHOLD, "Household"),
        ),
    )

    def clean(self):
        cleaned = super().clean()
        default_from, default_to = default_date_range()
        date_from = cleaned.get("date_from") or default_from
        date_to = cleaned.get("date_to") or default_to
        cleaned["date_from"] = date_from
        cleaned["date_to"] = date_to
        if date_from > date_to:
            self.add_error("date_to", END_DATE_ORDER_ERROR)
        elif period_count(date_from, date_to, "month") > MAX_REPORT_PERIODS:
            self.add_error(
                None,
                f"That range has more than {MAX_REPORT_PERIODS} periods. Choose a shorter range.",
            )
        return cleaned



class AccountSortForm(forms.Form):
    """Saves how a member sorts one page's balance-by-account list (#655)."""

    page = forms.ChoiceField(choices=[(page, page) for page in ACCOUNT_SORT_PAGES])
    sort = forms.ChoiceField(label="Sort by", required=False, choices=ACCOUNT_SORT_CHOICES)



class ManualBalanceForm(forms.Form):
    snapshot_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    amount = MoneyField(
        max_digits=19,
        decimal_places=2,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    net_contribution = MoneyField(
        label="Net contributions this period",
        max_digits=19,
        decimal_places=2,
        required=False,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
        help_text=(
            "From your statement: contributions minus withdrawals since the "
            "previous statement. Leave blank if unknown."
        ),
    )
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = account
        if account is not None and account.account_type in Account.LIABILITY_TYPES:
            self.fields["amount"].help_text = "Amount owed. An overpayment is negative."
        elif account is not None and account.is_physical_asset():
            self.fields["amount"].label = "Estimated value"
            self.fields["amount"].help_text = (
                "A personal estimate, not an appraisal or verified fact. Optional note can name the source."
            )
        elif account is not None:
            self.fields["amount"].help_text = "Current balance."
        if account is None or account.account_type != Account.Type.INVESTMENT:
            del self.fields["net_contribution"]

    def clean_snapshot_date(self):
        value = self.cleaned_data["snapshot_date"]
        if value > timezone.localdate():
            raise ValidationError("Balance date cannot be in the future.")
        if value < EARLIEST_ACTIVITY_DATE:
            raise ValidationError(TOO_EARLY_ERROR)
        return value

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if abs(int(amount * 100)) > MAX_BALANCE_MINOR:
            raise ValidationError(BALANCE_RANGE_ERROR)
        return amount

    def clean_net_contribution(self):
        amount = self.cleaned_data.get("net_contribution")
        if amount is None:
            return None
        if abs(int(amount * 100)) > MAX_BALANCE_MINOR:
            raise ValidationError(BALANCE_RANGE_ERROR)
        return amount

    def amount_minor(self):
        return int(self.cleaned_data["amount"] * 100)

    def net_contribution_minor(self):
        amount = self.cleaned_data.get("net_contribution")
        if amount is None:
            return None
        return int(amount * 100)



class AddAccountForm(forms.Form):
    name = forms.CharField(max_length=150)
    account_type = forms.ChoiceField(choices=Account.Type.choices, label="Type")
    sharing = forms.ChoiceField(
        choices=((Account.Scope.PRIVATE, "Private"),),
        initial=Account.Scope.PRIVATE,
        label="Sharing",
    )

    def __init__(self, *args, has_household=False, **kwargs):
        super().__init__(*args, **kwargs)
        if has_household:
            self.fields["sharing"].choices = (
                (Account.Scope.PRIVATE, "Private"),
                (Account.ShareMode.CO_OWNED, "Co-owned (household)"),
                (Account.ShareMode.LENT, "Lent (household)"),
            )



class SimpleFinSetupForm(forms.Form):
    token = forms.CharField(
        label="SimpleFIN setup token",
        widget=forms.Textarea(attrs={"rows": 4, "autocomplete": "off"}),
    )



class ShareAccountForm(forms.Form):
    share_mode = forms.ChoiceField(choices=Account.ShareMode.choices, label="Share as")



class ChangeShareModeForm(forms.Form):
    share_mode = forms.ChoiceField(choices=Account.ShareMode.choices)
    confirm_give_up_ownership = forms.BooleanField(required=False)



class AccountRenameForm(forms.Form):
    name = forms.CharField(max_length=150)



class DebtTermsForm(forms.Form):
    apr_percent = PercentField(
        label="APR percent",
        max_digits=6,
        decimal_places=3,
        required=False,
        min_value=0,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
        help_text="Annual percentage rate, for example 19.990.",
    )
    minimum_payment = MoneyField(
        label="Minimum payment",
        max_digits=19,
        decimal_places=2,
        required=False,
        min_value=0,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    payment_day = forms.IntegerField(
        label="Payment day",
        required=False,
        min_value=1,
        max_value=31,
        help_text="Day of the month the loan payment is due.",
    )

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = account
        if account is not None and account.account_type != Account.Type.LOAN:
            del self.fields["payment_day"]

    def clean_minimum_payment(self):
        amount = self.cleaned_data.get("minimum_payment")
        if amount is None:
            return None
        minor_units = int(amount * 100)
        if not 0 <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def minimum_payment_minor(self):
        amount = self.cleaned_data.get("minimum_payment")
        if amount is None:
            return None
        return int(amount * 100)



class PairLoanForm(forms.Form):
    secured_asset = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        required=False,
        empty_label="Not paired",
        label="Secured by",
    )

    def __init__(self, *args, loan=None, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.loan = loan
        assets = Account.objects.none()
        if loan is not None and principal is not None:
            assets = (
                visible_rows(Account, principal)
                .filter(
                    account_type__in=Account.PHYSICAL_ASSET_TYPES,
                    status=Account.Status.ACTIVE,
                    archived_at__isnull=True,
                    scope=loan.scope,
                )
                .order_by("name", "pk")
            )
            if loan.scope == Account.Scope.PRIVATE:
                assets = assets.filter(owner_id=loan.owner_id)
            else:
                assets = assets.filter(household_id=loan.household_id)
        self.fields["secured_asset"].queryset = assets  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        if loan is not None and loan.secured_asset_id:
            self.fields["secured_asset"].initial = loan.secured_asset_id



class AccountDeleteForm(forms.Form):
    confirm_name = forms.CharField(label="Type the exact account name to confirm", max_length=150)

    def __init__(self, *args, account_name, **kwargs):
        super().__init__(*args, **kwargs)
        self.account_name = account_name

    def clean_confirm_name(self):
        confirm_name = self.cleaned_data["confirm_name"]
        if confirm_name != self.account_name:
            raise ValidationError("Type the exact account name to confirm.")
        return confirm_name



class DeleteMyDataForm(forms.Form):
    confirm_username = forms.CharField(label="Type your username to confirm", max_length=150)

    def __init__(self, *args, username, lent_accounts, last_member, **kwargs):

        super().__init__(*args, **kwargs)
        self.username = username
        self.lent_accounts = list(lent_accounts)
        self.last_member = last_member
        self._lent_delete = LENT_DELETE
        self._lent_handover = LENT_HANDOVER
        for account in self.lent_accounts:
            key = f"lent_{account.pk}"
            if last_member:
                self.fields[key] = forms.CharField(widget=forms.HiddenInput(), initial=LENT_DELETE)
            else:
                self.fields[key] = forms.ChoiceField(
                    label=account.name,
                    choices=(
                        ("", "Choose…"),
                        (LENT_HANDOVER, "Hand over to household"),
                        (LENT_DELETE, "Delete with my data"),
                    ),
                )

    def clean_confirm_username(self):
        confirm_username = self.cleaned_data["confirm_username"]
        if confirm_username != self.username:
            raise ValidationError("Type your username to confirm.")
        return confirm_username

    def clean(self):
        cleaned = super().clean()
        for account in self.lent_accounts:
            key = f"lent_{account.pk}"
            choice = cleaned.get(key)
            if choice not in {self._lent_handover, self._lent_delete}:
                self.add_error(key, "Choose what happens to this lent account.")
            elif self.last_member and choice == self._lent_handover:
                self.add_error(key, "Choose what happens to this lent account.")
        return cleaned

    def lent_choices(self):
        return {account.pk: self.cleaned_data[f"lent_{account.pk}"] for account in self.lent_accounts}
