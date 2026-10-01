import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import connection, connections
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from allauth.socialaccount.models import SocialAccount, SocialToken

from finance.allauth_adapters import MemberSocialAccountAdapter
from finance.auth_services import accept_invitation, create_invitation, create_recovery_codes
from finance.google_auth import (
    GOOGLE_FLOW_NONCE_STATE_KEY,
    GoogleOnboardingConflict,
    complete_google_onboarding,
    consume_google_pending,
    disconnect_google_account,
    remove_member_password,
    sign_in_method_count,
    store_google_pending,
)
from finance.models import Invitation, RecoveryCode
from tests.test_auth_flows import PASSWORD, SYNTHETIC_SETUP_CODE, make_member


GOOGLE_CLIENT_ID = "synthetic-google-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "synthetic-google-client-secret"
GOOGLE_SUB = "synthetic-google-sub-001"
GOOGLE_EMAIL = "synthetic.google.member@example.com"


def _google_settings(**extra):
    providers = {
        "google": {
            "APP": {"client_id": GOOGLE_CLIENT_ID, "secret": GOOGLE_CLIENT_SECRET},
            "SCOPE": ["openid", "email", "profile"],
            "AUTH_PARAMS": {"access_type": "online"},
            "OAUTH_PKCE_ENABLED": True,
        }
    }
    return override_settings(
        GOOGLE_CLIENT_ID=GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET,
        SOCIALACCOUNT_PROVIDERS=providers,
        **extra,
    )


def _id_token(sub=GOOGLE_SUB, email=GOOGLE_EMAIL, verified=True):
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": GOOGLE_CLIENT_ID,
        "sub": sub,
        "email": email,
        "email_verified": verified,
        "exp": now + 3600,
        "iat": now,
        "name": "Synthetic Google User",
    }
    return jwt.encode(payload, "synthetic-hs256-key-with-enough-bytes", algorithm="HS256")


class _TokenResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)
        self.content = self.text.encode()
        self._payload = payload

    def json(self):
        return self._payload


class _FakeTokenSession:
    def __init__(self, id_token):
        self.id_token = id_token
        self.token_posts = []

    def request(self, method, url, params=None, data=None, headers=None, auth=None, **kwargs):
        self.token_posts.append({"method": method, "url": url, "data": data})
        if "oauth2.googleapis.com/token" not in url:
            raise AssertionError(f"unexpected request {method} {url}")
        payload = {
            "access_token": "synthetic-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": self.id_token,
        }
        return _TokenResponse(payload)


def _finish_google(client, start_response, id_token=_id_token()):
    assert start_response.status_code == 302
    location = start_response["Location"]
    assert "code_challenge=" in location
    assert "state=" in location
    state = parse_qs(urlparse(location).query)["state"][0]
    fake = _FakeTokenSession(id_token)
    with patch.object(MemberSocialAccountAdapter, "get_requests_session", return_value=fake):
        callback = client.get(reverse("google_callback"), {"code": "synthetic-auth-code", "state": state})
    assert fake.token_posts
    assert fake.token_posts[0]["data"].get("code_verifier")
    return callback


@pytest.mark.django_db
def test_google_controls_are_hidden_when_credentials_are_unset():
    make_member()
    client = Client()
    login_page = client.get(reverse("login"))
    join_page = client.get(reverse("join"))

    assert login_page.status_code == 200
    assert b"Sign in with Google" not in login_page.content
    assert b"Join with Google" not in join_page.content
    assert client.get(reverse("google_login")).status_code == 404
    assert client.get(reverse("google_callback")).status_code == 404
    assert client.post(reverse("google-sign-in")).status_code == 404


@pytest.mark.django_db
@_google_settings()
def test_invited_person_can_join_with_google_and_uninvited_identity_cannot():
    inviter, _person, _household = make_member("inviter")
    code = create_invitation(inviter.person)
    client = Client()

    uninvited = _finish_google(client, client.post(reverse("google-sign-in")))
    assert uninvited.status_code == 302
    assert get_user_model().objects.count() == 1
    assert SocialAccount.objects.count() == 0

    start = client.post(
        reverse("join"),
        {
            "intent": "google",
            "invitation_code": code,
            "username": "joined-google",
            "display_name": "Joined Google",
        },
    )
    joined = _finish_google(client, start)
    assert joined.status_code == 200
    assert b"Save these one-time recovery codes" in joined.content
    user = get_user_model().objects.get(username="joined-google")
    assert not user.has_usable_password()
    assert SocialAccount.objects.filter(user=user, uid=GOOGLE_SUB).exists()
    assert SocialToken.objects.count() == 0
    assert RecoveryCode.objects.filter(user=user).count() == 8
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@_google_settings()
def test_email_match_without_sub_does_not_sign_in_or_link():
    user, _person, _household = make_member()
    user.email = GOOGLE_EMAIL
    user.save(update_fields=("email",))
    client = Client()

    response = _finish_google(client, client.post(reverse("google-sign-in")))

    assert "_auth_user_id" not in client.session
    assert not SocialAccount.objects.exists()
    assert get_user_model().objects.get(pk=user.pk).email == GOOGLE_EMAIL
    assert response.status_code == 302


@pytest.mark.django_db
@_google_settings()
def test_unverified_google_email_is_refused():
    make_member()
    client = Client()

    _finish_google(client, client.post(reverse("google-sign-in")), id_token=_id_token(verified=False))

    assert "_auth_user_id" not in client.session
    assert not SocialAccount.objects.exists()


@pytest.mark.django_db
@_google_settings()
def test_password_member_can_connect_google_and_sign_in_with_either_method():
    user, _person, _household = make_member()
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    connected = _finish_google(
        client,
        client.post(reverse("account-settings"), {"action": "connect-google"}),
    )
    assert connected.status_code == 302
    assert SocialAccount.objects.filter(user=user, uid=GOOGLE_SUB).exists()
    assert SocialToken.objects.count() == 0

    client.post(reverse("logout"))
    signed_in = _finish_google(client, client.post(reverse("google-sign-in")))
    assert signed_in.status_code == 302
    assert client.session.get("_auth_user_id") == str(user.pk)

    client.post(reverse("logout"))
    password = client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert password.status_code == 302
    assert client.session.get("_auth_user_id") == str(user.pk)

    removed = client.post(reverse("account-settings"), {"action": "remove-password"})
    assert removed.status_code == 200
    user.refresh_from_db()
    assert not user.has_usable_password()
    disconnect = client.post(reverse("account-settings"), {"action": "disconnect-google"})
    assert b"Keep at least one sign-in method" in disconnect.content
    assert SocialAccount.objects.filter(user=user).exists()


@pytest.mark.django_db
@_google_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_first_run_setup_with_google_requires_setup_code_and_creates_one_member():
    client = Client()
    wrong = client.post(
        reverse("setup"),
        {
            "intent": "google",
            "setup_code": "wrong-setup-code",
            "username": "first-google",
            "display_name": "First Google",
            "household_name": "Synthetic Household",
        },
    )
    assert wrong.status_code == 200
    assert get_user_model().objects.count() == 0
    assert b"accounts.google.com" not in wrong.get("Location", "").encode() if wrong.get("Location") else True

    start = client.post(
        reverse("setup"),
        {
            "intent": "google",
            "setup_code": SYNTHETIC_SETUP_CODE,
            "username": "first-google",
            "display_name": "First Google",
            "household_name": "Synthetic Household",
        },
    )
    created = _finish_google(client, start)
    assert created.status_code == 200
    assert get_user_model().objects.count() == 1
    assert client.session.get("_auth_user_id")
    assert SocialAccount.objects.filter(uid=GOOGLE_SUB).count() == 1


@pytest.mark.django_db
@_google_settings()
def test_google_callback_rejects_a_missing_or_wrong_state():
    make_member()
    client = Client()
    start = client.post(reverse("google-sign-in"))
    assert start.status_code == 302
    fake = _FakeTokenSession(_id_token())
    with patch.object(MemberSocialAccountAdapter, "get_requests_session", return_value=fake):
        missing = client.get(reverse("google_callback"), {"code": "synthetic-auth-code"})
        wrong = client.get(
            reverse("google_callback"),
            {"code": "synthetic-auth-code", "state": "not-the-stashed-state"},
        )
    assert missing.status_code in (200, 302)
    assert wrong.status_code in (200, 302)
    assert "_auth_user_id" not in client.session
    assert fake.token_posts == []


@pytest.mark.django_db
@_google_settings()
def test_google_sign_in_sets_fixed_session_expiry_and_enforces_csrf():
    user, _person, _household = make_member()
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.post(reverse("google-sign-in")).status_code == 403

    client = Client()
    response = _finish_google(client, client.post(reverse("google-sign-in")))
    assert response.status_code == 302
    session_key = client.session.session_key
    expire_date = Session.objects.get(session_key=session_key).expire_date
    assert abs((expire_date - timezone.now()).total_seconds() - 60 * 60 * 24 * 28) < 30


@pytest.mark.django_db
@_google_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_google_sign_in_uses_the_login_throttle():
    make_member()
    client = Client()
    _finish_google(client, client.post(reverse("google-sign-in")))
    _finish_google(client, client.post(reverse("google-sign-in")))
    blocked = client.post(reverse("google-sign-in"))
    assert blocked.status_code == 200
    assert b"Sign-in failed" in blocked.content
    assert "accounts.google.com" not in (blocked.get("Location") or "")


@pytest.mark.django_db
@_google_settings()
def test_google_only_member_can_recover_with_a_recovery_code():
    user, _person, _household = make_member("google-only")
    user.set_unusable_password()
    user.save(update_fields=("password",))
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    codes = create_recovery_codes(user)
    client = Client()
    response = client.post(
        reverse("recover"),
        {
            "username": user.username,
            "recovery_code": codes[0],
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.has_usable_password()
    assert user.check_password(PASSWORD)


@pytest.mark.django_db
@_google_settings()
def test_google_login_page_offers_google_first():
    make_member()
    content = Client().get(reverse("login")).content
    google_at = content.find(b"Sign in with Google")
    password_at = content.find(b">Sign in</button>")
    assert google_at != -1
    assert password_at != -1
    assert google_at < password_at


def _session_request():
    from django.contrib.sessions.middleware import SessionMiddleware

    request = RequestFactory().get("/")
    SessionMiddleware(lambda response: response).process_request(request)
    request.session.save()
    return request


@pytest.mark.django_db
def test_google_pending_nonce_is_refused_when_missing_unknown_or_already_used():
    request = _session_request()
    nonce = store_google_pending(request, {"intent": "join", "invitation_code": "synthetic-a"})
    sociallogin = SimpleNamespace(state={GOOGLE_FLOW_NONCE_STATE_KEY: nonce})

    assert consume_google_pending(request, SimpleNamespace(state={})) is None
    assert consume_google_pending(
        request, SimpleNamespace(state={GOOGLE_FLOW_NONCE_STATE_KEY: "unknown-nonce"})
    ) is None
    first = consume_google_pending(request, sociallogin)
    second = consume_google_pending(request, sociallogin)

    assert first == {"intent": "join", "invitation_code": "synthetic-a"}
    assert second is None


@pytest.mark.django_db
@_google_settings()
def test_two_tab_google_joins_bind_each_invitation_to_its_own_oauth_callback():
    inviter_a, _person_a, household_a = make_member("inviter-a")
    inviter_b, _person_b, household_b = make_member("inviter-b")
    code_a = create_invitation(inviter_a.person)
    code_b = create_invitation(inviter_b.person)
    client = Client()

    start_a = client.post(
        reverse("join"),
        {
            "intent": "google",
            "invitation_code": code_a,
            "username": "joined-a",
            "display_name": "Joined A",
        },
    )
    start_b = client.post(
        reverse("join"),
        {
            "intent": "google",
            "invitation_code": code_b,
            "username": "joined-b",
            "display_name": "Joined B",
        },
    )

    finished_a = _finish_google(client, start_a, id_token=_id_token(sub="synthetic-google-sub-a"))
    assert finished_a.status_code == 200
    user_a = get_user_model().objects.get(username="joined-a")
    assert not get_user_model().objects.filter(username="joined-b").exists()
    assert user_a.person.memberships.get().household_id == household_a.pk
    assert SocialAccount.objects.filter(user=user_a, uid="synthetic-google-sub-a").exists()
    invitation_a = Invitation.objects.get(household=household_a, invited_by=inviter_a.person)
    invitation_b = Invitation.objects.get(household=household_b, invited_by=inviter_b.person)
    assert invitation_a.used_at is not None
    assert invitation_b.used_at is None

    finished_b = _finish_google(client, start_b, id_token=_id_token(sub="synthetic-google-sub-b"))
    assert finished_b.status_code == 200
    user_b = get_user_model().objects.get(username="joined-b")
    assert user_b.person.memberships.get().household_id == household_b.pk
    invitation_b.refresh_from_db()
    assert invitation_b.used_at is not None


@pytest.mark.django_db
@_google_settings()
def test_google_join_callback_is_refused_when_its_nonce_was_already_consumed():
    inviter, _person, _household = make_member("inviter-nonce")
    code = create_invitation(inviter.person)
    client = Client()
    start = client.post(
        reverse("join"),
        {
            "intent": "google",
            "invitation_code": code,
            "username": "joined-nonce",
            "display_name": "Joined Nonce",
        },
    )
    session = client.session
    session["google_pending"] = {}
    session.save()

    refused = _finish_google(client, start)
    assert get_user_model().objects.filter(username="joined-nonce").count() == 0
    assert SocialAccount.objects.count() == 0
    assert refused.status_code == 302


@pytest.mark.django_db(transaction=True)
@_google_settings()
def test_disconnect_google_and_remove_password_race_keeps_one_method():
    if connection.vendor != "postgresql":
        pytest.skip("concurrent sign-in method removal is serialized with PostgreSQL row locks")

    user, _person, _household = make_member("both-methods")
    SocialAccount.objects.create(
        user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB}
    )
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []

    def disconnect():
        try:
            barrier.wait(timeout=10)
            outcomes.append(disconnect_google_account(user))
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    def remove_password():
        try:
            barrier.wait(timeout=10)
            outcomes.append(remove_member_password(user))
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    first = threading.Thread(target=disconnect)
    second = threading.Thread(target=remove_password)
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes) == [False, True]
    user.refresh_from_db()
    assert sign_in_method_count(user) == 1


@pytest.mark.django_db
@_google_settings()
def test_remove_password_succeeds_when_google_is_enabled():
    user, _person, _household = make_member("remove-pwd-enabled")
    SocialAccount.objects.create(
        user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB}
    )
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    removed = client.post(reverse("account-settings"), {"action": "remove-password"})

    assert removed.status_code == 200
    assert b"Keep at least one sign-in method" not in removed.content
    user.refresh_from_db()
    assert not user.has_usable_password()
    assert sign_in_method_count(user) == 1


@pytest.mark.django_db
@override_settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")
def test_remove_password_is_refused_when_google_is_disabled_even_if_linked():
    user, _person, _household = make_member("remove-pwd-disabled")
    SocialAccount.objects.create(
        user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB}
    )
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    refused = client.post(reverse("account-settings"), {"action": "remove-password"})

    assert refused.status_code == 200
    assert b"Keep at least one sign-in method" in refused.content
    user.refresh_from_db()
    assert user.has_usable_password()
    assert user.check_password(PASSWORD)
    assert SocialAccount.objects.filter(user=user, uid=GOOGLE_SUB).exists()


@pytest.mark.django_db
def test_google_onboarding_rolls_back_when_identity_is_already_linked():
    inviter, _person, household = make_member("inviter-linked-identity")
    code_a = create_invitation(inviter.person)
    code_b = create_invitation(inviter.person)

    def link(user):
        SocialAccount.objects.create(
            user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB}
        )

    complete_google_onboarding(
        GOOGLE_SUB,
        lambda: accept_invitation(code_a, "joined-linked-a", "Joined A", password=None),
        link,
    )
    with pytest.raises(GoogleOnboardingConflict):
        complete_google_onboarding(
            GOOGLE_SUB,
            lambda: accept_invitation(code_b, "joined-linked-b", "Joined B", password=None),
            link,
        )

    assert get_user_model().objects.filter(username="joined-linked-a").exists()
    assert not get_user_model().objects.filter(username="joined-linked-b").exists()
    invitations = Invitation.objects.filter(household=household)
    assert invitations.filter(used_at__isnull=False).count() == 1
    assert invitations.filter(used_at__isnull=True).count() == 1
    assert SocialAccount.objects.filter(uid=GOOGLE_SUB).count() == 1


@pytest.mark.django_db(transaction=True)
@_google_settings()
def test_concurrent_google_joins_same_identity_keep_one_member():
    if connection.vendor != "postgresql":
        pytest.skip("concurrent Google identity linking uses PostgreSQL uniqueness and row locks")

    inviter, _person, household = make_member("inviter-identity-race")
    code_a = create_invitation(inviter.person)
    code_b = create_invitation(inviter.person)
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []
    statuses = []

    def link(user):
        SocialAccount.objects.create(
            user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB}
        )

    def join(username, code):
        try:
            barrier.wait(timeout=10)
            try:
                complete_google_onboarding(
                    GOOGLE_SUB,
                    lambda c=code, u=username: accept_invitation(c, u, u, password=None),
                    link,
                )
                outcomes.append("created")
                statuses.append(200)
            except GoogleOnboardingConflict:
                outcomes.append("conflict")
                statuses.append(302)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
            statuses.append(500)
        finally:
            connections.close_all()

    first = threading.Thread(target=join, args=("joined-identity-a", code_a))
    second = threading.Thread(target=join, args=("joined-identity-b", code_b))
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    assert errors == []
    assert 500 not in statuses
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes) == ["conflict", "created"]
    created = get_user_model().objects.filter(
        username__in=("joined-identity-a", "joined-identity-b")
    )
    assert created.count() == 1
    assert SocialAccount.objects.filter(uid=GOOGLE_SUB).count() == 1
    invitations = Invitation.objects.filter(household=household)
    assert invitations.filter(used_at__isnull=False).count() == 1
    assert invitations.filter(used_at__isnull=True).count() == 1
    unused = invitations.get(used_at__isnull=True)
    used = invitations.get(used_at__isnull=False)
    assert unused.pk != used.pk
    assert RecoveryCode.objects.filter(user=created.get()).count() == 8
