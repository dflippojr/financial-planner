from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from finance.lifecycle_services import leave_household
from finance.models import Household, Membership, Person, PrivacyPolicyAcceptance, PrivacyPolicyVersion
from finance.policy_services import (
    accept_policy,
    current_policy,
    household_ai_allowed,
    in_acceptance,
    may_use_ai,
    publish_policy,
)


PASSWORD = "Synthetic-passphrase-42!"


def make_member(username, household=None):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    if household is None:
        household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user, person, household


@pytest.mark.django_db
def test_non_material_publish_leaves_acceptance_and_ai_guards_true():
    _user, person, household = make_member("owner")
    first = publish_policy(material=True, body="Synthetic policy v1")
    accept_policy(person, first)

    second = publish_policy(material=False, body="Synthetic policy v2 typo fix")

    assert second.version == 2
    assert not second.is_material
    assert in_acceptance(person)
    assert may_use_ai(person)
    assert household_ai_allowed(household)


@pytest.mark.django_db
def test_material_publish_takes_members_out_of_acceptance_until_they_accept():
    _user, person, household = make_member("owner")
    first = publish_policy(material=True, body="Synthetic policy v1")
    accept_policy(person, first)
    publish_policy(material=True, body="Synthetic policy v2 material")

    assert not in_acceptance(person)
    assert not may_use_ai(person)
    assert not household_ai_allowed(household)

    accept_policy(person)

    assert in_acceptance(person)
    assert may_use_ai(person)
    assert household_ai_allowed(household)


@pytest.mark.django_db
def test_member_without_acceptance_cannot_use_ai():
    _user, person, household = make_member("owner")
    publish_policy(material=True, body="Synthetic policy v1")

    assert not may_use_ai(person)
    assert not household_ai_allowed(household)


@pytest.mark.django_db
def test_household_shared_ai_refused_until_every_current_member_accepts():
    _owner_user, owner, household = make_member("owner")
    _member_user, member, _ = make_member("member", household=household)
    first = publish_policy(material=True, body="Synthetic policy v1")
    accept_policy(owner, first)

    assert may_use_ai(owner)
    assert not may_use_ai(member)
    assert not household_ai_allowed(household)

    accept_policy(member, first)

    assert household_ai_allowed(household)


@pytest.mark.django_db
def test_join_and_leave_update_household_ai_allowed():
    _owner_user, owner, household = make_member("owner")
    first = publish_policy(material=True, body="Synthetic policy v1")
    accept_policy(owner, first)
    assert household_ai_allowed(household)

    _joiner_user, joiner, _ = make_member("joiner", household=household)
    assert not household_ai_allowed(household)

    accept_policy(joiner, first)
    assert household_ai_allowed(household)

    leave_household(joiner)
    assert Membership.objects.filter(person=joiner, ended_at__isnull=True).count() == 0
    assert household_ai_allowed(household)

    _new_user, _new_member, _ = make_member("newcomer", household=household)
    assert not household_ai_allowed(household)


@pytest.mark.django_db
def test_publish_command_reads_override_file(tmp_path):
    source = tmp_path / "privacy-policy.md"
    source.write_text("Synthetic operator policy", encoding="utf-8")

    with override_settings(PRIVACY_POLICY_PATH=str(source)):
        call_command("publish_privacy_policy", "--material")
        call_command("publish_privacy_policy")

    versions = list(PrivacyPolicyVersion.objects.order_by("version"))
    assert [row.version for row in versions] == [1, 2]
    assert versions[0].is_material
    assert not versions[1].is_material
    assert versions[1].body == "Synthetic operator policy"


@pytest.mark.django_db
def test_accepting_a_later_non_material_version_counts_as_in_acceptance():
    _user, person, _household = make_member("owner")
    publish_policy(material=True, body="Synthetic v1")
    later = publish_policy(material=False, body="Synthetic v2")
    accept_policy(person, later)

    assert in_acceptance(person)
    assert may_use_ai(person)


@pytest.mark.django_db
def test_current_policy_seeds_default_text_once():
    first = current_policy()
    second = current_policy()

    assert first.pk == second.pk
    assert first.version == 1
    assert first.is_material
    assert "Outside AI providers" in first.body
    assert PrivacyPolicyAcceptance.objects.count() == 0
