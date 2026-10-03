from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.budget_services import (
    amount_for,
    month_budget_cards,
    reset_budget_rollover,
    save_budget,
    set_budget_rollover,
)
from finance.cash_flow import spending_by_category_report
from finance.category_services import assign_category
from finance.export import collect_export_tables
from finance.models import (
    Account,
    Budget,
    BudgetRolloverReset,
    Household,
    ImportBatch,
    Membership,
    Person,
    Transaction,
)


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    from finance.category_services import ensure_household_categories

    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 15),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(transaction_date.year, transaction_date.month, 1),
        date_range_end=date(transaction_date.year, transaction_date.month, 28),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def add_budget(owner, *, category=None, amount_minor=10_000, month=date(2026, 1, 1), scope=Budget.Scope.PRIVATE, household=None, rollover=False):
    return save_budget(
        owner.user,
        {
            "scope": scope,
            "category": category,
            "amount_minor": amount_minor,
            "effective_month": month,
            "rollover_enabled": rollover,
        },
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_spent_matches_spending_by_category_for_same_month_and_accounts():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    grocery_tx = make_transaction(owner, checking, amount_minor=-5000, description="Synthetic groceries")
    dining_tx = make_transaction(
        owner,
        checking,
        amount_minor=-2500,
        description="Synthetic dining",
        fingerprint="c" * 64,
    )
    assign_category(owner, grocery_tx.pk, groceries.pk)
    assign_category(owner, dining_tx.pk, dining.pk)
    grocery_budget = add_budget(owner, category=groceries, amount_minor=8000)
    overall = add_budget(owner, category=None, amount_minor=20_000)
    month = date(2026, 1, 1)
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, month)}
    report = spending_by_category_report(owner, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    by_name = {row.name: row for row in report.rows}

    assert cards[grocery_budget.pk].spent_minor == by_name["Groceries"].spending_minor
    assert cards[overall.pk].spent_minor == report.total_spending_minor


@pytest.mark.django_db
def test_rollover_carries_unspent_and_overspent():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    jan = make_transaction(owner, checking, amount_minor=-4000, transaction_date=date(2026, 1, 10))
    assign_category(owner, jan.pk, groceries.pk)
    budget = add_budget(owner, category=groceries, amount_minor=10_000, rollover=True)
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 2, 1))}
    assert cards[budget.pk].carry_minor == 6000
    assert cards[budget.pk].available_minor == 16_000

    feb = make_transaction(
        owner,
        checking,
        amount_minor=-18_000,
        transaction_date=date(2026, 2, 10),
        fingerprint="d" * 64,
    )
    assign_category(owner, feb.pk, groceries.pk)
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 3, 1))}
    # January leftover 60.00 minus February overspend of 80.00 against 100.00.
    assert cards[budget.pk].carry_minor == -2000


@pytest.mark.django_db
def test_amount_change_leaves_past_months_unchanged():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 1, 1))
    save_budget(
        owner.user,
        {
            "scope": Budget.Scope.PRIVATE,
            "category": groceries,
            "amount_minor": 25_000,
            "effective_month": date(2026, 3, 1),
        },
        budget=budget,
    )
    assert amount_for(budget, date(2026, 1, 1)) == 10_000
    assert amount_for(budget, date(2026, 2, 1)) == 10_000
    assert amount_for(budget, date(2026, 3, 1)) == 25_000


@pytest.mark.django_db
def test_edit_prefill_uses_amount_in_effect_for_viewed_month():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 3, 1))
    client = signed_in(owner)
    page = client.get(reverse("budget-edit", args=[budget.pk]) + "?month=2026-01")
    assert page.status_code == 200
    prefill = page.context["form"].initial.get("amount")
    assert prefill not in (Decimal("100"), Decimal("100.00"), Decimal("100.0"))
    posted_amount = "" if prefill in (None, Decimal("0"), Decimal("0.00")) else str(prefill)
    client.post(
        reverse("budget-edit", args=[budget.pk]) + "?month=2026-01",
        {
            "amount": posted_amount,
            "effective_month": "2026-01",
            "month": "2026-01",
        },
    )
    assert amount_for(budget, date(2026, 1, 1)) == 0
    assert amount_for(budget, date(2026, 2, 1)) == 0
    assert amount_for(budget, date(2026, 3, 1)) == 10_000


@pytest.mark.django_db
def test_rollover_reset_zeroes_carry_from_that_month():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    jan = make_transaction(owner, checking, amount_minor=-1000, transaction_date=date(2026, 1, 10))
    assign_category(owner, jan.pk, groceries.pk)
    budget = add_budget(owner, category=groceries, amount_minor=10_000, rollover=True)
    reset_budget_rollover(owner.user, budget, month=date(2026, 2, 1))
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 2, 1))}
    assert cards[budget.pk].carry_minor == 0
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 3, 1))}
    assert cards[budget.pk].carry_minor == 10_000


@pytest.mark.django_db
def test_turning_rollover_on_starts_carry_that_month():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    jan = make_transaction(owner, checking, amount_minor=-1000, transaction_date=date(2026, 1, 10))
    assign_category(owner, jan.pk, groceries.pk)
    budget = add_budget(owner, category=groceries, amount_minor=10_000, rollover=False)
    set_budget_rollover(owner.user, budget, True, month=date(2026, 2, 1))
    budget.refresh_from_db()
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 2, 1))}
    assert cards[budget.pk].carry_minor == 0
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 3, 1))}
    assert cards[budget.pk].carry_minor == 10_000


@pytest.mark.django_db
def test_rollover_resets_from_earlier_period_do_not_affect_later_period():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 1, 1), rollover=True)
    reset_budget_rollover(owner.user, budget, month=date(2026, 6, 1))
    assert BudgetRolloverReset.objects.filter(budget=budget, month=date(2026, 6, 1)).exists()
    set_budget_rollover(owner.user, budget, False, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, True, month=date(2026, 3, 1))
    budget.refresh_from_db()
    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 7, 1))}
    # March through June leftover at 100.00 each; the June reset belongs to the prior period.
    assert cards[budget.pk].carry_minor == 40_000
    assert BudgetRolloverReset.objects.filter(budget=budget, month=date(2026, 6, 1)).exists()


@pytest.mark.django_db
def test_household_budget_excludes_private_spending():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Private")
    groceries = household.categories.get(name="Groceries")
    shared_tx = make_transaction(owner, shared, amount_minor=-3000, description="Synthetic shared groceries")
    private_tx = make_transaction(
        owner,
        private,
        amount_minor=-9000,
        description="Synthetic private groceries",
        fingerprint="e" * 64,
    )
    assign_category(owner, shared_tx.pk, groceries.pk)
    assign_category(owner, private_tx.pk, groceries.pk)
    budget = add_budget(
        owner,
        category=groceries,
        amount_minor=10_000,
        scope=Budget.Scope.HOUSEHOLD,
        household=household,
    )
    cards = {card.budget.pk: card for card in month_budget_cards(member.user, date(2026, 1, 1))}
    report = spending_by_category_report(
        member,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        scope=Account.Scope.HOUSEHOLD,
    )
    by_name = {row.name: row for row in report.rows}
    assert cards[budget.pk].spent_minor == by_name["Groceries"].spending_minor == 3000
    page = signed_in(member).get(reverse("budgets") + "?month=2026-01")
    body = page.content.decode()
    assert "90.00" not in body
    assert "Over by" not in body or "9,000" not in body.replace(",", "")


@pytest.mark.django_db
def test_former_member_loses_household_budget_access():
    owner = make_person("owner")
    former = make_person("former")
    household = make_household(owner, former)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(
        owner,
        category=groceries,
        amount_minor=10_000,
        scope=Budget.Scope.HOUSEHOLD,
        household=household,
    )
    membership = Membership.objects.get(person=former, household=household)
    membership.ended_at = timezone.now()
    membership.save()
    assert not Budget.objects.visible_to(former.user).filter(pk=budget.pk).exists()
    response = signed_in(former).get(reverse("budget-edit", args=[budget.pk]))
    assert response.status_code == 404
    with pytest.raises(PermissionDenied):
        save_budget(
            former.user,
            {
                "scope": Budget.Scope.HOUSEHOLD,
                "category": groceries,
                "amount_minor": 1,
                "effective_month": date(2026, 1, 1),
            },
            budget=budget,
        )


@pytest.mark.django_db
def test_private_budgets_are_owner_only():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000)
    assert Budget.objects.visible_to(owner.user).filter(pk=budget.pk).exists()
    assert not Budget.objects.visible_to(member.user).filter(pk=budget.pk).exists()
    response = signed_in(member).get(reverse("budget-edit", args=[budget.pk]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_export_includes_visible_budgets_only():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    private = add_budget(owner, category=groceries, amount_minor=10_000)
    household_budget = add_budget(
        owner,
        category=None,
        amount_minor=50_000,
        scope=Budget.Scope.HOUSEHOLD,
        household=household,
    )
    set_budget_rollover(owner.user, household_budget, True, month=date(2026, 1, 1))
    reset_budget_rollover(owner.user, household_budget, month=date(2026, 2, 1))
    owner_tables = collect_export_tables(owner)
    member_tables = collect_export_tables(member)
    owner_ids = {row["id"] for row in owner_tables["budgets"]}
    member_ids = {row["id"] for row in member_tables["budgets"]}
    assert private.pk in owner_ids
    assert household_budget.pk in owner_ids
    assert private.pk not in member_ids
    assert household_budget.pk in member_ids
    assert any(row["budget_id"] == household_budget.pk for row in owner_tables["budget_amounts"])
    assert any(row["budget_id"] == household_budget.pk for row in owner_tables["budget_rollover_resets"])
    assert not any(row["budget_id"] == private.pk for row in member_tables["budget_amounts"])


@pytest.mark.django_db
def test_budgets_page_and_dashboard_card():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    tx = make_transaction(owner, checking, amount_minor=-12_000, transaction_date=date(2026, 10, 2))
    assign_category(owner, tx.pk, groceries.pk)
    add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 10, 1))
    client = signed_in(owner)
    page = client.get(reverse("budgets") + "?month=2026-10")
    body = page.content.decode()
    assert page.status_code == 200
    assert "Groceries" in body
    assert "Over by" in body
    assert "12.00" in body or "120.00" in body
    home = client.get(reverse("home"))
    assert "Open budgets" in home.content.decode() or "Budgets" in home.content.decode()
    assert reverse("budgets") in home.content.decode()


@pytest.mark.django_db
def test_add_edit_archive_and_reset_confirmation():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    client = signed_in(owner)
    add = client.post(
        reverse("budgets") + "?month=2026-10",
        {
            "scope": "private",
            "category": str(groceries.pk),
            "amount": "40.00",
            "effective_month": "2026-10",
            "month": "2026-10",
        },
    )
    assert add.status_code == 302
    budget = Budget.objects.get(owner=owner, category=groceries)
    edit = client.post(
        reverse("budget-edit", args=[budget.pk]),
        {
            "amount": "50.00",
            "effective_month": "2026-11",
            "month": "2026-10",
        },
    )
    assert edit.status_code == 302
    assert amount_for(budget, date(2026, 10, 1)) == 4000
    assert amount_for(budget, date(2026, 11, 1)) == 5000
    client.post(
        reverse("budget-rollover-toggle", args=[budget.pk]),
        {"month": "2026-10", "enabled": "1"},
    )
    confirm = client.get(reverse("budget-rollover-reset", args=[budget.pk]) + "?month=2026-10")
    assert confirm.status_code == 200
    assert "Reset rollover" in confirm.content.decode()
    client.post(reverse("budget-rollover-reset", args=[budget.pk]), {"month": "2026-10"})
    client.post(reverse("budget-archive", args=[budget.pk]), {"month": "2026-10"})
    budget.refresh_from_db()
    assert budget.status == Budget.Status.ARCHIVED


@pytest.mark.django_db
def test_resetting_the_same_month_again_counts_in_the_new_rollover_period():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 1, 1), rollover=True)
    reset_budget_rollover(owner.user, budget, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, False, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, True, month=date(2026, 3, 1))
    budget.refresh_from_db()

    reset_budget_rollover(owner.user, budget, month=date(2026, 6, 1))

    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 7, 1))}
    # The June reset is part of the current period again: only June's leftover carries.
    assert cards[budget.pk].carry_minor == 10_000
    assert BudgetRolloverReset.objects.filter(budget=budget, month=date(2026, 6, 1)).count() == 1


@pytest.mark.django_db
def test_reset_and_re_enabling_in_the_same_clock_tick_starts_a_clean_period(monkeypatch):
    from django.utils import timezone as django_timezone

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 1, 1), rollover=True)
    frozen = django_timezone.now()
    monkeypatch.setattr("finance.budget_services.timezone.now", lambda: frozen)
    monkeypatch.setattr("django.utils.timezone.now", lambda: frozen)
    reset_budget_rollover(owner.user, budget, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, False, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, True, month=date(2026, 3, 1))
    budget.refresh_from_db()

    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 7, 1))}

    assert cards[budget.pk].carry_minor == 40_000


@pytest.mark.django_db
def test_reset_made_in_the_same_tick_after_re_enabling_counts_in_the_new_period(monkeypatch):
    from django.utils import timezone as django_timezone

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    budget = add_budget(owner, category=groceries, amount_minor=10_000, month=date(2026, 1, 1), rollover=True)
    frozen = django_timezone.now()
    monkeypatch.setattr("finance.budget_services.timezone.now", lambda: frozen)
    monkeypatch.setattr("django.utils.timezone.now", lambda: frozen)
    reset_budget_rollover(owner.user, budget, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, False, month=date(2026, 6, 1))
    set_budget_rollover(owner.user, budget, True, month=date(2026, 3, 1))
    budget.refresh_from_db()
    reset_budget_rollover(owner.user, budget, month=date(2026, 6, 1))

    cards = {card.budget.pk: card for card in month_budget_cards(owner.user, date(2026, 7, 1))}

    # The new June reset counts: only June's leftover carries into July.
    assert cards[budget.pk].carry_minor == 10_000
