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
    accept_shown_version,
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

    accept_policy(person, current_policy())

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
    assert timezone.localtime(policy.published_at).date().isoformat().encode() in response.content


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
    client.post(
        reverse("setup"),
        _setup_form(
            accept_privacy_policy="on",
            privacy_policy_version=str(current_policy().version),
        ),
    )
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
            "privacy_policy_version": str(current_policy().version),
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
    page = client.get(reverse("settings-ai"))
    assert b"not in acceptance" in page.content
    accepted = client.post(
        reverse("settings-ai"),
        {"action": "accept-privacy-policy", "version": str(current_policy().version)},
    )
    assert accepted.status_code == 200
    assert in_acceptance(person)
    assert b"You are in acceptance" in accepted.content


@pytest.mark.django_db
def test_existing_member_prompt_until_respond_then_ai_still_off_if_declined():
    user, person, _household = make_member("owner")
    current = publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)
    home = client.get(reverse("home"))
    assert b"needs a response before you can use AI" in home.content
    client.post(
        reverse("privacy-policy-respond"),
        {"action": "decline", "version": str(current.version), "next": reverse("home")},
    )
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


def _hidden_version(content, name="privacy_policy_version"):
    marker = f'name="{name}" value="'.encode()
    assert marker in content
    start = content.index(marker) + len(marker)
    return content[start : content.index(b'"', start)].decode()


@pytest.mark.django_db
def test_accept_shown_version_ignores_a_stale_render():
    _user, person, _household = make_member("owner")
    shown = publish_policy(material=True, body="Synthetic policy v1")
    later = publish_policy(material=True, body="Synthetic policy v2")

    assert not accept_shown_version(person, shown.version)
    assert not PrivacyPolicyAcceptance.objects.filter(person=person).exists()
    assert accept_shown_version(person, later.version)
    assert PrivacyPolicyAcceptance.objects.filter(person=person, policy_version=later).exists()


@pytest.mark.django_db
def test_settings_stale_shown_version_is_not_accepted():
    user, person, _household = make_member("owner")
    publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    shown = _hidden_version(client.get(reverse("settings-ai")).content, "version")
    later = publish_policy(material=True, body="Synthetic policy v2")

    posted = client.post(
        reverse("settings-ai"),
        {"action": "accept-privacy-policy", "version": shown},
    )

    assert posted.status_code == 200
    assert not PrivacyPolicyAcceptance.objects.filter(person=person).exists()
    assert f"Current version {later.version}".encode() in posted.content
    assert f'name="version" value="{later.version}"'.encode() in posted.content


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_stale_shown_version_finishes_without_acceptance():
    client = Client()
    shown = _hidden_version(client.get(reverse("setup")).content)
    publish_policy(material=True, body="Synthetic policy v2")

    created = client.post(
        reverse("setup"),
        _setup_form(accept_privacy_policy="on", privacy_policy_version=shown),
    )

    user = get_user_model().objects.get()
    assert created.status_code == 200
    assert PrivacyPolicyAcceptance.objects.filter(person=user.person).count() == 0
    assert client.get(reverse("home")).status_code == 200
    assert not in_acceptance(user.person)


@pytest.mark.django_db
def test_join_stale_shown_version_finishes_without_acceptance():
    inviter, person, household = make_member("owner")
    client = Client()
    client.force_login(inviter)
    stamp_recent_auth(client)
    code = create_invitation(person)
    join_client = Client()
    shown = _hidden_version(join_client.get(reverse("join")).content)
    publish_policy(material=True, body="Synthetic policy v2")

    joined = join_client.post(
        reverse("join"),
        {
            "invitation_code": code,
            "username": "new-member",
            "display_name": "New Example",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "accept_privacy_policy": "on",
            "privacy_policy_version": shown,
        },
    )

    new_user = get_user_model().objects.get(username="new-member")
    assert joined.status_code == 200
    assert PrivacyPolicyAcceptance.objects.filter(person=new_user.person).count() == 0
    assert Membership.objects.filter(person=new_user.person, household=household, ended_at__isnull=True).exists()


@pytest.mark.django_db
def test_google_join_stale_shown_version_finishes_without_acceptance():
    from tests.test_google_auth import _finish_google, _google_settings

    inviter, person, _household = make_member("owner")
    code = create_invitation(person)
    client = Client()
    shown = _hidden_version(client.get(reverse("join")).content)
    publish_policy(material=True, body="Synthetic policy v2")

    with _google_settings():
        start = client.post(
            reverse("join"),
            {
                "intent": "google",
                "invitation_code": code,
                "username": "joined-google",
                "display_name": "Joined Google",
                "accept_privacy_policy": "on",
                "privacy_policy_version": shown,
            },
        )
        joined = _finish_google(client, start)

    new_user = get_user_model().objects.get(username="joined-google")
    assert joined.status_code == 200
    assert PrivacyPolicyAcceptance.objects.filter(person=new_user.person).count() == 0


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_google_setup_stale_shown_version_finishes_without_acceptance():
    from tests.test_google_auth import _finish_google, _google_settings

    client = Client()
    shown = _hidden_version(client.get(reverse("setup")).content)
    publish_policy(material=True, body="Synthetic policy v2")

    with _google_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE):
        start = client.post(
            reverse("setup"),
            {
                "intent": "google",
                "setup_code": SYNTHETIC_SETUP_CODE,
                "username": "first-google",
                "display_name": "First Google",
                "household_name": "Synthetic Household",
                "accept_privacy_policy": "on",
                "privacy_policy_version": shown,
            },
        )
        created = _finish_google(client, start)

    user = get_user_model().objects.get(username="first-google")
    assert created.status_code == 200
    assert PrivacyPolicyAcceptance.objects.filter(person=user.person).count() == 0
    assert client.session.get("_auth_user_id")



@pytest.mark.django_db
def test_deny_on_a_stale_prompt_does_not_decline_the_new_version():
    user, person, _household = make_member("owner")
    shown = publish_policy(material=True, body="Synthetic policy v1")
    later = publish_policy(material=True, body="Synthetic policy v2")
    client = Client()
    client.force_login(user)

    posted = client.post(
        reverse("privacy-policy-respond"),
        {"action": "decline", "version": str(shown.version), "next": reverse("home")},
    )

    assert posted.status_code == 302
    person.refresh_from_db()
    assert person.privacy_policy_declined_version_id != later.pk


@pytest.mark.django_db
def test_deny_on_the_current_prompt_declines_that_version():
    user, person, _household = make_member("owner")
    current = publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)

    home = client.get(reverse("home"))
    assert _hidden_version(home.content, name="version") == str(current.version)

    client.post(
        reverse("privacy-policy-respond"),
        {"action": "decline", "version": str(current.version), "next": reverse("home")},
    )

    person.refresh_from_db()
    assert person.privacy_policy_declined_version_id == current.pk


@pytest.mark.django_db
def test_policy_page_rejects_post():
    publish_policy(material=True, body="Synthetic policy v1")

    response = Client().post(reverse("privacy-policy"))

    assert response.status_code == 405


@pytest.mark.django_db
def test_settings_ai_shows_one_privacy_prompt_and_can_decline():
    user, person, _household = make_member("owner")
    current = publish_policy(material=True, body="Synthetic policy v1")
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    page = client.get(reverse("settings-ai")).content.decode()
    assert "needs a response before you can use AI" not in page
    assert page.count("Privacy and data policy") == 1
    assert f"Accept version {current.version}" in page
    assert "Deny" in page
    # Other pages, including other settings tabs, keep the banner.
    assert "needs a response before you can use AI" in client.get(reverse("settings-alerts")).content.decode()
    posted = client.post(
        reverse("privacy-policy-respond"),
        {"action": "decline", "version": str(current.version), "next": reverse("settings-ai")},
    )
    assert posted.url == reverse("settings-ai")
    person.refresh_from_db()
    assert person.privacy_policy_declined_version_id == current.pk
    after = client.get(reverse("settings-ai")).content.decode()
    assert ">Deny</button>" not in after
    assert f"Accept version {current.version}" in after


@pytest.mark.django_db
@pytest.mark.parametrize(("response", "state", "banner"), [
    (None, "undecided", True), ("dismiss", "undecided", False),
    ("decline", "denied", False), ("accept", "no_backend", False),
])
def test_sidebar_consent_states_and_bar_responses(response, state, banner):
    user, person, _ = make_member("owner")
    policy = current_policy()
    client = Client()
    client.force_login(user)
    if response:
        posted = client.post(reverse("privacy-policy-respond"),
            {"action": response, "version": policy.version, "next": reverse("home")})
        assert posted.url == reverse("home")
    page = client.get(reverse("home"))
    assert page.context["ai_slot"]["state"] == state
    assert bool(page.context["privacy_policy_prompt"]) == banner
    assert not page.context["show_ai_features"]
    html = page.content.decode()
    assert "finance-chat-panel" not in html
    if state == "denied":
        assert "data-ai-slot" not in html
        assert 'id="ai-consent"' not in html
    elif state == "undecided":
        assert "Enable AI Features" in html
    else:
        assert "Finish AI setup" in html
    person.refresh_from_db()
    if response == "dismiss":
        assert person.privacy_policy_dismissed_version_id == policy.pk
        assert person.privacy_policy_declined_version_id is None


@pytest.mark.django_db
def test_pending_complete_and_solo_sidebar_states(monkeypatch):
    from finance.policy_services import ai_slot_state
    monkeypatch.setattr("finance.ai_services.member_has_ai", lambda person: True)
    user, person, household = make_member("owner")
    _other_user, other, _ = make_member("other", household)
    policy = current_policy()
    accept_policy(person, policy)
    client = Client()
    client.force_login(user)
    pending = client.get(reverse("home"))
    assert pending.context["ai_slot"]["state"] == "household_pending"
    assert "Chat with my money" in pending.content.decode()
    assert "1 of 2 members still to accept" in pending.content.decode()
    accept_policy(other, policy)
    ready = client.get(reverse("home"))
    assert ready.context["ai_slot"]["state"] == "ready"
    assert "members still to accept" not in ready.content.decode()
    Membership.objects.filter(person=other).update(ended_at=timezone.now())
    assert ai_slot_state(person)["state"] == "ready"
    Membership.objects.filter(person=person).update(ended_at=timezone.now())
    assert ai_slot_state(person)["state"] == "ready"


@pytest.mark.django_db
def test_accept_json_swaps_to_current_members_without_account_data():
    from finance.models import Account
    user, person, household = make_member("owner")
    _other_user, other, _ = make_member("other", household)
    _former_user, former, _ = make_member("former", household)
    _outside_user, outsider, _ = make_member("outsider")
    Membership.objects.filter(person=former).update(ended_at=timezone.now())
    Account.objects.create(name="Sensitive synthetic private account", owner=other,
        account_type=Account.Type.CHECKING)
    policy = current_policy()
    other.privacy_policy_declined_version = policy
    other.save()
    client = Client()
    client.force_login(user)
    reply = client.post(reverse("privacy-policy-respond"),
        {"action": "accept", "version": policy.version, "next": "/transactions/?account=7&ai=household"},
        HTTP_ACCEPT="application/json")
    assert reply.status_code == 200
    data = reply.json()
    assert data["ai_slot"]["state"] == "no_backend"
    assert data["ai_slot"]["household"]["pending"] == 1
    assert [member["id"] for member in data["ai_slot"]["household"]["members"]] == [person.pk, other.pk]
    assert [member["status"] for member in data["ai_slot"]["household"]["members"]] == ["accepted", "not_answered"]
    assert "AI is on for you" in data["dialog_html"]
    assert "Not answered yet" in data["dialog_html"]
    assert 'href="/transactions/?account=7"' in data["dialog_html"]
    for hidden in (former.display_name, outsider.display_name, "Sensitive synthetic private account", "Declined"):
        assert hidden not in reply.content.decode()
    former_client = Client()
    former_client.force_login(_former_user)
    old_page = former_client.get(reverse("home") + "?ai=household")
    assert old_page.context["ai_slot"]["household"]["members"] == []
    assert other.display_name not in old_page.content.decode()


@pytest.mark.django_db
def test_household_status_query_count_does_not_grow_with_members(django_assert_num_queries):
    from finance.policy_services import household_consent_status
    _user, person, household = make_member("owner")
    policy = current_policy()
    accept_policy(person, policy)
    for i in range(8):
        _user, other, _ = make_member(f"member{i}", household)
        if i % 2 == 0:
            accept_policy(other, policy)
    with django_assert_num_queries(2):
        status = household_consent_status(household)
    assert status["total"] == 9 and status["pending"] == 4


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["accept", "decline", "dismiss"])
def test_json_rejects_stale_policy_and_does_not_store_a_response(action):
    user, person, _ = make_member("owner")
    old = current_policy()
    publish_policy(material=True, body="Synthetic new material policy")
    client = Client()
    client.force_login(user)
    reply = client.post(reverse("privacy-policy-respond"),
        {"action": action, "version": old.version}, HTTP_ACCEPT="application/json")
    assert reply.status_code == 409
    assert reply.json()["policy_url"] == reverse("privacy-policy")
    assert not PrivacyPolicyAcceptance.objects.filter(person=person).exists()
    person.refresh_from_db()
    assert person.privacy_policy_declined_version_id is None
    assert person.privacy_policy_dismissed_version_id is None


@pytest.mark.django_db
def test_policy_json_requires_csrf_and_rejects_unknown_actions():
    user, _person, _ = make_member("owner")
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(user)
    assert protected.post(reverse("privacy-policy-respond"),
        {"action": "accept", "version": current_policy().version},
        HTTP_ACCEPT="application/json").status_code == 403
    ordinary = Client()
    ordinary.force_login(user)
    assert ordinary.post(reverse("privacy-policy-respond"),
        {"action": "unknown"}, HTTP_ACCEPT="application/json").status_code == 400


@pytest.mark.django_db
def test_new_material_policy_reprompts_dismissed_and_denied_members():
    from finance.policy_services import ai_slot_state, should_prompt_privacy_policy
    user, person, _ = make_member("owner")
    policy = current_policy()
    client = Client()
    client.force_login(user)
    for action in ("dismiss", "decline"):
        client.post(reverse("privacy-policy-respond"), {"action": action, "version": policy.version})
    person.refresh_from_db()
    assert not should_prompt_privacy_policy(person)
    publish_policy(material=False, body="Synthetic typo fix")
    assert ai_slot_state(person)["state"] == "denied"
    publish_policy(material=True, body="Synthetic new policy")
    assert should_prompt_privacy_policy(person)
    assert ai_slot_state(person)["state"] == "undecided"


@pytest.mark.django_db
def test_settings_accept_restores_slot_after_denial():
    user, person, _ = make_member("owner")
    client = Client()
    client.force_login(user)
    client.post(reverse("privacy-policy-respond"), {"action": "decline", "version": current_policy().version})
    stamp_recent_auth(client)
    client.post(reverse("settings-ai"), {"action": "accept-privacy-policy", "version": current_policy().version})
    assert client.get(reverse("home")).context["ai_slot"]["state"] == "no_backend"
    person.refresh_from_db()
    assert person.privacy_policy_declined_version_id is None


@pytest.mark.django_db(transaction=True)
def test_migration_converts_existing_not_now_declines_to_dismissals():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    before = [("finance", "0064_pending_sign_in_session")]
    after = [("finance", "0065_privacy_policy_dismissal")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    try:
        apps = executor.loader.project_state(before).apps
        User = apps.get_model("auth", "User")
        Policy = apps.get_model("finance", "PrivacyPolicyVersion")
        OldPerson = apps.get_model("finance", "Person")
        policy = Policy.objects.create(version=1, body="Synthetic migration policy", is_material=True, published_at=timezone.now())
        user = User.objects.create(username="legacy")
        old = OldPerson.objects.create(user=user, display_name="Legacy Example", privacy_policy_declined_version=policy)
        blank = OldPerson.objects.create(user=User.objects.create(username="blank"), display_name="Blank Example")
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        NewPerson = executor.loader.project_state(after).apps.get_model("finance", "Person")
        converted = NewPerson.objects.get(pk=old.pk)
        assert converted.privacy_policy_declined_version_id is None
        assert converted.privacy_policy_dismissed_version_id == policy.pk
        assert NewPerson.objects.get(pk=blank.pk).privacy_policy_dismissed_version_id is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db
def test_json_denial_uses_fresh_person_even_after_relation_was_cached():
    from django.test import RequestFactory
    from finance.views import privacy_policy_respond
    user, person, _ = make_member("owner")
    policy = current_policy()
    assert user.person.privacy_policy_declined_version_id is None
    request = RequestFactory().post(reverse("privacy-policy-respond"),
        {"action": "decline", "version": policy.version, "next": reverse("home")},
        HTTP_ACCEPT="application/json")
    request.user = user
    reply = privacy_policy_respond(request)
    data = json.loads(reply.content)
    assert data["ai_slot"]["state"] == "denied"
    assert "data-ai-slot" not in data["slot_html"]
    assert 'id="ai-consent"' not in data["dialog_html"]


@pytest.mark.django_db
def test_denial_from_an_old_tab_does_not_withdraw_append_only_acceptance():
    user, person, _ = make_member("owner")
    policy = current_policy()
    accept_policy(person, policy)
    client = Client()
    client.force_login(user)
    reply = client.post(reverse("privacy-policy-respond"),
        {"action": "decline", "version": policy.version}, HTTP_ACCEPT="application/json")
    assert reply.status_code == 200
    assert reply.json()["ai_slot"]["state"] == "no_backend"
    assert "AI is on for you" in reply.json()["dialog_html"]
    assert PrivacyPolicyAcceptance.objects.filter(person=person, policy_version=policy).exists()
