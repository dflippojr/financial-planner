import io
import json
import zipfile

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from tests.helpers import stamp_recent_auth

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
from finance.export import write_export_zip
from finance.auth_services import create_invitation


PASSWORD = "Synthetic-passphrase-42!"
SYNTHETIC_SETUP_CODE = "synthetic-setup-code-53"


def make_member(username, household=None):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    if household is None:
        household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user, person, household


@pytest.mark.django_db
def test_publish_policy_collision_returns_the_winner(monkeypatch):
    winner = PrivacyPolicyVersion.objects.create(
        version=1,
        body="Synthetic concurrent first publish",
        is_material=True,
        published_at=timezone.now(),
    )

    class EmptyLock:
        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    monkeypatch.setattr(
        PrivacyPolicyVersion.objects,
        "select_for_update",
        lambda *args, **kwargs: EmptyLock(),
    )

    result = publish_policy(material=True, body="Synthetic losing first publish")

    assert result.pk == winner.pk
    assert result.body == "Synthetic concurrent first publish"
    assert PrivacyPolicyVersion.objects.count() == 1


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


def _setup_form(**overrides):
    data = {
        "setup_code": SYNTHETIC_SETUP_CODE,
        "username": "first-member",
        "display_name": "First Example",
        "household_name": "Synthetic Household",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_policy_page_is_public_and_shows_version_and_date():
    response = Client().get(reverse("privacy-policy"))

    policy = PrivacyPolicyVersion.objects.get()
    assert response.status_code == 200
    assert b"Privacy and data policy" in response.content
    assert f"Version {policy.version}".encode() in response.content
    assert policy.published_at.date().isoformat().encode() in response.content


@pytest.mark.django_db
def test_sign_in_and_join_pages_link_to_the_policy():
    make_member("owner")
    login_page = Client().get(reverse("login"))
    join_page = Client().get(reverse("join"))

    assert reverse("privacy-policy").encode() in login_page.content
    assert b"Privacy and data policy" in join_page.content


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_presents_the_policy_on_get():
    shown = Client().get(reverse("setup"))
    assert b"Privacy and data policy" in shown.content


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_without_accepting_still_finishes():
    client = Client()
    client.post(reverse("setup"), _setup_form())
    user = get_user_model().objects.get()
    assert PrivacyPolicyAcceptance.objects.count() == 0
    assert client.get(reverse("home")).status_code == 200
    assert not may_use_ai(user.person)


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_with_acceptance_records_a_row():
    client = Client()
    client.post(reverse("setup"), _setup_form(accept_privacy_policy="on"))
    user = get_user_model().objects.get()
    row = PrivacyPolicyAcceptance.objects.get(person=user.person)
    assert row.policy_version == current_policy()
    assert may_use_ai(user.person)


@pytest.mark.django_db
def test_join_records_optional_acceptance():
    inviter, person, household = make_member("owner")
    client = Client()
    client.force_login(inviter)
    stamp_recent_auth(client)
    code = create_invitation(person)
    joined = Client().post(
        reverse("join"),
        {
            "invitation_code": code,
            "username": "new-member",
            "display_name": "New Example",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "accept_privacy_policy": "on",
        },
    )
    new_user = get_user_model().objects.get(username="new-member")
    assert joined.status_code == 200
    assert PrivacyPolicyAcceptance.objects.filter(person=new_user.person).exists()
    assert Membership.objects.filter(person=new_user.person, household=household, ended_at__isnull=True).exists()


@pytest.mark.django_db
def test_settings_shows_state_and_accepts_current_version():
    user, person, _household = make_member("owner")
    publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    page = client.get(reverse("account-settings"))
    assert b"not in acceptance" in page.content
    accepted = client.post(reverse("account-settings"), {"action": "accept-privacy-policy"})
    assert accepted.status_code == 200
    assert in_acceptance(person)
    assert b"You are in acceptance" in accepted.content


@pytest.mark.django_db
def test_existing_member_prompt_until_respond_then_ai_still_off_if_declined():
    user, person, _household = make_member("owner")
    publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)
    home = client.get(reverse("home"))
    assert b"needs a response before you can use AI" in home.content
    client.post(reverse("privacy-policy-respond"), {"action": "decline", "next": reverse("home")})
    home_after = client.get(reverse("home"))
    assert b"needs a response before you can use AI" not in home_after.content
    assert not may_use_ai(person)


@pytest.mark.django_db
def test_acceptance_rows_are_exported():
    _user, person, _household = make_member("owner")
    version = publish_policy(material=True, body="Synthetic policy v1")
    accept_policy(person, version)
    archive = zipfile.ZipFile(io.BytesIO(write_export_zip(person)))
    rows = json.loads(archive.read("privacy_policy_acceptances.json").decode())
    assert rows == [
        {
            "id": PrivacyPolicyAcceptance.objects.get().pk,
            "policy_version": version.version,
            "is_material": True,
            "accepted_at": rows[0]["accepted_at"],
        }
    ]
    assert rows[0]["accepted_at"]


@pytest.mark.django_db
def test_stale_versioned_page_does_not_accept_current_policy():
    user, person, _household = make_member("owner")
    first = publish_policy(material=True, body="Synthetic policy v1")
    accept_policy(person, first)
    current = publish_policy(material=True, body="Synthetic policy v2")
    client = Client()
    client.force_login(user)

    shown = client.get(reverse("privacy-policy-version", args=(first.version,)))
    assert shown.status_code == 200
    assert b"Accept this version" not in shown.content
    assert b"Read the current version" in shown.content

    posted = client.post(
        reverse("privacy-policy-respond"),
        {
            "action": "accept",
            "version": str(first.version),
            "next": reverse("account-settings"),
        },
    )

    assert not PrivacyPolicyAcceptance.objects.filter(person=person, policy_version=current).exists()
    assert posted.status_code == 302
    assert posted.url == reverse("privacy-policy")


@pytest.mark.django_db
def test_accept_from_current_policy_page_records_the_shown_version():
    user, person, _household = make_member("owner")
    current = publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)

    shown = client.get(reverse("privacy-policy"))
    assert b"Accept this version" in shown.content
    assert f'name="version" value="{current.version}"'.encode() in shown.content

    posted = client.post(
        reverse("privacy-policy-respond"),
        {
            "action": "accept",
            "version": str(current.version),
            "next": reverse("account-settings"),
        },
    )

    assert posted.status_code == 302
    assert PrivacyPolicyAcceptance.objects.filter(person=person, policy_version=current).exists()

