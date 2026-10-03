from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db.models import Q
from django.utils import timezone

from .cash_flow import MAX_REPORT_DATE, MAX_REPORT_PERIODS, default_date_range, period_count
from .projection import DEFAULT_HORIZON, HORIZONS

from .auth_services import validated_username
from .policy_services import current_policy
from .models import (
    Account,
    Budget,
    Category,
    Tag,
    PlannedItem,
    RecurringSeries,
    RefundLink,
    SavingsGoal,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
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


class PrivacyPolicyOnboardingMixin(forms.Form):
    privacy_policy_version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    accept_privacy_policy = forms.BooleanField(
        required=False,
        label="I accept the privacy and data policy",
        help_text="You can finish without accepting. AI backends stay off until you accept.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("privacy_policy_version", current_policy().version)


class JoinForm(PrivacyPolicyOnboardingMixin, PasswordPairForm):
    invitation_code = forms.CharField(max_length=64)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)

    field_order = (
        "invitation_code",
        "username",
        "display_name",
        "password1",
        "password2",
        "privacy_policy_version",
        "accept_privacy_policy",
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class SetupForm(PrivacyPolicyOnboardingMixin, PasswordPairForm):
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
        "privacy_policy_version",
        "accept_privacy_policy",
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class JoinGoogleForm(PrivacyPolicyOnboardingMixin):
    invitation_code = forms.CharField(max_length=64)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username


class SetupGoogleForm(PrivacyPolicyOnboardingMixin):
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
        self.fields["account"].queryset = (
            Account.objects.visible_to(principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["tag"].queryset = Tag.objects.visible_to(principal).order_by("name", "pk")
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
        self.fields["account"].queryset = (
            Account.objects.visible_to(principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["tag"].queryset = Tag.objects.visible_to(principal).order_by("name", "pk")

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
        self.fields["account"].queryset = (
            Account.objects.visible_to(principal).for_cash_flow().order_by("name", "pk")
        )
        self.fields["tag"].queryset = Tag.objects.visible_to(principal).order_by("name", "pk")

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
        from .category_services import SPLIT_AMOUNT_ERROR

        if (
            transaction.category_source == Transaction.CategorySource.SPLIT
            and new_amount_minor != transaction.amount_minor
        ):
            raise ValidationError(SPLIT_AMOUNT_ERROR)
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
    original_part = forms.ModelChoiceField(
        queryset=TransactionSplit.objects.none(),
        required=False,
        label="Split part",
    )

    def __init__(self, *args, principal=None, refund=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._refund = refund
        visible = Transaction.objects.visible_to(principal).filter(status=Transaction.Status.ACTIVE)
        if refund is not None:
            visible = visible.exclude(pk=refund.pk)
        self.fields["original"].queryset = visible.order_by("-transaction_date", "-pk")
        self.fields["original_part"].queryset = (
            TransactionSplit.objects.filter(transaction__in=visible)
            .select_related("category", "transaction")
            .order_by("transaction_id", "position")
        )

    def clean(self):
        cleaned = super().clean()
        from .category_services import REFUND_LINK_RULE, SPLIT_PART_MISMATCH, SPLIT_PART_REQUIRED

        original = cleaned.get("original")
        refund = self._refund
        if refund is None or original is None:
            return cleaned
        if refund.amount_minor <= 0 or original.amount_minor >= 0 or refund.kind != original.kind:
            raise ValidationError(REFUND_LINK_RULE)
        part = cleaned.get("original_part")
        if original.category_source == Transaction.CategorySource.SPLIT:
            if part is None:
                raise ValidationError(SPLIT_PART_REQUIRED)
            if part.transaction_id != original.pk:
                raise ValidationError(SPLIT_PART_MISMATCH)
        elif part is not None:
            raise ValidationError(SPLIT_PART_MISMATCH)
        return cleaned


class SplitTransactionForm(forms.Form):
    part_count = forms.IntegerField(min_value=2, max_value=20, widget=forms.HiddenInput)

    def __init__(self, *args, principal=None, transaction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._transaction = transaction
        self._refunds = []
        categories = Category.objects.none()
        if principal is not None:
            from .category_services import assignable_categories

            categories = assignable_categories(principal)
        existing = []
        if transaction is not None:
            existing = list(transaction.splits.select_related("category").order_by("position"))
            if principal is not None:
                self._refunds = list(
                    Transaction.objects.visible_to(principal)
                    .filter(
                        refund_link__original=transaction,
                        status=Transaction.Status.ACTIVE,
                        pk__in=RefundLink.objects.visible_to(principal).values("refund_id"),
                    )
                    .select_related("refund_link")
                    .order_by("pk")
                )
        count = max(2, len(existing))
        if self.data:
            try:
                count = max(2, int(self.data.get("part_count", count)))
            except (TypeError, ValueError):
                count = 2
        self.part_indexes = list(range(count))
        self.fields["part_count"].initial = count
        for index in self.part_indexes:
            initial_category = existing[index].category_id if index < len(existing) else None
            initial_amount = None
            if index < len(existing):
                initial_amount = Decimal(existing[index].amount_minor) / Decimal(100)
            self.fields[f"part_{index}_category"] = forms.ModelChoiceField(
                queryset=categories,
                label=f"Part {index + 1} category",
                initial=initial_category,
            )
            self.fields[f"part_{index}_amount"] = forms.DecimalField(
                max_digits=19,
                decimal_places=2,
                label=f"Part {index + 1} amount",
                initial=initial_amount,
                widget=forms.TextInput(attrs={"inputmode": "decimal", "class": "split-part-amount"}),
            )
        self.refund_fields = []
        choices = [(str(index), f"Part {index + 1}") for index in self.part_indexes]
        for refund in self._refunds:
            name = f"refund_{refund.pk}_part"
            initial = ""
            link = getattr(refund, "refund_link", None)
            if link is not None and link.original_part_id:
                for index, part in enumerate(existing):
                    if part.pk == link.original_part_id:
                        initial = str(index)
                        break
            self.fields[name] = forms.ChoiceField(
                choices=choices,
                label=f"Refund on {refund.transaction_date} ({refund.amount_display})",
                initial=initial,
            )
            self.refund_fields.append(name)

    def parts_payload(self):
        parts = []
        for index in self.part_indexes:
            category = self.cleaned_data[f"part_{index}_category"]
            amount = self.cleaned_data[f"part_{index}_amount"]
            parts.append({"category_id": category.pk, "amount_minor": int(amount * 100)})
        return parts

    def refund_assignments(self):
        if not self.refund_fields:
            return None
        assigned = {}
        for name in self.refund_fields:
            refund_id = int(name.split("_")[1])
            assigned[refund_id] = int(self.cleaned_data[name])
        return assigned


class UnsplitTransactionForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, empty_label="Uncategorized")

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .category_services import assignable_categories

        self.fields["category"].queryset = assignable_categories(principal)


class SplitPartCategoryForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=True)

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .category_services import assignable_categories

        self.fields["category"].queryset = assignable_categories(principal)


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


class HarnessConnectForm(forms.Form):
    base_url = forms.CharField(
        label="Agent Harness Server URL",
        max_length=255,
        widget=forms.URLInput(attrs={"autocomplete": "off"}),
    )
    token = forms.CharField(
        label="App token",
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )


class AiDefaultsForm(forms.Form):
    chat_backend = forms.ChoiceField(label="Chat backend")
    background_backend = forms.ChoiceField(label="Background jobs backend")
    chat_model = forms.CharField(label="Chat model", required=False, max_length=80)
    background_model = forms.CharField(label="Background model", required=False, max_length=80)

    def __init__(self, *args, backends=(), **kwargs):
        super().__init__(*args, **kwargs)
        allow_local_chat = bool(getattr(settings, "AI_CHAT_LOCAL_ENABLED", False))
        chat_choices = [
            (item.id, item.label)
            for item in backends
            if item.suits_live and (allow_local_chat or item.id != "local")
        ]
        background_choices = [(item.id, item.label) for item in backends if item.suits_background]
        if not chat_choices:
            chat_choices = [("", "No available backend")]
        if not background_choices:
            background_choices = [("", "No available backend")]
        self.fields["chat_backend"].choices = chat_choices
        self.fields["background_backend"].choices = background_choices


class ShareAccountForm(forms.Form):
    share_mode = forms.ChoiceField(choices=Account.ShareMode.choices, label="Share as")


class ChangeShareModeForm(forms.Form):
    share_mode = forms.ChoiceField(choices=Account.ShareMode.choices)
    confirm_give_up_ownership = forms.BooleanField(required=False)


class AccountRenameForm(forms.Form):
    name = forms.CharField(max_length=150)


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
                Account.objects.visible_to(principal)
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
        self.fields["secured_asset"].queryset = assets
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
        self.fields["tags"].queryset = Tag.objects.visible_to(principal).active().order_by("name", "pk")

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


class BudgetForm(forms.Form):
    scope = forms.ChoiceField(choices=((Budget.Scope.PRIVATE, "Private"),))
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(),
        required=False,
        empty_label="Overall monthly total",
        help_text="Leave blank for an overall spending total.",
    )
    amount = forms.DecimalField(
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
        self.fields["category"].queryset = (
            Category.objects.visible_to(principal).exclude(code=Category.Code.TRANSFER).order_by("name", "pk")
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
        from .budget_services import parse_month

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

        self.fields["account"].queryset = (
            Account.objects.visible_to(principal).for_cash_flow().order_by("name", "pk")
        )
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


class AlertSettingsForm(forms.Form):
    sync_enabled = forms.BooleanField(required=False, label="Sync failures and re-linking")
    recurring_price_enabled = forms.BooleanField(required=False, label="Recurring price changes")
    recurring_missed_enabled = forms.BooleanField(required=False, label="Missed recurring charges")
    budget_enabled = forms.BooleanField(required=False, label="Budgets near or over the limit")
    large_transaction_enabled = forms.BooleanField(required=False, label="Large transactions")
    monthly_review_enabled = forms.BooleanField(required=False, label="Monthly review")
    large_transaction_amount = forms.DecimalField(
        required=False,
        min_value=Decimal("0.01"),
        max_digits=19,
        decimal_places=2,
        label="Large transaction threshold",
        help_text="USD. Leave blank to keep large-transaction alerts off.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    def save_payload(self):
        amount = self.cleaned_data.get("large_transaction_amount")
        minor = int(amount * 100) if amount is not None else None
        return {
            "sync_enabled": self.cleaned_data["sync_enabled"],
            "recurring_price_enabled": self.cleaned_data["recurring_price_enabled"],
            "recurring_missed_enabled": self.cleaned_data["recurring_missed_enabled"],
            "budget_enabled": self.cleaned_data["budget_enabled"],
            "large_transaction_enabled": self.cleaned_data["large_transaction_enabled"],
            "monthly_review_enabled": self.cleaned_data["monthly_review_enabled"],
            "large_transaction_minor": minor,
        }
