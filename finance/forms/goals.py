"""Savings goals, fund moves and set-asides, shortfalls, imports and buffers."""

from types import SimpleNamespace
from decimal import Decimal

from django import forms

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from ..access import visible_rows
from ..money_input import MoneyField

from ..models import (
    Account,
    PlannedItem,
    SavingsGoal,
)
from .common import AMOUNT_RANGE_ERROR, MAX_SIGNED_BIGINT, MIN_SIGNED_BIGINT
from ..access import require_person
from ..households import current_household
from ..savings_goal_services import (
    TARGET_REQUIRED_ERROR,
    validate_fund,
    validate_planned_item_link,
)
from ..savings_goal_services import DEPENDENTS_SCOPE_ERROR, validate_dependency


class SavingsGoalForm(forms.Form):
    name = forms.CharField(max_length=150)
    target_amount = MoneyField(
        required=False,
        min_value=Decimal("0.01"),
        max_digits=19,
        decimal_places=2,
        label="Target amount",
        help_text="Leave blank to use the amount of the planned item this goal funds.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    target_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Optional. Leave blank for a wishlist goal with no deadline.",
    )
    priority = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=1_000_000,
        help_text="Funding plan rank, 1 is funded first. Leave blank to rank after every numbered goal.",
    )
    depends_on = forms.ModelChoiceField(
        queryset=SavingsGoal.objects.none(),
        required=False,
        empty_label="No dependency",
        label="Buy after",
        help_text="The funding plan funds this goal only after that goal. It must have the same scope.",
    )
    time_sensitive = forms.BooleanField(
        required=False,
        help_text="The funding plan flags this goal when it would miss its target date.",
    )
    scope = forms.ChoiceField(choices=((SavingsGoal.Scope.PRIVATE, "Private"),))
    linked_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        required=False,
        empty_label="No linked account",
    )
    share_of_account = forms.BooleanField(
        required=False,
        label="Sinking fund: hold a share of the linked account",
        help_text=(
            "The goal counts only the money assigned to it, so several goals can split one checking or "
            "savings account. A household fund needs a household account; a private fund one of your own."
        ),
    )
    funds_planned_item = forms.ModelChoiceField(
        queryset=PlannedItem.objects.none(),
        required=False,
        empty_label="No planned item",
        label="Funds planned expense",
        help_text=(
            "Sinking funds only. A blank target and date come from the item: its amount and next date. "
            "When the expense posts, the fund pays for it."
        ),
    )
    manual_amount = MoneyField(
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

    def __init__(self, *args, principal=None, has_household=False, household_only=False, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.principal = principal
        self.instance = instance
        self.fields["linked_account"].queryset = visible_rows(Account, principal).order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        # Keep the current dependency selectable even if it has since been
        # archived, so saving the form does not silently drop the link.
        offered = Q(status=SavingsGoal.Status.ACTIVE)
        if instance is not None:
            offered |= Q(pk=instance.depends_on_id)
        candidates = visible_rows(SavingsGoal, principal).filter(offered)
        if instance is not None:
            candidates = candidates.exclude(pk=instance.pk)
        self.fields["depends_on"].queryset = candidates.order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        items = visible_rows(PlannedItem, principal).filter(kind=PlannedItem.Kind.EXPENSE)
        current_item = instance.funds_planned_item_id if instance is not None else None
        items = items.filter(Q(enabled=True) | Q(pk=current_item))
        self.fields["funds_planned_item"].queryset = items.order_by("name", "pk")  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        if household_only:
            # Only a goal's owner can take a household goal private.
            self.fields["scope"].choices = ((SavingsGoal.Scope.HOUSEHOLD, SavingsGoal.Scope.HOUSEHOLD.label),)
        elif has_household:
            self.fields["scope"].choices = SavingsGoal.Scope.choices

    def clean_target_amount(self):
        amount = self.cleaned_data["target_amount"]
        if amount is None:
            return amount
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
        self._check_dependency(cleaned)
        self._check_fund(cleaned)
        return cleaned

    def _check_fund(self, cleaned):
        """Mirror the service's sinking-fund checks so a bad choice is a form error, not a 404."""

        share = cleaned.get("share_of_account", False)
        item = cleaned.get("funds_planned_item")
        scope = cleaned.get("scope")
        if "target_amount" in cleaned and cleaned.get("target_amount") is None and not (share and item):
            self.add_error("target_amount", TARGET_REQUIRED_ERROR)
        if scope is None:
            return
        if share and cleaned.get("manual_amount") is not None:
            self.add_error("manual_amount", "A fund's amount comes from what you assign to it; leave this blank.")
        instance = self.instance if self.instance is not None else SavingsGoal()
        if not share and not instance.share_of_account and item is None:
            return
        person = require_person(self.principal, check_authenticated=False)
        household = current_household(person) if scope == SavingsGoal.Scope.HOUSEHOLD else None
        account = cleaned.get("linked_account")
        saved_account_id = instance.linked_account_id  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        if account is None and saved_account_id is not None and not visible_rows(Account, person).filter(
            pk=saved_account_id
        ).exists():
            account = instance.linked_account
        owner_id = instance.owner_id  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        try:
            validate_fund(
                instance, share=share, account=account, scope=scope, household=household,
                owner_id=owner_id if owner_id is not None else person.pk,
            )
        except ValidationError as exc:
            self.add_error("share_of_account" if not share else "linked_account", exc.messages[0])
        try:
            validate_planned_item_link(person, instance, item, scope=scope, household=household, share=share)
        except (ValidationError, PermissionDenied) as exc:
            message = exc.messages[0] if isinstance(exc, ValidationError) else "Choose a planned item you can see."
            self.add_error("funds_planned_item", message)

    def _check_dependency(self, cleaned):

        dependency = cleaned.get("depends_on")
        scope = cleaned.get("scope")
        if scope is None:
            return
        instance = self.instance
        if instance is not None and instance.scope != scope and instance.dependents.exclude(scope=scope).exists():
            self.add_error("scope", DEPENDENTS_SCOPE_ERROR)
        if dependency is None:
            return
        person = require_person(self.principal, check_authenticated=False)
        try:
            validate_dependency(
                person,
                instance if instance is not None else SavingsGoal(),
                dependency,
                scope=scope,
                household=current_household(person),
            )
        except (ValidationError, PermissionDenied) as exc:
            message = exc.messages[0] if isinstance(exc, ValidationError) else "Choose a goal you can see."
            self.add_error("depends_on", message)

    def save_payload(self):
        manual_amount = self.cleaned_data.get("manual_amount")
        target = self.cleaned_data.get("target_amount")
        return {
            "name": self.cleaned_data["name"],
            "target_amount_minor": int(target * 100) if target is not None else None,
            "target_date": self.cleaned_data.get("target_date"),
            "priority": self.cleaned_data.get("priority"),
            "depends_on": self.cleaned_data.get("depends_on"),
            "time_sensitive": self.cleaned_data.get("time_sensitive", False),
            "scope": self.cleaned_data["scope"],
            "linked_account": self.cleaned_data.get("linked_account"),
            "manual_amount_minor": int(manual_amount * 100) if manual_amount is not None else None,
            "manual_amount_date": self.cleaned_data.get("manual_amount_date"),
            "share_of_account": self.cleaned_data.get("share_of_account", False),
            "funds_planned_item": self.cleaned_data.get("funds_planned_item"),
        }



def _fund_amount_field(**kwargs):
    return MoneyField(
        min_value=Decimal("0.01"),
        max_digits=18,  # cents must fit the BIGINT column
        decimal_places=2,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
        **kwargs,
    )



class FundMoveForm(forms.Form):
    """Assign money to a sinking fund, or release it back to the account (#670)."""

    ASSIGN = "assign"
    RELEASE = "release"
    action = forms.ChoiceField(
        choices=((ASSIGN, "Assign from the account's unassigned money"), (RELEASE, "Release back to the account")),
        initial=ASSIGN,
        label="Move",
    )
    amount = _fund_amount_field(label="Amount")

    def amount_minor(self):
        return int(self.cleaned_data["amount"] * 100)



class FundSetAsideForm(forms.Form):
    """This month's suggested set-asides: a checkbox and an editable amount per fund (#670)."""

    def __init__(self, *args, rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = list(rows)
        for row in self.rows:
            pk = row.goal.pk
            self.fields[f"pick_{pk}"] = forms.BooleanField(
                required=False, initial=row.default_minor > 0, label=f"Set aside for {row.goal.name}"
            )
            self.fields[f"amount_{pk}"] = _fund_amount_field(
                required=False,
                label=f"Amount for {row.goal.name}",
                initial=Decimal(row.default_minor) / Decimal(100) if row.default_minor else None,
            )

    def clean(self):
        cleaned = super().clean()
        for row in self.rows:
            pk = row.goal.pk
            if cleaned.get(f"pick_{pk}") and cleaned.get(f"amount_{pk}") is None and f"amount_{pk}" not in self.errors:
                self.add_error(f"amount_{pk}", "Enter an amount for each fund you check.")
        return cleaned

    def picks(self):
        return [
            (row.goal, int(self.cleaned_data[f"amount_{row.goal.pk}"] * 100))
            for row in self.rows
            if self.cleaned_data.get(f"pick_{row.goal.pk}")
        ]

    def row_fields(self):
        return [
            SimpleNamespace(row=row, pick=self[f"pick_{row.goal.pk}"], amount=self[f"amount_{row.goal.pk}"])
            for row in self.rows
        ]



class FundShortfallForm(forms.Form):
    """How much to take out of each fund after an account's balance fell below them (#670)."""

    def __init__(self, *args, funds=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.funds = list(funds)
        for row in self.funds:
            self.fields[f"reduce_{row.goal.pk}"] = MoneyField(
                required=False,
                min_value=Decimal("0"),
                max_digits=18,
                decimal_places=2,
                label=f"Take out of {row.goal.name}",
                help_text=f"Holds {row.assigned_display}.",
                widget=forms.TextInput(attrs={"inputmode": "decimal"}),
            )

    def reductions(self):
        return [
            (row.goal, int((self.cleaned_data.get(f"reduce_{row.goal.pk}") or Decimal(0)) * 100))
            for row in self.funds
        ]



class GoalImportForm(forms.Form):
    goals_file = forms.FileField(
        label="Wishlist file",
        help_text="A .csv or .json file of at most 256 KB.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.json"}),
        error_messages={"required": "Choose a CSV or JSON wishlist file."},
    )



class SavingsBufferForm(forms.Form):
    buffer = MoneyField(
        required=False,
        min_value=Decimal("0"),
        max_value=Decimal("10000000000"),
        max_digits=15,
        decimal_places=2,
        label="Safety buffer",
        help_text="Held back once before any goal is funded. Leave blank to use one month of average spending.",
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    def buffer_minor(self):
        value = self.cleaned_data.get("buffer")
        return None if value is None else int(value * 100)
