from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError

from .auth_services import validated_username
from .models import Account


MIN_SIGNED_BIGINT = -(2**63)
MAX_SIGNED_BIGINT = 2**63 - 1


class PasswordPairForm(forms.Form):
    password1 = forms.CharField(label="New password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm new password", widget=forms.PasswordInput)

    def password_user(self, cleaned):
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


class JoinForm(PasswordPairForm):
    invitation_code = forms.CharField(max_length=64)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)

    field_order = ("invitation_code", "username", "display_name", "password1", "password2")

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

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.visible_to(principal).order_by("name", "pk")

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", "End date must be on or after start date.")
        return cleaned


class TransactionCorrectionForm(forms.Form):
    transaction_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    amount = forms.DecimalField(max_digits=19, decimal_places=2, help_text="Negative is money out; positive is money in.")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        minor_units = int(amount * 100)
        if not MIN_SIGNED_BIGINT <= minor_units <= MAX_SIGNED_BIGINT:
            raise ValidationError("Amount is outside the supported range.")
        return amount

    @classmethod
    def for_transaction(cls, transaction, *args, **kwargs):
        return cls(
            *args,
            initial={
                "transaction_date": transaction.transaction_date,
                "description": transaction.description,
                "amount": transaction.amount_minor / 100,
            },
            **kwargs,
        )

    def apply(self, transaction):
        transaction.transaction_date = self.cleaned_data["transaction_date"]
        transaction.description = self.cleaned_data["description"]
        transaction.amount_minor = int(self.cleaned_data["amount"] * 100)
        transaction.save(update_fields=("transaction_date", "description", "amount_minor", "updated_at"))
        return transaction
