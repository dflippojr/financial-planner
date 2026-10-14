import json
import time
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from allauth.socialaccount.models import SocialAccount

from finance import reauth
from finance.auth_services import throttle_key
from finance.models import Invitation, LoginThrottle, MemberSession, Membership, Passkey
from finance.reauth import RECENT_AUTH_SESSION_KEY, action_label
from financial_planner import urls as project_urls
from tests.helpers import stamp_recent_auth
from tests.test_account_views import make_account
from tests.test_auth_flows import NEW_PASSWORD, PASSWORD, make_member
from tests.test_google_auth import (
    GOOGLE_SUB,
    _finish_google,
    _google_settings,
    _id_token,
)
from tests.test_simplefin import connect_owner


def _reauth_query(response):
    parsed = urlparse(response.url)
    return parse_qs(parsed.query)


@pytest.mark.django_db
def test_invite_is_refused_without_recent_auth_then_succeeds_after_password_confirmation():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)

    refused = client.post(reverse("invite"))
    assert refused.status_code == 302
    assert refused.url.startswith(reverse("reauth"))
    query = _reauth_query(refused)
    assert query["next"] == [reverse("invite")]
    assert query["action"] == ["invite"]
    assert Invitation.objects.count() == 0

    page = client.get(refused.url)
    assert page.status_code == 200
    assert action_label("invite").encode() in page.content
    assert b"Confirm with Google" not in page.content
    assert b"password" in page.content.lower()

    confirmed = client.post(reverse("reauth"), {"password": PASSWORD, "next": reverse("invite"), "action": "invite"})
    assert confirmed.status_code == 302
    assert confirmed.url == reverse("invite")
    assert RECENT_AUTH_SESSION_KEY in client.session

    created = client.post(reverse("invite"))
    assert created.status_code == 200
    assert created.context["invitation_code"]
    assert Invitation.objects.count() == 1


@pytest.mark.django_db
def test_reauth_next_stays_on_this_site():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)

    page = client.get(reverse("reauth"), {"next": "https://example.invalid/steal", "action": "invite"})
    assert page.status_code == 200
    assert page.context["next"] == reverse("home")

    confirmed = client.post(
        reverse("reauth"),
        {"password": PASSWORD, "next": "https://example.invalid/steal", "action": "invite"},
    )
    assert confirmed.url == reverse("home")


@pytest.mark.django_db
@override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_reauth_failures_use_the_login_throttle():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)

    first = client.post(reverse("reauth"), {"password": "wrong-password", "next": reverse("invite")})
    second = client.post(reverse("reauth"), {"password": "wrong-password", "next": reverse("invite")})
    assert first.status_code == second.status_code == 200
    assert LoginThrottle.objects.get(key_digest=throttle_key(user.username, "127.0.0.1").pair_digest).blocked_until is not None

    blocked = client.post(reverse("reauth"), {"password": PASSWORD, "next": reverse("invite")})
    assert blocked.status_code == 200
    assert RECENT_AUTH_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_sign_in_stamps_recent_auth_and_sign_out_clears_it():
    user, _person, _household = make_member()
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert RECENT_AUTH_SESSION_KEY in client.session
    client.post(reverse("logout"))
    assert RECENT_AUTH_SESSION_KEY not in client.session



@pytest.mark.django_db
def test_google_only_member_is_not_asked_for_a_password():
    user, _person, _household = make_member("google-only")
    user.set_unusable_password()
    user.save(update_fields=("password",))
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    client = Client()
    client.force_login(user)

    page = client.get(reverse("reauth"), {"next": reverse("invite"), "action": "invite"})
    assert b'name="password"' not in page.content
    assert b"Confirm with Google" not in page.content


@pytest.mark.django_db
@_google_settings()
def test_google_only_member_sees_google_confirmation_when_enabled():
    user, _person, _household = make_member("google-only-enabled")
    user.set_unusable_password()
    user.save(update_fields=("password",))
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    client = Client()
    client.force_login(user)

    page = client.get(reverse("reauth"), {"next": reverse("invite"), "action": "invite"})
    assert b'name="password"' not in page.content
    assert b"Confirm with Google" in page.content


@pytest.mark.django_db
@_google_settings()
def test_google_reauth_accepts_fresh_matching_sub_and_refuses_stale_or_wrong_identity():
    user, _person, _household = make_member()
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    client = Client()
    client.force_login(user)

    start = client.post(reverse("reauth-google"), {"next": reverse("invite"), "action": "invite"})
    location = start["Location"]
    params = parse_qs(urlparse(location).query)
    assert params.get("prompt") == ["select_account"]
    assert params.get("max_age") == ["0"]
    assert json.loads(params["claims"][0]) == {"id_token": {"auth_time": {"essential": True}}}

    stale = _finish_google(client, start, id_token=_id_token(auth_time=int(time.time()) - 3600))
    assert stale.status_code == 302
    assert stale.url.startswith(reverse("reauth"))
    assert RECENT_AUTH_SESSION_KEY not in client.session

    wrong = _finish_google(
        client,
        client.post(reverse("reauth-google"), {"next": reverse("invite")}),
        id_token=_id_token(sub="synthetic-google-sub-other", auth_time=int(time.time())),
    )
    assert wrong.status_code == 302
    assert RECENT_AUTH_SESSION_KEY not in client.session

    accepted = _finish_google(
        client,
        client.post(reverse("reauth-google"), {"next": reverse("invite")}),
        id_token=_id_token(auth_time=int(time.time())),
    )
    assert accepted.status_code == 302
    assert accepted.url == reverse("invite")
    assert RECENT_AUTH_SESSION_KEY in client.session



def _expire_recent_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = (timezone.now() - timedelta(minutes=11)).timestamp()
    session.save()



@pytest.mark.django_db
@_google_settings()
def test_google_connect_callback_rechecks_recent_auth():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    start = client.post(reverse("google_login"), {"process": "connect"})
    _expire_recent_auth(client)

    finished = _finish_google(client, start)

    assert finished.status_code == 302
    assert finished.url.startswith(reverse("reauth"))
    assert not SocialAccount.objects.filter(user=user).exists()


@pytest.mark.django_db
@_google_settings()
def test_another_members_google_login_never_confirms_this_session():
    victim, _person, _household = make_member("victim")
    attacker, _attacker_person, _attacker_household = make_member("attacker")
    SocialAccount.objects.create(user=attacker, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    client = Client()
    client.force_login(victim)
    _expire_recent_auth(client)

    start = client.post(reverse("google_login"), {"process": "redirect"})
    _finish_google(client, start)

    # Whatever happened, the victim's session must not be freshly confirmed.
    session = client.session
    if str(session.get("_auth_user_id")) == str(victim.pk):
        fresh = session.get(RECENT_AUTH_SESSION_KEY)
        assert not (fresh and fresh > (timezone.now() - timedelta(minutes=10)).timestamp())
    refused = client.post(reverse("invite"))
    assert refused.status_code == 302
    assert Invitation.objects.count() == 0


@pytest.mark.django_db
@_google_settings()
def test_own_google_login_from_a_stale_session_does_not_confirm_it():
    member, _person, _household = make_member("member")
    SocialAccount.objects.create(user=member, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    client = Client()
    client.force_login(member)
    _expire_recent_auth(client)

    # A silent Google sign-in through the ordinary login flow must not stand in
    # for the confirmation step, which checks how recently Google authenticated.
    start = client.post(reverse("google_login"), {"process": "redirect"})
    _finish_google(client, start)

    refused = client.post(reverse("invite"))
    assert refused.status_code == 302
    assert Invitation.objects.count() == 0


@pytest.mark.django_db
@_google_settings()
def test_google_reauth_without_auth_time_needs_a_freshly_issued_token():
    user, _person, _household = make_member()
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    client = Client()
    client.force_login(user)

    # A token issued well before this round trip never confirms.
    old = _finish_google(
        client,
        client.post(reverse("reauth-google"), {"next": reverse("invite")}),
        id_token=_id_token(issued_at=int(time.time()) - 900),
    )
    assert old.url.startswith(reverse("reauth"))
    assert RECENT_AUTH_SESSION_KEY not in client.session

    # Unpublished apps get no auth_time from Google; a fresh round trip counts.
    fresh = _finish_google(
        client,
        client.post(reverse("reauth-google"), {"next": reverse("invite")}),
        id_token=_id_token(),
    )
    assert fresh.url == reverse("invite")
    assert RECENT_AUTH_SESSION_KEY in client.session


@pytest.mark.django_db
def test_leave_household_ends_the_membership_once_confirmed():
    user, person, household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)

    left = client.post(reverse("leave-household"))

    assert left.status_code == 302
    assert not Membership.objects.filter(person=person, household=household, ended_at__isnull=True).exists()


@dataclass(frozen=True)
class StaleCase:
    """One state-changing POST that needs a recent sign-in.

    ``form_url_name`` is where the member is sent back to resubmit; None means
    the page that was posted to. ``args`` and ``payload`` receive the world
    built by ``stale_world`` so a case can point at the rows it needs.
    """

    url_name: str
    action: str
    form_url_name: str | None = None
    args: Callable = lambda world: ()
    payload: Callable = lambda world: {}
    case_id: str | None = None


def _settings_ai(url_name, action, **kwargs):
    return StaleCase(url_name, action, "settings-ai", **kwargs)


def _account_args(world):
    return (world.account.pk,)


STALE_CASES = [
    StaleCase("account-share", "share-account", "account-list", _account_args, lambda world: {"share_mode": "co_owned"}),
    StaleCase(
        "account-share-mode",
        "change-share-mode",
        "account-list",
        _account_args,
        lambda world: {"share_mode": "lent", "confirm_give_up_ownership": "on"},
    ),
    StaleCase("account-unshare", "unshare-account", "account-list", _account_args),
    StaleCase("account-delete", "delete-account", None, _account_args, lambda world: {"confirm_name": world.account.name}),
    _settings_ai(
        "ai-connect",
        "connect-ai",
        payload=lambda world: {"base_url": "https://harness.example.test", "token": "synthetic-token"},
    ),
    _settings_ai("ai-disconnect", "disconnect-ai"),
    _settings_ai(
        "ai-defaults",
        "ai-defaults",
        payload=lambda world: {"chat_backend": "claude", "background_backend": "claude"},
    ),
    _settings_ai("ai-offer-local", "ai-offer-local", payload=lambda world: {"offer_local_to_household": "on"}),
    _settings_ai("ai-offer-local-chat", "ai-offer-local-chat", payload=lambda world: {"offer_local_chat": "on"}),
    _settings_ai("ai-shared-local", "ai-shared-local", payload=lambda world: {"use_shared_local_chat": "on"}),
    _settings_ai(
        "ai-key-connect",
        "connect-ai-key",
        payload=lambda world: {"provider": "anthropic", "key": "synthetic-api-key"},
    ),
    _settings_ai(
        "ai-key-defaults",
        "ai-key-defaults",
        args=lambda world: ("anthropic",),
        payload=lambda world: {"use_for_chat": "on"},
    ),
    _settings_ai("ai-key-disconnect", "disconnect-ai-key", args=lambda world: ("anthropic",)),
    _settings_ai("ai-plan-unlink", "unlink-ai-plan", args=lambda world: ("claude",)),
    _settings_ai("ai-offer-plan", "ai-offer-plan", payload=lambda world: {"offer_plan_links": "on"}),
    StaleCase("passkey-delete", "remove-passkey", "account-settings", lambda world: (world.passkey.pk,)),
    StaleCase(
        "passkey-require",
        "require-passkey",
        "account-settings",
        payload=lambda world: {"require_passkey": "on"},
    ),
    StaleCase(
        "simplefin-connections",
        "connect-simplefin",
        "simplefin-connections",
        payload=lambda world: {"intent": "claim", "token": "synthetic-setup-token"},
    ),
    StaleCase("simplefin-disconnect", "disconnect-simplefin", "simplefin-connections"),
    StaleCase("invite", "invite"),
    StaleCase(
        "account-settings",
        "add-password",
        "account-settings",
        payload=lambda world: {"action": "add-password", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        case_id="account-settings-add-password",
    ),
    StaleCase(
        "account-settings",
        "remove-password",
        "account-settings",
        payload=lambda world: {"action": "remove-password"},
        case_id="account-settings-remove-password",
    ),
    StaleCase(
        "account-settings",
        "disconnect-google",
        "account-settings",
        payload=lambda world: {"action": "disconnect-google"},
        case_id="account-settings-disconnect-google",
    ),
    StaleCase(
        "account-settings",
        "connect-google",
        "account-settings",
        payload=lambda world: {"action": "connect-google"},
        case_id="account-settings-connect-google",
    ),
    StaleCase("revoke-session", "revoke-session", "account-settings", lambda world: (world.other_session.pk,)),
    StaleCase("revoke-other-sessions", "revoke-other-sessions", "account-settings"),
    StaleCase("account-export", "export-data", "settings-data"),
    StaleCase("leave-household", "leave-household", "invite"),
    StaleCase(
        "delete-my-data",
        "delete-my-data",
        "delete-my-data",
        payload=lambda world: {"confirm_username": world.user.username},
    ),
    # Google's connect entry is not a decorated view; it checks the same window itself.
    StaleCase(
        "google_login",
        "connect-google",
        "account-settings",
        payload=lambda world: {"process": "connect"},
        case_id="google-connect-entry",
    ),
]


def _normalized(value):
    return bytes(value) if isinstance(value, memoryview) else value


def _database_state():
    """Every finance row, the members and the open sessions, to compare before and after a refusal."""
    state = {}
    for model in [*apps.get_app_config("finance").get_models(), get_user_model()]:
        if model is MemberSession:
            # The activity stamp moves on every request; which sessions exist is what matters.
            rows = model.objects.values_list("pk", "session_key")
        else:
            rows = model.objects.values_list()
        state[model._meta.label] = sorted(repr([_normalized(value) for value in row]) for row in rows)
    state["sessions"] = sorted(Session.objects.values_list("session_key", flat=True))
    return state


@pytest.fixture
def stale_world(monkeypatch):
    user, person, _household = make_member()
    account = make_account(person)
    passkey = Passkey.objects.create(
        member=person, name="Synthetic key", credential_id=b"synthetic-credential", public_key=b"x"
    )
    connect_owner(person, monkeypatch)
    # A second signed-in session for the same member, for the sign-out cases.
    other = Client()
    other.force_login(user)
    other.get(reverse("home"))
    client = Client()
    client.force_login(user)
    client.get(reverse("home"))
    _expire_recent_auth(client)
    return SimpleNamespace(
        client=client,
        user=user,
        person=person,
        account=account,
        passkey=passkey,
        other_session=MemberSession.objects.get(session_key=other.session.session_key),
    )


@pytest.mark.django_db
@_google_settings()
@pytest.mark.parametrize("case", STALE_CASES, ids=lambda case: case.case_id or case.url_name)
def test_stale_session_is_sent_to_reauth_without_side_effects(stale_world, case):
    world = stale_world
    url = reverse(case.url_name, args=case.args(world))
    before = _database_state()

    refused = world.client.post(url, case.payload(world))

    assert refused.status_code == 302
    query = _reauth_query(refused)
    assert urlparse(refused.url).path == reverse("reauth")
    assert query["action"] == [case.action]
    assert query["next"] == [reverse(case.form_url_name) if case.form_url_name else url]
    assert _database_state() == before
    assert world.client.get(refused.url).status_code == 200


def _decorated_url_names():
    marker = reauth.requires_recent_auth("any")(lambda request: None).__code__
    names = set()
    for pattern in project_urls.urlpatterns:
        view = pattern.callback
        while view is not None:
            if getattr(view, "__code__", None) is marker:
                names.add(pattern.name)
                break
            view = getattr(view, "__wrapped__", None)
    return names


def test_every_view_that_requires_recent_auth_has_a_stale_session_case():
    covered = {case.url_name for case in STALE_CASES}

    assert _decorated_url_names() - covered == set()
    # A case for a view that lost the decorator would test nothing; Google's own check is the one exception.
    assert covered - _decorated_url_names() == {"google_login"}
