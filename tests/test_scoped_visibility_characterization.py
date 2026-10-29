"""Pin the private-or-household visibility rule shared by four models (#585).

Each row carries a literal label so the expected sets name the rows a viewer
should and should not see, rather than recomputing the rule.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from finance.models import (
    Account,
    Budget,
    Category,
    Household,
    Membership,
    Person,
    PlannedItem,
    SavingsGoal,
)


@dataclass
class RowFactory:
    model: type
    create: Callable


def _archived(inactive):
    if inactive:
        return {"status": "archived", "archived_at": timezone.now()}
    return {}


def _account(world, label, owner, household, inactive):
    shared = {"scope": "household", "household": household, "share_mode": "co_owned"} if household else {}
    return Account.objects.create(name=label, account_type="checking", owner=owner, **shared, **_archived(inactive))


def _planned_item(world, label, owner, household, inactive):
    shared = {"scope": "household", "household": household} if household else {}
    return PlannedItem.objects.create(
        name=label,
        owner=owner,
        kind="expense",
        amount_minor=1_250,
        start_date=date(2026, 1, 1),
        cadence="monthly",
        enabled=not inactive,
        **shared,
    )


def _savings_goal(world, label, owner, household, inactive):
    shared = {"scope": "household", "household": household} if household else {}
    return SavingsGoal.objects.create(
        name=label, owner=owner, target_amount_minor=50_000, **shared, **_archived(inactive)
    )


def _budget(world, label, owner, household, inactive):
    # A category per row keeps the one-active-budget constraints out of the way;
    # visibility never looks at it.
    category = Category.objects.create(household=world.catalog, name=label)
    shared = {"scope": "household", "household": household} if household else {}
    return Budget.objects.create(owner=owner, category=category, **shared, **_archived(inactive))


FACTORIES = [
    pytest.param(RowFactory(Account, _account), id="account"),
    pytest.param(RowFactory(PlannedItem, _planned_item), id="planned-item"),
    pytest.param(RowFactory(SavingsGoal, _savings_goal), id="savings-goal"),
    pytest.param(RowFactory(Budget, _budget), id="budget"),
]


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password="Synthetic-passphrase-42!")
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def build_world(factory):
    viewer = make_person("viewer")
    member = make_person("member")
    stranger = make_person("stranger")
    home = Household.objects.create(name="Synthetic Home")
    away = Household.objects.create(name="Synthetic Elsewhere")
    catalog = Household.objects.create(name="Synthetic Category Catalog")
    memberships = {
        "viewer": Membership.objects.create(person=viewer, household=home),
        "member": Membership.objects.create(person=member, household=home),
        "stranger": Membership.objects.create(person=stranger, household=away),
    }
    world = SimpleNamespace(
        viewer=viewer, member=member, stranger=stranger, home=home, away=away, catalog=catalog,
        memberships=memberships, rows={},
    )
    specs = {
        "viewer-private": (viewer, None, False),
        "viewer-private-inactive": (viewer, None, True),
        "viewer-shared-home": (viewer, home, False),
        "member-private": (member, None, False),
        "member-shared-home": (member, home, False),
        "member-shared-home-inactive": (member, home, True),
        "stranger-private": (stranger, None, False),
        "stranger-shared-away": (stranger, away, False),
        "viewer-shared-away": (viewer, away, False),
    }
    for label, (owner, household, inactive) in specs.items():
        world.rows[label] = factory.create(world, label, owner, household, inactive)
    return world


def labels(world, queryset):
    by_pk = {row.pk: label for label, row in world.rows.items()}
    pks = list(queryset.values_list("pk", flat=True))
    assert len(pks) == len(set(pks)), "visibility must not duplicate rows"
    return {by_pk[pk] for pk in pks}


ALL_LABELS = {
    "viewer-private",
    "viewer-private-inactive",
    "viewer-shared-home",
    "member-private",
    "member-shared-home",
    "member-shared-home-inactive",
    "stranger-private",
    "stranger-shared-away",
    "viewer-shared-away",
}

VIEWER_SEES = {
    "viewer-private",
    "viewer-private-inactive",
    "viewer-shared-home",
    "member-shared-home",
    "member-shared-home-inactive",
}

VIEWER_SEES_AFTER_LEAVING = {
    "viewer-private",
    "viewer-private-inactive",
}


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_viewer_sees_own_private_and_current_household_rows_only(factory):
    world = build_world(factory)

    visible = labels(world, factory.model.objects.visible_to(world.viewer))

    assert visible == VIEWER_SEES
    assert ALL_LABELS - visible == {
        "member-private",
        "stranger-private",
        "stranger-shared-away",
        "viewer-shared-away",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_person_and_their_user_see_identical_rows(factory):
    world = build_world(factory)

    by_person = labels(world, factory.model.objects.visible_to(world.viewer))
    by_user = labels(world, factory.model.objects.visible_to(world.viewer.user))

    assert by_person == by_user == VIEWER_SEES


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_other_members_see_their_own_scope(factory):
    world = build_world(factory)

    assert labels(world, factory.model.objects.visible_to(world.member)) == {
        "member-private",
        "member-shared-home",
        "member-shared-home-inactive",
        "viewer-shared-home",
    }
    assert labels(world, factory.model.objects.visible_to(world.stranger.user)) == {
        "stranger-private",
        "stranger-shared-away",
        "viewer-shared-away",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_principals_without_a_person_see_nothing(factory):
    world = build_world(factory)
    personless = get_user_model().objects.create_user(username="no-person", password="Synthetic-passphrase-42!")

    for principal in (None, AnonymousUser(), personless):
        assert not factory.model.objects.visible_to(principal).exists()
    assert labels(world, factory.model.objects.all()) == ALL_LABELS


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_ending_membership_revokes_shared_rows_on_a_fresh_query(factory):
    world = build_world(factory)
    assert labels(world, factory.model.objects.visible_to(world.viewer)) == VIEWER_SEES

    membership = world.memberships["viewer"]
    membership.ended_at = timezone.now()
    membership.save(update_fields=("ended_at",))

    assert labels(world, factory.model.objects.visible_to(world.viewer)) == VIEWER_SEES_AFTER_LEAVING
    assert labels(world, factory.model.objects.visible_to(world.viewer.user)) == VIEWER_SEES_AFTER_LEAVING
    assert labels(world, factory.model.objects.visible_to(world.member)) == {
        "member-private",
        "member-shared-home",
        "member-shared-home-inactive",
        "viewer-shared-home",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_rejoining_another_household_shows_only_that_household(factory):
    world = build_world(factory)
    membership = world.memberships["viewer"]
    membership.ended_at = timezone.now()
    membership.save(update_fields=("ended_at",))
    Membership.objects.create(person=world.viewer, household=world.away)

    assert labels(world, factory.model.objects.visible_to(world.viewer)) == {
        "viewer-private",
        "viewer-private-inactive",
        "stranger-shared-away",
        "viewer-shared-away",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_filters_chained_before_or_after_visibility_are_kept(factory):
    world = build_world(factory)
    chosen = [world.rows[label].pk for label in ("viewer-private", "member-shared-home", "member-private")]
    manager = factory.model.objects

    assert labels(world, manager.filter(pk__in=chosen).visible_to(world.viewer)) == {
        "viewer-private",
        "member-shared-home",
    }
    assert labels(world, manager.visible_to(world.viewer).filter(pk__in=chosen)) == {
        "viewer-private",
        "member-shared-home",
    }
    assert labels(world, manager.exclude(pk=world.rows["viewer-private"].pk).visible_to(world.viewer)) == (
        VIEWER_SEES - {"viewer-private"}
    )
    assert labels(world, manager.visible_to(world.viewer).filter(owner=world.member)) == {
        "member-shared-home",
        "member-shared-home-inactive",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("factory", FACTORIES)
def test_visibility_keeps_archived_and_disabled_rows(factory):
    world = build_world(factory)

    visible = labels(world, factory.model.objects.visible_to(world.viewer))

    assert {"viewer-private-inactive", "member-shared-home-inactive"} <= visible
