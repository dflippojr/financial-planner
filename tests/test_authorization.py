import pytest
from django.contrib.auth import get_user_model

from finance.models import Account, Household, Membership, Person


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password="Synthetic-passphrase-42!")
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


@pytest.mark.django_db
def test_account_visibility_includes_own_private_and_current_household_accounts():
    viewer = make_person("viewer")
    other = make_person("other")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=viewer, household=household)
    Membership.objects.create(person=other, household=household)
    own_private = Account.objects.create(name="Viewer Private", account_type="checking", owner=viewer)
    other_private = Account.objects.create(name="Other Private", account_type="checking", owner=other)
    shared = Account.objects.create(
        name="Shared",
        account_type="checking",
        owner=other,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )

    visible = set(Account.objects.visible_to(viewer.user))

    assert visible == {own_private, shared}
    assert other_private not in visible


@pytest.mark.django_db
def test_account_visibility_excludes_ended_household_membership():
    viewer = make_person("former-member")
    owner = make_person("owner")
    household = Household.objects.create(name="Synthetic Household")
    membership = Membership.objects.create(person=viewer, household=household)
    membership.ended_at = membership.joined_at
    membership.save(update_fields=("ended_at",))
    shared = Account.objects.create(
        name="Shared",
        account_type="checking",
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )

    assert not Account.objects.visible_to(viewer).filter(pk=shared.pk).exists()


@pytest.mark.django_db
def test_anonymous_account_visibility_is_empty():
    owner = make_person("owner")
    Account.objects.create(name="Private", account_type="checking", owner=owner)

    assert not Account.objects.visible_to(None).exists()
