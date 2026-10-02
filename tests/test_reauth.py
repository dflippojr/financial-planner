import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from allauth.socialaccount.models import SocialAccount

from finance.models import Invitation, LoginThrottle, Membership
from finance.reauth import RECENT_AUTH_SESSION_KEY, action_label
from tests.helpers import stamp_recent_auth
from tests.test_auth_flows import PASSWORD, make_member
from tests.test_google_auth import (
    GOOGLE_SUB,
    _finish_google,
    _google_settings,
    _id_token,
)


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
    assert LoginThrottle.objects.get().blocked_until is not None

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
def test_recent_auth_window_expires():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = (timezone.now() - timedelta(minutes=11)).timestamp()
    session.save()

    refused = client.post(reverse("invite"))
    assert refused.status_code == 302
    assert refused.url.startswith(reverse("reauth"))
    assert Invitation.objects.count() == 0


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


@pytest.mark.django_db
def test_leave_household_requires_recent_auth():
    user, person, household = make_member()
    client = Client()
    client.force_login(user)

    refused = client.post(reverse("leave-household"))
    assert refused.status_code == 302
    assert refused.url.startswith(reverse("reauth"))
    assert Membership.objects.filter(person=person, household=household, ended_at__isnull=True).exists()

    stamp_recent_auth(client)
    left = client.post(reverse("leave-household"))
    assert left.status_code == 302
    assert not Membership.objects.filter(person=person, household=household, ended_at__isnull=True).exists()


def _expire_recent_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = (timezone.now() - timedelta(minutes=11)).timestamp()
    session.save()


@pytest.mark.django_db
@_google_settings()
def test_direct_google_connect_from_a_stale_session_is_refused():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    _expire_recent_auth(client)

    response = client.post(reverse("google_login"), {"process": "connect"})

    assert response.status_code == 302
    assert response.url.startswith(reverse("reauth"))
    assert not SocialAccount.objects.filter(user=user).exists()


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
