import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from allauth.socialaccount.models import SocialAccount, SocialToken

from finance.allauth_adapters import MemberSocialAccountAdapter
from finance.auth_services import create_invitation, create_recovery_codes
from finance.models import RecoveryCode
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
