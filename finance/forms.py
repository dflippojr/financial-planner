from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError


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


class RecoveryForm(PasswordPairForm):
    username = forms.CharField(max_length=150)
    recovery_code = forms.CharField(max_length=32)

    field_order = ("username", "recovery_code", "password1", "password2")
