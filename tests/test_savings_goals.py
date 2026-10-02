from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.models import Account, BalanceSnapshot, Household, Membership, Person, SavingsGoal
from finance.savings_goal_services import (
    goal_progress,
    months_left,
    save_savings_goal,
    set_savings_goal_archived,
    set_savings_goal_completed,
)

PASSWORD = "Synthetic-passphrase-42!"
SECRET_ACCOUNT = "Owner Secret Vault"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, *, name="Synthetic Savings", scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.SAVINGS,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def add_snapshot(account, snapshot_date, amount_minor, source=BalanceSnapshot.Source.MANUAL):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=source,
    )


def make_goal(owner, *, name="Vacation", target_amount_minor=100_000, target_date=date(2026, 12, 31), **kwargs):
    return SavingsGoal.objects.create(
        owner=owner,
        name=name,
        target_amount_minor=target_amount_minor,
        target_date=target_date,
        **kwargs,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


# -- months_left, the issue owner's pinned formula --


def test_months_left_worked_examples():
    today = date(2026, 10, 2)
    assert months_left(today, date(2026, 12, 15)) == 3
    assert months_left(today, date(2026, 12, 1)) == 2
    assert months_left(today, date(2026, 10, 31)) == 1


def test_months_left_is_at_least_one_when_target_is_today():
    today = date(2026, 10, 2)
    assert months_left(today, today) == 1


# -- progress math --


@pytest.mark.django_db
def test_progress_uses_linked_account_latest_snapshot():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_snapshot(account, date(2026, 1, 1), 40_000)
    add_snapshot(account, date(2026, 2, 1), 60_000)
    goal = make_goal(owner, target_amount_minor=100_000, linked_account=account)

    progress = goal_progress(owner.user, goal, today=date(2026, 2, 15))

    assert progress.source == "snapshot"
    assert progress.current_amount_minor == 60_000
    assert progress.as_of == date(2026, 2, 1)
    assert progress.percent == 60
    assert progress.remaining_minor == 40_000


@pytest.mark.django_db
def test_progress_falls_back_to_manual_when_no_linked_account():
    owner = make_person("owner")
    make_household(owner)
    goal = make_goal(
        owner,
        target_amount_minor=100_000,
        manual_amount_minor=25_000,
        manual_amount_date=date(2026, 1, 15),
    )

    progress = goal_progress(owner.user, goal, today=date(2026, 2, 1))

    assert progress.source == "manual"
    assert progress.current_amount_minor == 25_000
    assert progress.as_of == date(2026, 1, 15)


@pytest.mark.django_db
def test_progress_falls_back_to_manual_when_linked_account_has_no_snapshot():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    goal = make_goal(
        owner,
        target_amount_minor=100_000,
        linked_account=account,
        manual_amount_minor=10_000,
        manual_amount_date=date(2026, 1, 1),
    )

    progress = goal_progress(owner.user, goal, today=date(2026, 2, 1))

    assert progress.source == "manual"
    assert progress.current_amount_minor == 10_000


@pytest.mark.django_db
def test_monthly_amount_needed_rounds_up_to_the_cent():
    owner = make_person("owner")
    make_household(owner)
    goal = make_goal(
        owner,
        target_amount_minor=100_000,
        target_date=date(2026, 12, 15),
        manual_amount_minor=1_000,
        manual_amount_date=date(2026, 10, 1),
    )

    progress = goal_progress(owner.user, goal, today=date(2026, 10, 2))

    # remaining = 99000 cents over 3 months = 33000.0 exactly -> no rounding needed, check a case that does round up
    assert progress.monthly_needed_minor == 33_000

    goal.manual_amount_minor = 1
    goal.save()
    progress = goal_progress(owner.user, goal, today=date(2026, 10, 2))
    remaining = 100_000 - 1
    expected = -(-remaining // 3)
    assert progress.monthly_needed_minor == expected
    assert expected == 33_333  # 99999 / 3 = 33333.0 exactly; bump remainder to force a round-up
    goal.manual_amount_minor = 0
    goal.manual_amount_date = date(2026, 10, 1)
    goal.save()
    progress = goal_progress(owner.user, goal, today=date(2026, 10, 2))
    assert progress.monthly_needed_minor == 33_334  # 100000 / 3 = 33333.33 -> rounds up to the cent


@pytest.mark.django_db
def test_goal_shown_as_past_due_when_target_date_has_passed():
    owner = make_person("owner")
    make_household(owner)
    goal = make_goal(
        owner,
        target_amount_minor=100_000,
        target_date=date(2026, 1, 1),
        manual_amount_minor=10_000,
        manual_amount_date=date(2025, 12, 1),
    )

    progress = goal_progress(owner.user, goal, today=date(2026, 2, 1))

    assert progress.past_due is True
    assert progress.reached is False
    assert progress.monthly_needed_minor is None


@pytest.mark.django_db
def test_reached_takes_precedence_over_past_due():
    owner = make_person("owner")
    make_household(owner)
    goal = make_goal(
        owner,
        target_amount_minor=100_000,
        target_date=date(2026, 1, 1),
        manual_amount_minor=150_000,
        manual_amount_date=date(2025, 12, 1),
    )

    progress = goal_progress(owner.user, goal, today=date(2026, 2, 1))

    assert progress.reached is True
    assert progress.past_due is False
    assert progress.monthly_needed_minor is None
    assert progress.remaining_minor == 0


# -- access --


@pytest.mark.django_db
def test_private_goal_visible_only_to_owner():
    owner = make_person("owner")
    outsider = make_person("outsider")
    make_household(owner)
    make_household(outsider, name="Other Household")
    goal = make_goal(owner, name=SECRET_ACCOUNT)

    assert SavingsGoal.objects.visible_to(owner.user).filter(pk=goal.pk).exists()
    assert not SavingsGoal.objects.visible_to(outsider.user).filter(pk=goal.pk).exists()


@pytest.mark.django_db
def test_household_goal_visible_to_current_members_only():
    owner = make_person("owner")
    member = make_person("member")
    former_member = make_person("former")
    household = make_household(owner, member)
    membership = Membership.objects.create(person=former_member, household=household)
    membership.ended_at = timezone.now()
    membership.save()
    goal = make_goal(owner, scope=SavingsGoal.Scope.HOUSEHOLD, household=household)

    assert SavingsGoal.objects.visible_to(owner.user).filter(pk=goal.pk).exists()
    assert SavingsGoal.objects.visible_to(member.user).filter(pk=goal.pk).exists()
    assert not SavingsGoal.objects.visible_to(former_member.user).filter(pk=goal.pk).exists()


@pytest.mark.django_db
def test_linked_account_that_becomes_private_falls_back_without_leaking():
    owner = make_person("owner")
    viewer = make_person("viewer")
    household = make_household(owner, viewer)
    shared_account = make_account(owner, name=SECRET_ACCOUNT, scope=Account.Scope.HOUSEHOLD, household=household)
    add_snapshot(shared_account, date(2026, 1, 1), 90_000)
    goal = make_goal(
        owner,
        scope=SavingsGoal.Scope.HOUSEHOLD,
        household=household,
        linked_account=shared_account,
        manual_amount_minor=5_000,
        manual_amount_date=date(2025, 12, 1),
    )

    before = goal_progress(viewer.user, goal, today=date(2026, 2, 1))
    assert before.source == "snapshot"
    assert before.current_amount_minor == 90_000

    # The account becomes private to its owner, so the viewer can no longer see it.
    shared_account.scope = Account.Scope.PRIVATE
    shared_account.household = None
    shared_account.share_mode = ""
    shared_account.save()

    after = goal_progress(viewer.user, goal, today=date(2026, 2, 1))
    assert after.source == "manual"
    assert after.current_amount_minor == 5_000
    assert after.linked_account is None

    owner_view = goal_progress(owner.user, goal, today=date(2026, 2, 1))
    assert owner_view.source == "snapshot"
    assert owner_view.current_amount_minor == 90_000


# -- save/edit/complete/archive permissions --


@pytest.mark.django_db
def test_non_owner_cannot_edit_a_private_goal():
    owner = make_person("owner")
    other = make_person("other")
    make_household(owner)
    make_household(other, name="Other Household")
    goal = make_goal(owner)
    payload = {
        "scope": SavingsGoal.Scope.PRIVATE,
        "name": "Hijacked",
        "target_amount_minor": 1,
        "target_date": date(2026, 1, 1),
        "linked_account": None,
        "manual_amount_minor": None,
        "manual_amount_date": None,
    }

    with pytest.raises(PermissionDenied):
        save_savings_goal(other.user, payload, goal=goal)


@pytest.mark.django_db
def test_household_member_can_edit_and_complete_a_household_goal():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    goal = make_goal(owner, scope=SavingsGoal.Scope.HOUSEHOLD, household=household)

    save_savings_goal(
        member.user,
        {
            "scope": SavingsGoal.Scope.HOUSEHOLD,
            "name": "Renamed by member",
            "target_amount_minor": 50_000,
            "target_date": date(2026, 6, 1),
            "linked_account": None,
            "manual_amount_minor": None,
            "manual_amount_date": None,
        },
        goal=goal,
    )
    goal.refresh_from_db()
    assert goal.name == "Renamed by member"

    set_savings_goal_completed(member.user, goal, True)
    goal.refresh_from_db()
    assert goal.completed_at is not None

    set_savings_goal_archived(member.user, goal, True)
    goal.refresh_from_db()
    assert goal.status == SavingsGoal.Status.ARCHIVED
    assert goal.archived_at is not None


@pytest.mark.django_db
def test_cannot_link_an_account_the_actor_cannot_see():
    owner = make_person("owner")
    other = make_person("other")
    make_household(owner)
    make_household(other, name="Other Household")
    other_private_account = make_account(other, name="Other Private")
    payload = {
        "scope": SavingsGoal.Scope.PRIVATE,
        "name": "Bad link",
        "target_amount_minor": 1_000,
        "target_date": date(2026, 1, 1),
        "linked_account": other_private_account,
        "manual_amount_minor": None,
        "manual_amount_date": None,
    }

    with pytest.raises(PermissionDenied):
        save_savings_goal(owner.user, payload)


# -- view-level smoke tests --


@pytest.mark.django_db
def test_goals_page_shows_only_visible_goals_and_supports_actions():
    owner = make_person("owner")
    outsider = make_person("outsider")
    make_household(owner)
    make_household(outsider, name="Other Household")
    goal = make_goal(owner, name=SECRET_ACCOUNT)

    owner_client = signed_in(owner)
    outsider_client = signed_in(outsider)

    owner_page = owner_client.get(reverse("savings-goals"))
    outsider_page = outsider_client.get(reverse("savings-goals"))
    assert SECRET_ACCOUNT in owner_page.content.decode()
    assert SECRET_ACCOUNT not in outsider_page.content.decode()

    complete = owner_client.post(reverse("savings-goal-complete", args=[goal.pk]))
    assert complete.status_code == 302
    goal.refresh_from_db()
    assert goal.completed_at is not None

    missing = outsider_client.post(reverse("savings-goal-complete", args=[goal.pk]))
    assert missing.status_code == 404


@pytest.mark.django_db
def test_add_goal_via_the_page():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)

    response = client.post(
        reverse("savings-goals"),
        {
            "name": "Emergency fund",
            "target_amount": "500.00",
            "target_date": "2026-12-31",
            "scope": "private",
            "linked_account": "",
            "manual_amount": "100.00",
            "manual_amount_date": "2026-01-01",
        },
    )
    assert response.status_code == 302
    goal = SavingsGoal.objects.get(name="Emergency fund")
    assert goal.target_amount_minor == 50_000
    assert goal.manual_amount_minor == 10_000
