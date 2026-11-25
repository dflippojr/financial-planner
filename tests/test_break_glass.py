"""Break-glass for production mode (#792): a host command, never a password form.

`manage.py break_glass_sign_in` prints a one-time link for one member. The
link is the only way in besides Google, exists only after the command ran on
the host, and is refused for any request that came through Cloudflare.
"""

import re
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from allauth.socialaccount.models import SocialAccount

from finance.models import AuditEvent, BreakGlassGrant, LoginThrottle, MemberSecurityEvent
from finance.reauth import RECENT_AUTH_SESSION_KEY
from tests.test_auth_flows import make_member
from tests.test_google_auth import GOOGLE_SUB, _finish_google, _google_settings, _id_token

EVENT_TYPES = MemberSecurityEvent.EventType
LINK = re.compile(r"/break-glass/([A-Za-z0-9_-]{40,})/")


def _google_only(**extra):
    return _google_settings(GOOGLE_ONLY_SIGN_IN=True, **extra)


def _issue(username, **options):
    out = StringIO()
    call_command("break_glass_sign_in", username=username, stdout=out, **options)
    match = LINK.search(out.getvalue())
    assert match, out.getvalue()
    return match.group(0), match.group(1)


@pytest.mark.django_db
@_google_only()
def test_the_command_prints_a_one_time_link_and_stores_only_a_digest():
    user, person, _household = make_member()

    path, token = _issue(user.username)

    assert path == reverse("break-glass", args=[token])
    grant = BreakGlassGrant.objects.get()
    assert grant.user == user
    assert grant.used_at is None
    assert token not in grant.token_digest
    assert timedelta(minutes=14) < grant.expires_at - timezone.now() <= timedelta(minutes=15)
    assert MemberSecurityEvent.objects.filter(member=person, event_type=EVENT_TYPES.BREAK_GLASS_ISSUED).count() == 1


@pytest.mark.django_db
@_google_only()
def test_the_link_signs_the_member_in_once():
    user, person, _household = make_member()
    path, _token = _issue(user.username)
    client = Client()

    # Opening the link changes nothing, so a link preview cannot use it up.
    page = client.get(path)
    assert page.status_code == 200
    assert "_auth_user_id" not in client.session
    assert BreakGlassGrant.objects.get().used_at is None

    response = client.post(path)

    assert response.status_code == 302
    assert response["Location"] == reverse("account-settings")
    assert client.session["_auth_user_id"] == str(user.pk)
    # The member can reconnect Google straight away.
    assert RECENT_AUTH_SESSION_KEY in client.session
    assert client.get(reverse("account-settings")).status_code == 200
    assert BreakGlassGrant.objects.get().used_at is not None
    assert MemberSecurityEvent.objects.filter(member=person, event_type=EVENT_TYPES.BREAK_GLASS_SIGN_IN).count() == 1

    again = Client()
    assert again.get(path).status_code == 404
    assert again.post(path).status_code == 404
    assert "_auth_user_id" not in again.session


@pytest.mark.django_db
@_google_only()
def test_an_expired_link_is_refused():
    user, _person, _household = make_member()
    path, _token = _issue(user.username)
    BreakGlassGrant.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    client = Client()

    assert client.post(path).status_code == 404
    assert "_auth_user_id" not in client.session
    assert BreakGlassGrant.objects.get().used_at is None


@pytest.mark.django_db
@_google_only()
def test_a_new_link_replaces_the_earlier_one():
    user, _person, _household = make_member()
    first, _token = _issue(user.username)
    second, _token = _issue(user.username)
    client = Client()

    assert client.post(first).status_code == 404
    assert "_auth_user_id" not in client.session
    assert client.post(second).status_code == 302


@pytest.mark.django_db
@_google_only()
def test_a_link_for_one_member_never_signs_in_another():
    user, _person, _household = make_member("locked-out")
    other, _other_person, _other_household = make_member("other")
    path, _token = _issue(user.username)
    client = Client()

    client.post(path)

    assert client.session["_auth_user_id"] == str(user.pk)
    assert not BreakGlassGrant.objects.filter(user=other).exists()


@pytest.mark.django_db
@_google_only()
def test_the_google_link_stays_unless_the_operator_asks_to_remove_it():
    user, _person, _household = make_member()
    SocialAccount.objects.create(user=user, provider="google", uid="synthetic-lost-sub", extra_data={})

    _issue(user.username)

    assert SocialAccount.objects.filter(user=user, uid="synthetic-lost-sub").exists()


@pytest.mark.django_db
@_google_only()
def test_a_member_who_lost_their_google_account_connects_another():
    user, person, _household = make_member()
    SocialAccount.objects.create(user=user, provider="google", uid="synthetic-lost-sub", extra_data={})
    path, _token = _issue(user.username, unlink_google=True)
    assert not SocialAccount.objects.filter(user=user).exists()
    assert AuditEvent.objects.filter(action=AuditEvent.Action.GOOGLE_DISCONNECTED, effective_member=person).count() == 1
    client = Client()
    client.post(path)

    connected = _finish_google(client, client.post(reverse("account-settings"), {"action": "connect-google"}))

    assert connected.status_code == 302
    assert SocialAccount.objects.filter(user=user, uid=GOOGLE_SUB).exists()
    client.post(reverse("logout"))
    signed_in = _finish_google(client, client.post(reverse("google-sign-in")), _id_token())
    assert signed_in.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
@_google_only(LOGIN_FAILURE_LIMIT=2)
def test_guessed_links_are_refused_and_throttled():
    user, _person, _household = make_member()
    path, _token = _issue(user.username)
    guess = reverse("break-glass", args=["A" * 43])
    client = Client(REMOTE_ADDR="100.64.0.9")

    assert client.post(guess).status_code == 404
    assert client.post(guess).status_code == 404
    assert LoginThrottle.objects.count() == 1

    # The blocked client cannot use even the real link until the block ends.
    assert client.post(path).status_code == 404
    assert "_auth_user_id" not in client.session
    assert Client(REMOTE_ADDR="100.64.0.10").post(path).status_code == 302


@pytest.mark.django_db
@_google_only()
def test_the_link_is_refused_for_a_request_that_came_through_cloudflare():
    # Cloudflare sets CF-Connecting-IP on every request it forwards and a
    # visitor cannot remove it, so break-glass works on the tailnet only.
    user, _person, _household = make_member()
    path, _token = _issue(user.username)
    client = Client()

    assert client.get(path, HTTP_CF_CONNECTING_IP="203.0.113.20").status_code == 404
    assert client.post(path, HTTP_CF_CONNECTING_IP="203.0.113.20").status_code == 404
    assert client.post(path, HTTP_CF_CONNECTING_IP="").status_code == 404
    assert "_auth_user_id" not in client.session
    assert BreakGlassGrant.objects.get().used_at is None


@pytest.mark.django_db
@_google_only()
def test_the_link_stops_working_when_production_mode_is_turned_off():
    user, _person, _household = make_member()
    path, _token = _issue(user.username)
    client = Client()

    with override_settings(GOOGLE_ONLY_SIGN_IN=False):
        assert client.get(path).status_code == 404
        assert client.post(path).status_code == 404

    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_the_command_is_refused_outside_production_mode():
    # With passwords on, recovery codes are the way back in.
    user, _person, _household = make_member()

    with pytest.raises(CommandError, match="GOOGLE_ONLY_SIGN_IN"):
        call_command("break_glass_sign_in", username=user.username, stdout=StringIO())

    assert not BreakGlassGrant.objects.exists()


@pytest.mark.django_db
@_google_only()
def test_the_command_rejects_an_unknown_member():
    make_member()

    with pytest.raises(CommandError, match="No member"):
        call_command("break_glass_sign_in", username="nobody", stdout=StringIO())

    assert not BreakGlassGrant.objects.exists()
