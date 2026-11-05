"""Money inputs accept ordinary formats such as 10,000 and $10,000 (#657)."""

import ast
import inspect
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

import finance.audit_views
import finance.csv_import.forms
import finance.forms
from finance.category_services import ensure_household_categories
from finance.models import (
    Account,
    AlertSettings,
    Budget,
    Household,
    Membership,
    Person,
    PlannedItem,
    SavingsGoal,
    Transaction,
)
from finance.money_input import MoneyField, PercentField, WholePercentField

PASSWORD = "Synthetic-passphrase-42!"
FINANCE_DIR = Path(finance.forms.__file__).resolve().parent

# -- the shared field ---------------------------------------------------------

ACCEPTED = [
    ("10000", Decimal("10000")),
    ("10,000", Decimal("10000")),
    ("$10,000", Decimal("10000")),
    ("$10,000.50", Decimal("10000.50")),
    (" 10,000 ", Decimal("10000")),
    ("10000.5", Decimal("10000.5")),
    ("$ 10,000", Decimal("10000")),
    (".50", Decimal("0.50")),
    ("-25", Decimal("-25")),
    ("-$25", Decimal("-25")),
    ("$-25", Decimal("-25")),
    ("$1,234,567.89", Decimal("1234567.89")),
    ("1,000.", Decimal("1000")),
    ("+25", Decimal("25")),
]

REJECTED = [
    "1,00",
    "10,00,0",
    "1,0000",
    ",100",
    "100,",
    "1.2.3",
    "1,000.00.0",
    "abc",
    "12a",
    "1e5",
    "NaN",
    "Infinity",
    "1_000",
    "€10",
    "£10",
    "10 USD",
    "US$10",
    "$",
    "-",
    ".",
    "--25",
    "-$-25",
    "$$25",
    "25$",
    "1 000",
    "- 25",
    "١٢٣",  # Arabic-Indic digits: Decimal() takes them, the field must not
]


def money_field(**kwargs):
    kwargs.setdefault("max_digits", 19)
    kwargs.setdefault("decimal_places", 2)
    return MoneyField(**kwargs)


@pytest.mark.parametrize(("text", "expected"), ACCEPTED)
def test_money_field_accepts_ordinary_formats(text, expected):
    assert money_field().clean(text) == expected


@pytest.mark.parametrize("text", REJECTED)
def test_money_field_rejects_malformed_text_with_the_normal_error(text):
    with pytest.raises(ValidationError) as caught:
        money_field().clean(text)
    assert caught.value.messages == ["Enter a number."]


@pytest.mark.parametrize("text", ["", "   "])
def test_money_field_empty_is_required_or_none(text):
    with pytest.raises(ValidationError) as caught:
        money_field().clean(text)
    assert caught.value.messages == ["This field is required."]
    assert money_field(required=False).clean(text) is None


def test_min_max_and_decimal_places_still_apply_after_normalizing():
    positive = money_field(min_value=Decimal("0.01"))
    with pytest.raises(ValidationError, match="greater than or equal to 0.01"):
        positive.clean("$0")
    with pytest.raises(ValidationError, match="greater than or equal to 0.01"):
        positive.clean("$-25")
    assert positive.clean("$0.01") == Decimal("0.01")
    capped = money_field(max_value=Decimal("10000000000"), max_digits=15)
    with pytest.raises(ValidationError, match="less than or equal to"):
        capped.clean("$10,000,000,000.01")
    with pytest.raises(ValidationError, match="2 decimal places"):
        money_field().clean("$10,000.505")
    with pytest.raises(ValidationError, match="19 digits"):
        money_field().clean("$1" + ",000" * 6 + ".00")


@pytest.mark.parametrize(
    "stored",
    [Decimal("0"), Decimal("-0.01"), Decimal("1000000.00"), Decimal("1E+3"), Decimal("12.5"), Decimal("99999999999999999.99")],
)
def test_a_stored_amount_renders_in_a_form_the_field_accepts_back(stored):
    field = money_field()
    rendered = field.widget.format_value(field.prepare_value(stored))
    assert "E" not in rendered
    assert field.clean(rendered) == stored


def test_unbound_initial_decimal_renders_plainly():
    class GoalLike(forms.Form):
        amount = money_field()

    html = str(GoalLike(initial={"amount": Decimal("1E+3")})["amount"])
    assert 'value="1000"' in html


@pytest.mark.parametrize(("text", "expected"), [("19.99", Decimal("19.99")), (" 19.99 ", Decimal("19.99")), ("19.99%", Decimal("19.99")), ("19.99 %", Decimal("19.99"))])
def test_percent_field_tolerates_spaces_and_a_trailing_percent(text, expected):
    assert PercentField(max_digits=6, decimal_places=3).clean(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("10", 10), (" 10 ", 10), ("10%", 10), (" 10 % ", 10)])
def test_whole_percent_field_tolerates_spaces_and_a_trailing_percent(text, expected):
    assert WholePercentField(min_value=1, max_value=100).clean(text) == expected


@pytest.mark.parametrize("text", ["10%%", "%10", "$10", "10,0%"])
def test_percent_fields_still_reject_other_text(text):
    with pytest.raises(ValidationError):
        WholePercentField().clean(text)
    with pytest.raises(ValidationError):
        PercentField(max_digits=6, decimal_places=3).clean(text)


def test_count_fields_tolerate_surrounding_spaces():
    form = finance.forms.TransferWindowForm({"transfer_match_window_days": " 5 "})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["transfer_match_window_days"] == 5


def test_alert_percent_fields_accept_a_trailing_percent():
    form = finance.forms.AlertSettingsForm(
        {"recurring_price_percent": "15%", "unusual_category_percent": " 60 % ", "unusual_category_amount": "$1,000"}
    )
    form.is_valid()
    for name in ("recurring_price_percent", "unusual_category_percent", "unusual_category_amount"):
        assert name not in form.errors
    assert form.cleaned_data["recurring_price_percent"] == 15
    assert form.cleaned_data["unusual_category_percent"] == 60
    assert form.cleaned_data["unusual_category_amount"] == Decimal("1000")


def test_percent_inputs_render_as_text_so_a_browser_lets_a_percent_sign_through():
    # A type=number input drops "15%" before it is sent, so the parser would never see it.
    form = finance.forms.AlertSettingsForm()
    for name in ("recurring_price_percent", "unusual_category_percent"):
        html = str(form[name])
        assert 'type="text"' in html, html
        assert 'inputmode="numeric"' in html, html
    apr = str(finance.forms.DebtTermsForm()["apr_percent"])
    assert 'type="text"' in apr and 'inputmode="decimal"' in apr


@pytest.mark.django_db
def test_alert_settings_page_saves_a_percent_typed_with_a_percent_sign():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    page = client.get(reverse("settings-alerts")).content.decode()
    assert 'name="recurring_price_percent"' in page
    response = client.post(
        reverse("settings-alerts"),
        {"recurring_price_enabled": "on", "recurring_price_percent": "25%", "unusual_category_percent": " 60 % "},
    )
    assert response.status_code == 302
    prefs = AlertSettings.objects.get(person=owner)
    assert prefs.recurring_price_percent == 25
    assert prefs.unusual_category_percent == 60


def test_debt_terms_apr_accepts_a_trailing_percent_and_payment_accepts_dollars():
    form = finance.forms.DebtTermsForm({"apr_percent": "19.99%", "minimum_payment": "$1,250.00"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["apr_percent"] == Decimal("19.99")
    assert form.cleaned_data["minimum_payment"] == Decimal("1250.00")


# -- every money input uses the shared field ---------------------------------

FORM_MODULES = (finance.forms, finance.csv_import.forms, finance.audit_views)
# Decimal fields that are not dollar amounts. A new entry needs a reason.
NOT_MONEY_DECIMALS = {("DebtTermsForm", "apr_percent")}  # a percent, PercentField
PERCENT_INTEGERS = {
    ("AlertSettingsForm", "recurring_price_percent"),
    ("AlertSettingsForm", "unusual_category_percent"),
}


def _form_classes():
    for module in FORM_MODULES:
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, forms.BaseForm) and cls.__module__ == module.__name__:
                yield cls


def test_every_declared_decimal_field_is_a_money_or_percent_field():
    money = set()
    for cls in _form_classes():
        for name, field in cls.base_fields.items():
            key = (cls.__name__, name)
            if isinstance(field, forms.DecimalField):
                if key in NOT_MONEY_DECIMALS:
                    assert isinstance(field, PercentField), key
                else:
                    assert isinstance(field, MoneyField), f"{key} is a plain DecimalField; use MoneyField"
                    money.add(key)
            if "percent" in name and isinstance(field, forms.IntegerField) and not isinstance(field, forms.DecimalField):
                assert key in PERCENT_INTEGERS and isinstance(field, WholePercentField), key
    # The issue's named forms are all covered.
    for key in [
        ("SavingsGoalForm", "target_amount"),
        ("SavingsGoalForm", "manual_amount"),
        ("BudgetForm", "amount"),
        ("PlannedItemForm", "amount"),
        ("ManualTransactionForm", "amount"),
        ("ManualBalanceForm", "amount"),
        ("ManualBalanceForm", "net_contribution"),
        ("DebtTermsForm", "minimum_payment"),
        ("DebtPlannerForm", "extra"),
        ("ScenarioChangeForm", "amount"),
        ("TransactionCorrectionForm", "amount"),
    ]:
        assert key in money, key


def test_no_form_module_builds_a_plain_decimal_field():
    """Catches fields added at run time too, such as the split part amounts."""
    offenders = []
    for path in sorted(FINANCE_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"DecimalField", "FloatField"} and getattr(node.func.value, "id", None) == "forms":
                    offenders.append(f"{path.relative_to(FINANCE_DIR)}:{node.lineno}")
    assert offenders == [], f"use MoneyField or PercentField from finance.money_input: {offenders}"


# -- end to end through the views ---------------------------------------------


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


class _FormScraper(HTMLParser):
    """Collects each <form>'s successful controls as a browser would submit them."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self._form = None
        self._select = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._form = {"data": {}, "names": set()}
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        name = attrs.get("name")
        if tag == "input" and name:
            self._form["names"].add(name)
            kind = attrs.get("type", "text")
            if kind in {"submit", "button", "file", "image", "reset"}:
                return
            if kind in {"checkbox", "radio"} and "checked" not in attrs:
                return
            self._form["data"].setdefault(name, []).append(attrs.get("value", "on" if kind == "checkbox" else ""))
        elif tag == "select" and name:
            self._form["names"].add(name)
            self._select = {"name": name, "first": None, "chosen": [], "multiple": "multiple" in attrs}
        elif tag == "option" and self._select is not None:
            value = attrs.get("value", "")
            if self._select["first"] is None:
                self._select["first"] = value
            if "selected" in attrs:
                self._select["chosen"].append(value)
        elif tag == "textarea" and name:
            self._form["names"].add(name)
            self._textarea = name
            self._form["data"][name] = [""]

    def handle_data(self, data):
        if self._textarea is not None:
            self._form["data"][self._textarea][0] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select" and self._select is not None:
            chosen = self._select["chosen"]
            if not chosen and not self._select["multiple"] and self._select["first"] is not None:
                chosen = [self._select["first"]]
            if chosen:
                self._form["data"][self._select["name"]] = chosen
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def resubmit_unchanged(client, url, field):
    """GET an edit page, then POST its form back exactly as rendered."""
    page = client.get(url)
    assert page.status_code == 200
    scraper = _FormScraper()
    scraper.feed(page.content.decode())
    matching = [form for form in scraper.forms if field in form["names"]]
    assert len(matching) == 1, f"expected one form with {field}"
    data = matching[0]["data"]
    response = client.post(url, data)
    assert response.status_code == 302, response.content.decode()[:2000]
    return data[field][0]


@pytest.mark.django_db
@pytest.mark.parametrize(("typed", "minor"), [("10,000", 1_000_000), ("$10,000.50", 1_000_050), ("$ 1,250", 125_000)])
def test_savings_goal_add_and_edit_accept_formatted_amounts(typed, minor):
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)

    response = client.post(
        reverse("savings-goals"),
        {
            "name": "Emergency fund",
            "target_amount": typed,
            "target_date": "2026-12-31",
            "scope": "private",
            "linked_account": "",
            "manual_amount": "$1,000.25",
            "manual_amount_date": "2026-01-01",
        },
    )

    assert response.status_code == 302
    goal = SavingsGoal.objects.get(name="Emergency fund")
    assert goal.target_amount_minor == minor
    assert goal.manual_amount_minor == 100_025
    shown = resubmit_unchanged(client, reverse("savings-goal-edit", args=[goal.pk]), "target_amount")
    goal.refresh_from_db()
    assert goal.target_amount_minor == minor
    assert goal.manual_amount_minor == 100_025
    assert Decimal(shown) * 100 == minor


@pytest.mark.django_db
def test_savings_goal_rejects_malformed_amount_and_stores_nothing():
    owner = make_person("owner")
    make_household(owner)
    response = signed_in(owner).post(
        reverse("savings-goals"),
        {"name": "Emergency fund", "target_amount": "10,00,0", "target_date": "", "scope": "private"},
    )
    assert response.status_code == 200
    assert "Enter a number." in response.content.decode()
    assert not SavingsGoal.objects.exists()


@pytest.mark.django_db
def test_budget_add_and_edit_accept_formatted_amounts():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    client = signed_in(owner)

    response = client.post(
        reverse("budgets") + "?month=2026-10",
        {
            "scope": "private",
            "category": str(groceries.pk),
            "amount": "$1,200.50",
            "effective_month": "2026-10",
            "month": "2026-10",
        },
    )

    assert response.status_code == 302
    budget = Budget.objects.get(owner=owner, category=groceries)
    assert budget.amounts.get().amount_minor == 120_050
    resubmit_unchanged(client, reverse("budget-edit", args=[budget.pk]) + "?month=2026-10", "amount")
    assert sorted(budget.amounts.values_list("amount_minor", flat=True)) == [120_050]


@pytest.mark.django_db
def test_budget_rejects_zero_after_normalizing_and_stores_nothing():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    response = signed_in(owner).post(
        reverse("budgets") + "?month=2026-10",
        {"scope": "private", "category": str(groceries.pk), "amount": "$0", "effective_month": "2026-10", "month": "2026-10"},
    )
    assert response.status_code == 200
    assert "greater than or equal to 0.01" in response.content.decode()
    assert not Budget.objects.exists()


@pytest.mark.django_db
def test_planned_item_add_and_edit_accept_formatted_amounts():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)

    response = client.post(
        reverse("planned-items"),
        {
            "name": "Synthetic rent",
            "kind": PlannedItem.Kind.EXPENSE,
            "amount": "$2,450",
            "start_date": "2026-11-01",
            "cadence": PlannedItem.Cadence.MONTHLY,
            "scope": PlannedItem.Scope.PRIVATE,
        },
    )

    assert response.status_code == 302
    item = PlannedItem.objects.get()
    assert item.amount_minor == 245_000
    resubmit_unchanged(client, reverse("planned-item-edit", args=[item.pk]), "amount")
    item.refresh_from_db()
    assert item.amount_minor == 245_000


@pytest.mark.django_db
def test_planned_item_rejects_letters_and_stores_nothing():
    owner = make_person("owner")
    make_household(owner)
    response = signed_in(owner).post(
        reverse("planned-items"),
        {
            "name": "Synthetic rent",
            "kind": PlannedItem.Kind.EXPENSE,
            "amount": "2450 dollars",
            "start_date": "2026-11-01",
            "cadence": PlannedItem.Cadence.MONTHLY,
            "scope": PlannedItem.Scope.PRIVATE,
        },
    )
    assert response.status_code == 200
    assert not PlannedItem.objects.exists()


@pytest.mark.django_db
def test_manual_transaction_add_and_correction_accept_formatted_amounts():
    owner = make_person("owner")
    make_household(owner)
    account = Account.objects.create(
        name="Synthetic Cash", account_type=Account.Type.CHECKING, owner=owner, scope=Account.Scope.PRIVATE
    )
    client = signed_in(owner)

    response = client.post(
        reverse("transaction-add"),
        {
            "account": account.pk,
            "transaction_date": "2026-09-27",
            "direction": "out",
            "amount": "$1,234.56",
            "description": "SYNTHETIC FURNITURE",
            "category": "",
            "note": "",
        },
    )

    assert response.status_code == 302
    txn = Transaction.objects.get(account=account)
    assert txn.amount_minor == -123_456
    shown = resubmit_unchanged(client, reverse("transaction-edit", args=[txn.pk]), "amount")
    assert shown == "-1234.56"
    txn.refresh_from_db()
    assert txn.amount_minor == -123_456

    corrected = client.post(
        reverse("transaction-edit", args=[txn.pk]),
        {"transaction_date": "2026-09-27", "description": "SYNTHETIC FURNITURE", "amount": "-$1,000"},
    )
    assert corrected.status_code == 302
    txn.refresh_from_db()
    assert txn.amount_minor == -100_000


@pytest.mark.django_db
def test_manual_transaction_rejects_another_currency_and_stores_nothing():
    owner = make_person("owner")
    make_household(owner)
    account = Account.objects.create(
        name="Synthetic Cash", account_type=Account.Type.CHECKING, owner=owner, scope=Account.Scope.PRIVATE
    )
    response = signed_in(owner).post(
        reverse("transaction-add"),
        {
            "account": account.pk,
            "transaction_date": "2026-09-27",
            "direction": "out",
            "amount": "€12.00",
            "description": "SYNTHETIC GROCER",
            "category": "",
            "note": "",
        },
    )
    assert response.status_code == 200
    assert "Enter a number." in response.content.decode()
    assert not Transaction.objects.filter(account=account).exists()
