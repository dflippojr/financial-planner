import io
import json
import zipfile
from dataclasses import dataclass
from typing import Callable

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


# Each step is ("publish", is_material), ("accept", member, version number or "current"),
# ("join", member), ("leave", member) or ("expect", household_ai_allowed, {member: accepted}).
# A member who is accepted is in acceptance and may use AI; one who is not is neither.
POLICY_RULE_CASES = {
    "non-material publish keeps acceptance": [
        ("publish", True),
        ("accept", "owner", 1),
        ("publish", False),
        ("expect", True, {"owner": True}),
    ],
    "material publish needs a new acceptance": [
        ("publish", True),
        ("accept", "owner", 1),
        ("publish", True),
        ("expect", False, {"owner": False}),
        ("accept", "owner", "current"),
        ("expect", True, {"owner": True}),
    ],
    "member without acceptance": [
        ("publish", True),
        ("expect", False, {"owner": False}),
    ],
    "household needs every member": [
        ("join", "member"),
        ("publish", True),
        ("accept", "owner", 1),
        ("expect", False, {"owner": True, "member": False}),
        ("accept", "member", 1),
        ("expect", True, {"owner": True, "member": True}),
    ],
    "join and leave": [
        ("publish", True),
        ("accept", "owner", 1),
        ("expect", True, {"owner": True}),
        ("join", "joiner"),
        ("expect", False, {"owner": True, "joiner": False}),
        ("accept", "joiner", 1),
        ("expect", True, {"owner": True, "joiner": True}),
        ("leave", "joiner"),
        ("expect", True, {"owner": True}),
        ("join", "newcomer"),
        ("expect", False, {"owner": True, "newcomer": False}),
    ],
    "accepting a later non-material version": [
        ("publish", True),
        ("publish", False),
        ("accept", "owner", 2),
        ("expect", True, {"owner": True}),
    ],
}


@pytest.mark.django_db
@pytest.mark.parametrize("steps", POLICY_RULE_CASES.values(), ids=POLICY_RULE_CASES.keys())
def test_policy_history_decides_acceptance_and_ai_use(steps):
    _user, owner, household = make_member("owner")
    people = {"owner": owner}
    versions = []
    for kind, *args in steps:
        if kind == "publish":
            versions.append(publish_policy(material=args[0], body=f"Synthetic policy v{len(versions) + 1}"))
        elif kind == "accept":
            member, which = args
            accept_policy(people[member], current_policy() if which == "current" else versions[which - 1])
        elif kind == "join":
            people[args[0]] = make_member(args[0], household=household)[1]
        elif kind == "leave":
            leave_household(people[args[0]])
            assert not Membership.objects.filter(person=people[args[0]], ended_at__isnull=True).exists()
        else:
            household_allowed, accepted = args
            for member, expected in accepted.items():
                assert in_acceptance(people[member]) is expected, member
                assert may_use_ai(people[member]) is expected, member
            assert household_ai_allowed(household) is household_allowed


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


def _policy_date(policy):
    return timezone.localtime(policy.published_at).date().isoformat().encode()


POLICY_LINK_CASES = {
    "policy-page": ("privacy-policy", False, lambda policy: [
        b"Privacy and data policy",
        f"Version {policy.version}".encode(),
        _policy_date(policy),
    ]),
    "sign-in": ("login", True, lambda policy: [reverse("privacy-policy").encode()]),
    "join": ("join", True, lambda policy: [b"Privacy and data policy"]),
    "setup": ("setup", False, lambda policy: [b"Privacy and data policy"]),
}


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
@pytest.mark.parametrize("case", POLICY_LINK_CASES.values(), ids=POLICY_LINK_CASES.keys())
def test_pages_present_or_link_to_the_policy(case):
    url_name, needs_member, expected = case
    if needs_member:
        make_member("owner")

    response = Client().get(reverse(url_name))

    assert response.status_code == 200
    for fragment in expected(current_policy()):
        assert fragment in response.content


def _onboard_by_setup(accept):
    client = Client()
    version = {"accept_privacy_policy": "on", "privacy_policy_version": str(current_policy().version)} if accept else {}
    client.post(reverse("setup"), _setup_form(**version))
    person = get_user_model().objects.get().person
    assert client.get(reverse("home")).status_code == 200
    return person


def _onboard_by_join(accept):
    inviter, inviter_person, household = make_member("owner")
    client = Client()
    client.force_login(inviter)
    stamp_recent_auth(client)
    form = {
        "invitation_code": create_invitation(inviter_person),
        "username": "new-member",
        "display_name": "New Example",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    if accept:
        form.update(accept_privacy_policy="on", privacy_policy_version=str(current_policy().version))
    joined = Client().post(reverse("join"), form)
    person = get_user_model().objects.get(username="new-member").person
    assert joined.status_code == 200
    assert Membership.objects.filter(person=person, household=household, ended_at__isnull=True).exists()
    return person


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
@pytest.mark.parametrize("accept", [True, False], ids=["accepting", "not-accepting"])
@pytest.mark.parametrize("onboard", [_onboard_by_setup, _onboard_by_join], ids=["setup", "join"])
def test_setup_and_join_finish_with_or_without_accepting_the_policy(onboard, accept):
    person = onboard(accept)

    rows = PrivacyPolicyAcceptance.objects.filter(person=person)
    if accept:
        assert rows.get().policy_version == current_policy()
    else:
        assert not rows.exists()
    assert may_use_ai(person) is accept


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


@dataclass
class StaleSubmission:
    """What a form submitted with an out-of-date policy version left behind."""

    response: object
    person: Person
    completed: Callable


def _publish_next_policy():
    return publish_policy(material=True, body="Synthetic policy v2")


def _stale_respond(action, *, json_reply=False):
    def submit():
        user, person, _household = make_member("owner")
        client = Client()
        client.force_login(user)
        shown = _hidden_version(client.get(reverse("privacy-policy")).content, "version")
        _publish_next_policy()
        extra = {"HTTP_ACCEPT": "application/json"} if json_reply else {}

        response = client.post(
            reverse("privacy-policy-respond"),
            {"action": action, "version": shown, "next": reverse("account-settings")},
            **extra,
        )

        def completed():
            if json_reply:
                assert response.status_code == 409
                assert response.json()["policy_url"] == reverse("privacy-policy")
                return
            assert response.status_code == 302
            assert response.url == reverse("privacy-policy")
            # The old version's own page offers no way to accept it any more.
            stale_page = client.get(reverse("privacy-policy-version", args=(int(shown),)))
            assert b"Accept this version" not in stale_page.content
            assert b"Read the current version" in stale_page.content

        return StaleSubmission(response, person, completed)

    return submit


def _stale_settings_ai():
    user, person, _household = make_member("owner")
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    shown = _hidden_version(client.get(reverse("settings-ai")).content, "version")
    later = _publish_next_policy()

    response = client.post(reverse("settings-ai"), {"action": "accept-privacy-policy", "version": shown})

    def completed():
        assert response.status_code == 200
        assert f"Current version {later.version}".encode() in response.content
        assert f'name="version" value="{later.version}"'.encode() in response.content

    return StaleSubmission(response, person, completed)


def _stale_setup():
    client = Client()
    shown = _hidden_version(client.get(reverse("setup")).content)
    _publish_next_policy()

    response = client.post(reverse("setup"), _setup_form(accept_privacy_policy="on", privacy_policy_version=shown))

    user = get_user_model().objects.get()

    def completed():
        assert response.status_code == 200
        assert client.get(reverse("home")).status_code == 200

    return StaleSubmission(response, user.person, completed)


def _stale_join():
    inviter, inviter_person, household = make_member("owner")
    client = Client()
    client.force_login(inviter)
    stamp_recent_auth(client)
    code = create_invitation(inviter_person)
    join_client = Client()
    shown = _hidden_version(join_client.get(reverse("join")).content)
    _publish_next_policy()

    response = join_client.post(
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

    def completed():
        assert response.status_code == 200
        assert Membership.objects.filter(person=new_user.person, household=household, ended_at__isnull=True).exists()

    return StaleSubmission(response, new_user.person, completed)


def _stale_google_join():
    from tests.test_google_auth import _finish_google, _google_settings

    _inviter, inviter_person, _household = make_member("owner")
    code = create_invitation(inviter_person)
    client = Client()
    shown = _hidden_version(client.get(reverse("join")).content)
    _publish_next_policy()

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
        response = _finish_google(client, start)

    new_user = get_user_model().objects.get(username="joined-google")

    def completed():
        assert response.status_code == 200

    return StaleSubmission(response, new_user.person, completed)


def _stale_google_setup():
    from tests.test_google_auth import _finish_google, _google_settings

    client = Client()
    shown = _hidden_version(client.get(reverse("setup")).content)
    _publish_next_policy()

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
        response = _finish_google(client, start)

    user = get_user_model().objects.get(username="first-google")

    def completed():
        assert response.status_code == 200
        assert client.session.get("_auth_user_id")

    return StaleSubmission(response, user.person, completed)


STALE_ENTRIES = {
    "respond-accept": _stale_respond("accept"),
    "respond-decline": _stale_respond("decline"),
    "respond-json-accept": _stale_respond("accept", json_reply=True),
    "respond-json-decline": _stale_respond("decline", json_reply=True),
    "respond-json-dismiss": _stale_respond("dismiss", json_reply=True),
    "settings-ai": _stale_settings_ai,
    "setup": _stale_setup,
    "join": _stale_join,
    "google-join": _stale_google_join,
    "google-setup": _stale_google_setup,
}


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
@pytest.mark.parametrize("entry", STALE_ENTRIES.values(), ids=STALE_ENTRIES.keys())
def test_stale_shown_version_is_never_recorded(entry):
    submission = entry()

    person = submission.person
    person.refresh_from_db()
    assert not PrivacyPolicyAcceptance.objects.filter(person=person).exists()
    assert person.privacy_policy_declined_version_id is None
    assert person.privacy_policy_dismissed_version_id is None
    assert not in_acceptance(person)
    submission.completed()



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
