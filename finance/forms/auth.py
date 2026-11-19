"""Sign-in, joining, setup, password, re-auth and recovery forms."""


from django import forms

from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError


from ..auth_services import validated_username
from ..policy_services import current_policy


class PasswordPairForm(forms.Form):
    # Set by a view when a signed-in member changes their own password.
    existing_user: AbstractUser | None = None
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
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"autocomplete": "username"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))



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
            self.initial.setdefault("privacy_policy_version", current_policy().version)  # pyright: ignore[reportOptionalMemberAccess] - current_policy() is None only before a policy is seeded



class JoinForm(PrivacyPolicyOnboardingMixin, PasswordPairForm):
    invitation_code = forms.CharField(max_length=64)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)

    field_order = [  # pyright: ignore[reportOptionalMemberAccess] - current_policy() is None only before a policy is seeded
        "invitation_code",
        "username",
        "display_name",
        "password1",
        "password2",
        "privacy_policy_version",
        "accept_privacy_policy",
    ]

    def clean_username(self):
        username = self.cleaned_data["username"]
        validated_username(username)
        return username



class SetupForm(PrivacyPolicyOnboardingMixin, PasswordPairForm):
    setup_code = forms.CharField(max_length=128, widget=forms.PasswordInput)
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(max_length=150)
    household_name = forms.CharField(max_length=150, label="Household name")

    field_order = [  # pyright: ignore[reportOptionalMemberAccess] - current_policy() is None only before a policy is seeded
        "setup_code",
        "username",
        "display_name",
        "household_name",
        "password1",
        "password2",
        "privacy_policy_version",
        "accept_privacy_policy",
    ]

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

    field_order = ["username", "recovery_code", "password1", "password2"]



class RecoveryCodeOnlyForm(forms.Form):
    recovery_code = forms.CharField(max_length=32, label="Recovery code")
