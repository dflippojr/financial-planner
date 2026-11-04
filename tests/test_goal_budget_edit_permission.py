"""Who may edit a savings goal or a budget, through every service that checks it.

The same rule guards both models: the owner edits a private row, and a current
member of the row's household edits a household row. Everyone else is refused
and the row stays as it was.

The audit layer separately refuses an actor outside a row's audience, and its
refusal rolls the write back, so a missing edit check would still look like a
refusal. Each refused case therefore also asserts that it never reached the
audit layer: the edit check, not that backstop, has to be what stops it.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from finance import audit_services, budget_services, savings_goal_services
from finance.budget_services import (
    reset_budget_rollover,
    save_budget,
    set_budget_archived,
    set_budget_rollover,
)
from finance.models import Budget, BudgetRolloverReset, Household, Membership, Person, SavingsGoal
from finance.savings_goal_services import (
    save_savings_goal,
    set_savings_goal_archived,
    set_savings_goal_completed,
)

MONTH = date(2026, 1, 1)
PRIVATE = "private"
HOUSEHOLD = "household"

OWNER = "owner"
OWNER_WHO_LEFT = "owner_who_left"
MEMBER = "other_member"
FORMER_MEMBER = "former_member"
NON_MEMBER = "non_member"
OTHER_HOUSEHOLD_MEMBER = "other_household_member"

# Actor, scope of the row, may they edit it?
MATRIX = [
    (OWNER, PRIVATE, True),
    (OWNER_WHO_LEFT, PRIVATE, True),
    (MEMBER, PRIVATE, False),
    (FORMER_MEMBER, PRIVATE, False),
    (NON_MEMBER, PRIVATE, False),
    (OTHER_HOUSEHOLD_MEMBER, PRIVATE, False),
    (OWNER, HOUSEHOLD, True),
    (OWNER_WHO_LEFT, HOUSEHOLD, False),
    (MEMBER, HOUSEHOLD, True),
    (FORMER_MEMBER, HOUSEHOLD, False),
    (NON_MEMBER, HOUSEHOLD, False),
    (OTHER_HOUSEHOLD_MEMBER, HOUSEHOLD, False),
]


@pytest.fixture
def audit_record(monkeypatch):
    spy = Mock(wraps=audit_services.record)
    monkeypatch.setattr(budget_services, "record", spy)
    monkeypatch.setattr(savings_goal_services, "record", spy)
    return spy


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password="Synthetic-passphrase-42!")
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def end_membership(membership):
    membership.ended_at = timezone.now()
    membership.save(update_fields=("ended_at",))


def make_world(actor, scope, make_row):
    """Build the row and the people around it, with the owner's membership as the actor needs it."""
    people = {name: make_person(name) for name in (OWNER, MEMBER, FORMER_MEMBER, NON_MEMBER, OTHER_HOUSEHOLD_MEMBER)}
    household = Household.objects.create(name="Synthetic Household")
    owner_membership = Membership.objects.create(person=people[OWNER], household=household)
    Membership.objects.create(person=people[MEMBER], household=household)
    end_membership(Membership.objects.create(person=people[FORMER_MEMBER], household=household))
    other_household = Household.objects.create(name="Other Synthetic Household")
    Membership.objects.create(person=people[OTHER_HOUSEHOLD_MEMBER], household=other_household)
    row = make_row(people[OWNER], scope, household if scope == HOUSEHOLD else None)
    if actor == OWNER_WHO_LEFT:
        end_membership(owner_membership)
        return SimpleNamespace(person=people[OWNER], row=row)
    return SimpleNamespace(person=people[actor], row=row)


def make_goal(owner, scope, household):
    return SavingsGoal.objects.create(
        owner=owner, scope=scope, household=household, name="Synthetic Goal", target_amount_minor=100_000
    )


def make_budget(owner, scope, household):
    return Budget.objects.create(owner=owner, scope=scope, household=household)


def save_goal(user, goal):
    save_savings_goal(user, {"scope": goal.scope, "name": "Renamed"}, goal=goal)


def save_budget_amount(user, budget):
    save_budget(
        user,
        {"scope": budget.scope, "category": None, "amount_minor": 12_345, "effective_month": MONTH},
        budget=budget,
    )


def row_state(row):
    return type(row).objects.filter(pk=row.pk).values().get()


GOAL_ACTIONS = {
    "save": (save_goal, lambda goal: goal.name == "Renamed"),
    "complete": (lambda user, goal: set_savings_goal_completed(user, goal, True), lambda goal: goal.completed_at),
    "archive": (
        lambda user, goal: set_savings_goal_archived(user, goal, True),
        lambda goal: goal.status == SavingsGoal.Status.ARCHIVED,
    ),
}

BUDGET_ACTIONS = {
    "save": (save_budget_amount, lambda budget: budget.amounts.filter(amount_minor=12_345).exists()),
    "archive": (
        lambda user, budget: set_budget_archived(user, budget, True),
        lambda budget: budget.status == Budget.Status.ARCHIVED,
    ),
    "rollover": (
        lambda user, budget: set_budget_rollover(user, budget, True, month=MONTH),
        lambda budget: budget.rollover_enabled,
    ),
    "reset_rollover": (
        lambda user, budget: reset_budget_rollover(user, budget, month=MONTH),
        lambda budget: BudgetRolloverReset.objects.filter(budget=budget, month=MONTH).exists(),
    ),
}


def assert_edit_outcome(world, action, took_effect, allowed, audit_record, refused_with=PermissionDenied):
    row = world.row
    before = row_state(row)
    if allowed:
        action(world.person.user, row)
        row.refresh_from_db()
        assert took_effect(row)
        return
    with pytest.raises(refused_with):
        action(world.person.user, row)
    audit_record.assert_not_called()
    assert row_state(row) == before
    row.refresh_from_db()
    assert not took_effect(row)


@pytest.mark.django_db
@pytest.mark.parametrize("action_name", GOAL_ACTIONS)
@pytest.mark.parametrize(("actor", "scope", "allowed"), MATRIX)
def test_goal_edit_permission(actor, scope, allowed, action_name, audit_record):
    world = make_world(actor, scope, make_goal)
    action, took_effect = GOAL_ACTIONS[action_name]
    # Saving a household goal asks for the actor's household before the edit
    # check, so someone with none is refused by that validation instead.
    without_a_household = actor in (OWNER_WHO_LEFT, FORMER_MEMBER, NON_MEMBER)
    validation_first = action_name == "save" and scope == HOUSEHOLD and without_a_household
    refused_with = ValidationError if validation_first else PermissionDenied
    assert_edit_outcome(world, action, took_effect, allowed, audit_record, refused_with=refused_with)


@pytest.mark.django_db
@pytest.mark.parametrize("action_name", BUDGET_ACTIONS)
@pytest.mark.parametrize(("actor", "scope", "allowed"), MATRIX)
def test_budget_edit_permission(actor, scope, allowed, action_name, audit_record):
    world = make_world(actor, scope, make_budget)
    action, took_effect = BUDGET_ACTIONS[action_name]
    assert_edit_outcome(world, action, took_effect, allowed, audit_record)
