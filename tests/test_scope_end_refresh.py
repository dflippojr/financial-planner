"""Derived data follows current membership and sharing scope (#301)."""

from datetime import date

import pytest
from django.urls import reverse

from finance.budget_services import save_budget
from finance.category_services import assign_category, rename_category, split_transaction
from finance.lifecycle_services import leave_household
from finance.rule_services import save_category_rule
from finance.models import Budget, SavingsGoal, Transaction
from finance.monthly_review import store_monthly_review
from tests.test_monthly_review import (
    SEP,
    TODAY,
    make_account,
    make_household,
    make_person,
    make_transaction,
    signed_in,
)


def household_goal(owner, household, name="Synthetic shared trip"):
    return SavingsGoal.objects.create(
        owner=owner,
        scope=SavingsGoal.Scope.HOUSEHOLD,
        household=household,
        name=name,
        target_amount_minor=500_000,
        target_date=date(2026, 12, 31),
        manual_amount_minor=100_000,
        manual_amount_date=date(2026, 9, 30),
    )


@pytest.mark.django_db
def test_leaving_drops_household_goal_and_budget_from_stored_review():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    pantry = household.categories.get(name="Groceries")
    rename_category(owner, pantry.pk, "Synthetic Shared Pantry")
    save_budget(
        owner.user,
        {"scope": Budget.Scope.HOUSEHOLD, "category": pantry, "amount_minor": 20_000, "effective_month": SEP},
    )
    household_goal(owner, household)
    review, _ = store_monthly_review(member, SEP, today=TODAY)
    assert review.facts["largest_remaining"]["name"] == "Synthetic Shared Pantry"
    assert [row["name"] for row in review.facts["savings_goals"]] == ["Synthetic shared trip"]

    leave_household(member)

    body = signed_in(member).get(reverse("monthly-review") + "?month=2026-09").content.decode()
    assert "Synthetic Shared Pantry" not in body
    assert "Synthetic shared trip" not in body


@pytest.mark.django_db
def test_goal_made_private_leaves_other_members_review_and_unchanged_state_hits_cache():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    goal = household_goal(owner, household)
    first, wrote = store_monthly_review(member, SEP, today=TODAY)
    assert wrote
    assert [row["name"] for row in first.facts["savings_goals"]] == ["Synthetic shared trip"]
    _, wrote = store_monthly_review(member, SEP, today=TODAY)
    assert not wrote

    goal.scope = SavingsGoal.Scope.PRIVATE
    goal.household = None
    goal.save()

    review, wrote = store_monthly_review(member, SEP, today=TODAY)
    assert wrote
    assert review.facts["savings_goals"] == []
    body = signed_in(member).get(reverse("monthly-review") + "?month=2026-09").content.decode()
    assert "Synthetic shared trip" not in body


@pytest.mark.django_db
def test_former_member_never_sees_household_category_names_after_rename():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    rename_category(owner, groceries.pk, "Synthetic Old Pantry")
    private = make_account(member, name="Member Private")
    whole = make_transaction(member, private, amount_minor=-1_000, description="Synthetic whole row")
    split = make_transaction(member, private, amount_minor=-2_000, description="Synthetic split row")
    assign_category(member, whole.pk, groceries.pk)
    split_transaction(member, split.pk, [
        {"category_id": groceries.pk, "amount_minor": -1_200},
        {"category_id": dining.pk, "amount_minor": -800},
    ])
    save_budget(
        member.user,
        {"scope": Budget.Scope.PRIVATE, "category": groceries, "amount_minor": 30_000, "effective_month": SEP},
    )
    owner_account = make_account(owner, name="Owner Private")
    owner_row = make_transaction(owner, owner_account, amount_minor=-500, description="Synthetic owner row")
    assign_category(owner, owner_row.pk, groceries.pk)

    leave_household(member)
    rename_category(owner, groceries.pk, "Synthetic New Pantry")

    client = signed_in(member)
    budget = Budget.objects.get(owner=member)
    pages = [
        client.get(reverse("transaction-list") + "?date_from=2026-09-01&date_to=2026-09-30"),
        client.get(reverse("budgets") + "?month=2026-09"),
        client.get(reverse("budget-rollover-reset", args=[budget.pk]) + "?month=2026-09"),
    ]
    for page in pages:
        assert page.status_code == 200
        body = page.content.decode()
        assert "Synthetic Old Pantry" not in body
        assert "Synthetic New Pantry" not in body
    assert "Synthetic whole row" in pages[0].content.decode()
    assert "Uncategorized" in pages[0].content.decode()

    owner_body = signed_in(owner).get(
        reverse("transaction-list") + "?date_from=2026-09-01&date_to=2026-09-30"
    ).content.decode()
    assert "Synthetic New Pantry" in owner_body
    assert "Synthetic whole row" not in owner_body


@pytest.mark.django_db
def test_rule_preview_hides_category_from_a_household_the_member_left():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    rename_category(owner, groceries.pk, "Synthetic Old Pantry")
    private = make_account(member, name="Member Private")
    row = make_transaction(member, private, amount_minor=-1_000, description="SYNTHETIC CANARY STORE")
    assign_category(member, row.pk, groceries.pk)
    Transaction.objects.filter(pk=row.pk).update(category_source=Transaction.CategorySource.RULE)

    leave_household(member)
    rename_category(owner, groceries.pk, "Synthetic New Pantry")
    new_household = make_household(member, name="Synthetic Second Household")
    rule = save_category_rule(
        member,
        owner_kind="personal",
        description_contains="SYNTHETIC CANARY",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=new_household.categories.get(name="Dining").pk,
        priority=1,
    )

    page = signed_in(member).get(reverse("category-rule-detail", args=[rule.pk]))
    body = page.content.decode()
    assert page.status_code == 200
    assert "SYNTHETIC CANARY STORE" in body
    assert "Synthetic Old Pantry" not in body
    assert "Synthetic New Pantry" not in body
