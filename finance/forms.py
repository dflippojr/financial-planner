from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db.models import Q
from django.utils import timezone

from .cash_flow import MAX_REPORT_DATE, MAX_REPORT_PERIODS, default_date_range, period_count
from .projection import DEFAULT_HORIZON, HORIZONS

from .auth_services import validated_username
from .models import (
    Account,
    Category,
    PlannedItem,
    RecurringSeries,
    SavingsGoal,
    Transaction,
    TransactionCorrectionHistory,
)


MIN_SIGNED_BIGINT = -(2**63)
MAX_SIGNED_BIGINT = 2**63 - 1
ALL_VISIBLE_ACCOUNTS = "All visible accounts"
END_DATE_ORDER_ERROR = "End date must be on or after start date."
AMOUNT_RANGE_ERROR = "Amount is outside the supported range."


class PasswordPairForm(forms.Form):
    password1 = forms.CharField(label="New password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm new password", widget=forms.PasswordInput)

    def password_user(self, cleaned):
        if getattr(self, "existing_user", None) is not None:
            return self.existing_user
        return get_user_model()(username=cleaned.get("username", ""))

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password1")
        if password and password != cleaned.get("password2"):
            self.add_error("password2", "The two passwords do not match.")
        if password:
            try:
                password_validation.validate_password(password, self.password_user(cleaned))
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned


class LoginForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput)


class ReauthPasswordForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput)


class JoinForm(PasswordPairForm):
    invitation_code = forms.CharField(max_length=64)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)

    field_order = ("invitation_code", "username", "display_name", "password1", "password2")

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class SetupForm(PasswordPairForm):
    setup_code = forms.CharField(max_length=128, widget=forms.PasswordInput)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)
    household_name = forms.CharField(max_length=150, label="Household name")

    field_order = (
        "setup_code",
        "username",
        "display_name",
        "household_name",
        "password1",
        "password2",
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class JoinGoogleForm(forms.Form):
    invitation_code = forms.CharField(max_length=64)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class SetupGoogleForm(forms.Form):
    setup_code = forms.CharField(max_length=128, widget=forms.PasswordInput)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)
    household_name = forms.CharField(max_length=150, label="Household name")

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class RecoveryForm(PasswordPairForm):
    username = forms.CharField(max_length=150)
    recovery_code = forms.CharField(max_length=32)

    field_order = ("username", "recovery_code", "password1", "password2")


class TransactionFilterForm(forms.Form):
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False)
    category = forms.ChoiceField(
        required=False,
        choices=(("", "All categories"), ("uncategorized", "Uncategorized")),
    )
    q = forms.CharField(required=False, label="Description contains", max_length=200)
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
        self.fields["account"].queryset = Account.objects.visible_to(principal).order_by("name", "pk")
        choices = [("", "All categories"), ("uncategorized", "Uncategorized"), ("transfer", "Transfer")]
        if principal is not None:
            from .category_services import assignable_categories

            for category in assignable_categories(principal).exclude(code=Category.Code.UNCATEGORIZED):
                choices.append((str(category.pk), category.name))
        self.fields["category"].choices = choices

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", END_DATE_ORDER_ERROR)
        return cleaned


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
        self.fields["account"].queryset = Account.objects.visible_to(principal).order_by("name", "pk")

    def clean(self):
        cleaned = super().clean()
        default_from, default_to = default_date_range()
        date_from = cleaned.get("date_from") or default_from
        date_to = cleaned.get("date_to") or default_to
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


class ManualBalanceForm(forms.Form):
    snapshot_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(
        max_digits=19,
        decimal_places=2,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    net_contribution = forms.DecimalField(
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
        if account is not None and account.account_type == Account.Type.CREDIT_CARD:
            self.fields["amount"].help_text = "Amount owed. An overpayment is negative."
        elif account is not None:
            self.fields["amount"].help_text = "Current balance."
        if account is None or account.account_type != Account.Type.INVESTMENT:
            del self.fields["net_contribution"]

    def clean_snapshot_date(self):
        value = self.cleaned_data["snapshot_date"]
        if value > timezone.localdate():
            raise ValidationError("Balance date cannot be in the future.")
        return value

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean_net_contribution(self):
        amount = self.cleaned_data.get("net_contribution")
        if amount is None:
            return None
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def amount_minor(self):
        return int(self.cleaned_data["amount"] * 100)

    def net_contribution_minor(self):
        amount = self.cleaned_data.get("net_contribution")
        if amount is None:
            return None
        return int(amount * 100)


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
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False)
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
        self.fields["account"].queryset = Account.objects.visible_to(principal).order_by("name", "pk")

    def clean(self):
        cleaned = super().clean()
        default_from, default_to = default_date_range()
        date_from = cleaned.get("date_from") or default_from
        date_to = cleaned.get("date_to") or default_to
        cleaned["date_from"] = date_from
        cleaned["date_to"] = date_to
        if date_from > date_to:
            self.add_error("date_to", END_DATE_ORDER_ERROR)
        return cleaned


def _correction_history_rows(transaction, actor, recorded_at, new_date, new_description, new_amount_minor):
    rows = []
    if new_date != transaction.transaction_date:
        rows.append(
            TransactionCorrectionHistory(
                transaction=transaction,
                actor=actor,
                recorded_at=recorded_at,
                field_name=TransactionCorrectionHistory.Field.TRANSACTION_DATE,
                previous_date=transaction.transaction_date,
                new_date=new_date,
            )
        )
    if new_description != transaction.description:
        rows.append(
            TransactionCorrectionHistory(
                transaction=transaction,
                actor=actor,
                recorded_at=recorded_at,
                field_name=TransactionCorrectionHistory.Field.DESCRIPTION,
                previous_description=transaction.description,
                new_description=new_description,
            )
        )
    if new_amount_minor != transaction.amount_minor:
        rows.append(
            TransactionCorrectionHistory(
                transaction=transaction,
                actor=actor,
                recorded_at=recorded_at,
                field_name=TransactionCorrectionHistory.Field.AMOUNT_MINOR,
                previous_amount_minor=transaction.amount_minor,
                new_amount_minor=new_amount_minor,
                currency=transaction.currency,
            )
        )
    return rows


class TransactionCorrectionForm(forms.Form):
    transaction_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    amount = forms.DecimalField(
        max_digits=19,
        decimal_places=2,
        help_text="Negative is money out; positive is money in.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

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
                "amount": Decimal(transaction.amount_minor) / Decimal(100),
            },
            **kwargs,
        )

    def apply(self, transaction, *, actor):
        new_date = self.cleaned_data["transaction_date"]
        new_description = self.cleaned_data["description"]
        new_amount_minor = int(self.cleaned_data["amount"] * 100)
        recorded_at = timezone.now()
        history_rows = _correction_history_rows(
            transaction,
            actor,
            recorded_at,
            new_date,
            new_description,
            new_amount_minor,
        )
        if not history_rows:
            return transaction
        transaction.transaction_date = new_date
        transaction.description = new_description
        transaction.amount_minor = new_amount_minor
        transaction.save(update_fields=("transaction_date", "description", "amount_minor", "updated_at"))
        TransactionCorrectionHistory.objects.bulk_create(history_rows)
        return transaction


class TransactionCategoryForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, empty_label="Uncategorized")

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .category_services import assignable_categories

        self.fields["category"].queryset = assignable_categories(principal)


class RefundLinkForm(forms.Form):
    original = forms.ModelChoiceField(queryset=Transaction.objects.none(), required=True, label="Original purchase")

    def __init__(self, *args, principal=None, refund=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._refund = refund
        visible = Transaction.objects.visible_to(principal).filter(status=Transaction.Status.ACTIVE)
        if refund is not None:
            visible = visible.exclude(pk=refund.pk)
        self.fields["original"].queryset = visible.order_by("-transaction_date", "-pk")

    def clean(self):
        cleaned = super().clean()
        from .category_services import REFUND_LINK_RULE

        original = cleaned.get("original")
        refund = self._refund
        if refund is None or original is None:
            return cleaned
        if refund.amount_minor <= 0 or original.amount_minor >= 0 or refund.kind != original.kind:
            raise ValidationError(REFUND_LINK_RULE)
        return cleaned


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


class CategoryNameForm(forms.Form):
    name = forms.CharField(max_length=80)


class TransferWindowForm(forms.Form):
    transfer_match_window_days = forms.IntegerField(min_value=0, max_value=366, label="Match window (days)")


class PlannedItemForm(forms.Form):
    name = forms.CharField(max_length=150)
    kind = forms.ChoiceField(choices=PlannedItem.Kind.choices)
    amount = forms.DecimalField(
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
        self.fields["category"].queryset = Category.objects.visible_to(principal).order_by("name", "pk")
        # Keep the item's current series selectable even if it has since gone
        # inactive, so saving the form does not silently drop the link.
        offered = Q(status=RecurringSeries.Status.CONFIRMED, is_active=True)
        if current_series_id is not None:
            offered |= Q(pk=current_series_id)
        self.fields["replaces_series"].queryset = (
            RecurringSeries.objects.visible_to(principal).filter(offered).order_by("display_name", "pk")
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


class SavingsGoalForm(forms.Form):
    name = forms.CharField(max_length=150)
    target_amount = forms.DecimalField(
        min_value=Decimal("0.01"),
        max_digits=19,
        decimal_places=2,
        label="Target amount",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    target_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    scope = forms.ChoiceField(choices=((SavingsGoal.Scope.PRIVATE, "Private"),))
    linked_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        required=False,
        empty_label="No linked account",
    )
    manual_amount = forms.DecimalField(
        required=False,
        max_digits=19,
        decimal_places=2,
        label="Manual current amount",
        help_text="Used when there is no linked account, or it has no balance yet.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    manual_amount_date = forms.DateField(
        required=False,
        label="Manual amount as of",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, principal=None, has_household=False, household_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["linked_account"].queryset = Account.objects.visible_to(principal).order_by("name", "pk")
        if household_only:
            # Only a goal's owner can take a household goal private.
            self.fields["scope"].choices = ((SavingsGoal.Scope.HOUSEHOLD, SavingsGoal.Scope.HOUSEHOLD.label),)
        elif has_household:
            self.fields["scope"].choices = SavingsGoal.Scope.choices

    def clean_target_amount(self):
        amount = self.cleaned_data["target_amount"]
        minor_units = int(amount * 100)
        if minor_units <= 0 or minor_units > MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean_manual_amount(self):
        amount = self.cleaned_data.get("manual_amount")
        if amount is None:
            return amount
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError(AMOUNT_RANGE_ERROR)
        return amount

    def clean_manual_amount_date(self):
        value = self.cleaned_data.get("manual_amount_date")
        if value is not None and value > timezone.localdate():
            raise ValidationError("Manual amount date cannot be in the future.")
        return value

    def clean(self):
        cleaned = super().clean()
        manual_amount = cleaned.get("manual_amount")
        manual_amount_date = cleaned.get("manual_amount_date")
        if (manual_amount is None) != (manual_amount_date is None):
            self.add_error("manual_amount_date", "Enter both a manual amount and its date, or neither.")
        return cleaned

    def save_payload(self):
        manual_amount = self.cleaned_data.get("manual_amount")
        return {
            "name": self.cleaned_data["name"],
            "target_amount_minor": int(self.cleaned_data["target_amount"] * 100),
            "target_date": self.cleaned_data["target_date"],
            "scope": self.cleaned_data["scope"],
            "linked_account": self.cleaned_data.get("linked_account"),
            "manual_amount_minor": int(manual_amount * 100) if manual_amount is not None else None,
            "manual_amount_date": self.cleaned_data.get("manual_amount_date"),
        }


class CategoryRuleForm(forms.Form):
    owner_kind = forms.ChoiceField(
        choices=(("personal", "Personal"), ("household", "Household")),
        label="Rule type",
    )
    description_contains = forms.CharField(max_length=200, label="Description contains")
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False, empty_label="Any accessible account")
    min_amount = forms.DecimalField(
        max_digits=19,
        decimal_places=2,
        required=False,
        label="Minimum amount",
        help_text="Optional. Negative is money out; positive is money in.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    max_amount = forms.DecimalField(
        max_digits=19,
        decimal_places=2,
        required=False,
        label="Maximum amount",
        help_text="Optional. Negative is money out; positive is money in.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    category = forms.ModelChoiceField(queryset=Category.objects.none())
    priority = forms.IntegerField(initial=0, help_text="Lower numbers run first within personal or household rules.")
    enabled = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .category_services import assignable_categories

        self.fields["account"].queryset = Account.objects.visible_to(principal).order_by("name", "pk")
        self.fields["category"].queryset = assignable_categories(principal)

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
