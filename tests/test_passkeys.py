from datetime import timedelta
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.auth_services import create_recovery_codes
from finance.lifecycle_services import delete_member_data
from finance.models import Passkey, RecoveryCode
from finance.reauth import RECENT_AUTH_SESSION_KEY
from tests.helpers import stamp_recent_auth
from tests.test_auth_flows import PASSWORD, make_member
from tests.webauthn_device import SoftwareAuthenticator


PASSKEY_SETTINGS = override_settings(
    ALLOWED_HOSTS=["localhost", "testserver"],
    CSRF_TRUSTED_ORIGINS=["http://localhost"],
)


def _expire_recent_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = (timezone.now() - timedelta(minutes=11)).timestamp()
    session.save()
    return client


def _json(client, url, payload=None):
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


def _register_passkey(client, name="Synthetic laptop"):
    options = _json(client, reverse("passkey-register-options"))
    assert options.status_code == 200
    body = options.json()
    device = SoftwareAuthenticator("http://localhost", "localhost")
    credential = device.create(body["options"])
    result = _json(client, reverse("passkey-register"), {"name": name, "credential": credential})
    assert result.status_code == 200, result.content
    assert result.json()["ok"] is True
    return device


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_passkeys_are_private_to_the_owner():
    owner, owner_person, _household = make_member("owner")
    _outsider, outsider_person, _other = make_member("outsider")
    client = Client()
    client.force_login(owner)
    stamp_recent_auth(client)
    _register_passkey(client, "Owner only key")

    assert Passkey.objects.visible_to(owner_person).count() == 1
    assert Passkey.objects.visible_to(outsider_person).count() == 0
    assert Passkey.objects.visible_to(None).count() == 0
    outsider = Client()
    outsider.force_login(get_user_model().objects.get(username="outsider"))
    page = outsider.get(reverse("account-settings"))
    assert b"Owner only key" not in page.content
    owner_page = client.get(reverse("account-settings"))
    assert b"Owner only key" in owner_page.content
    stolen_id = Passkey.objects.get().pk
    stamp_recent_auth(outsider)
    outsider.post(reverse("passkey-delete", args=(stolen_id,)))
    assert Passkey.objects.filter(pk=stolen_id).exists()


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_password_alone_does_not_create_a_session_when_a_passkey_is_required():
    user, person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    device = _register_passkey(client)
    client.post(reverse("passkey-require"), {"require_passkey": "on"})
    person.refresh_from_db()
    assert person.require_passkey_after_password
    client.post(reverse("logout"))

    signed = Client()
    response = signed.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert response.status_code == 302
    assert response.url == reverse("passkey-sign-in")
    assert "_auth_user_id" not in signed.session

    options = _json(signed, reverse("passkey-sign-in-options"))
    credential = device.get(options.json()["options"])
    finished = _json(signed, reverse("passkey-sign-in-assert"), {"credential": credential})
    assert finished.json()["ok"] is True
    assert signed.session["_auth_user_id"] == str(user.pk)
    person.passkeys.get().refresh_from_db()
    assert person.passkeys.get().last_used_at is not None


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_recovery_code_completes_passkey_sign_in_once():
    user, person, _household = make_member()
    codes = create_recovery_codes(user, count=1)
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    _register_passkey(client)
    client.post(reverse("passkey-require"), {"require_passkey": "on"})
    client.post(reverse("logout"))

    signed = Client()
    signed.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    assert "_auth_user_id" not in signed.session
    used = signed.post(reverse("passkey-sign-in"), {"recovery_code": codes[0]})
    assert used.status_code == 302
    assert signed.session["_auth_user_id"] == str(user.pk)
    assert RecoveryCode.objects.get().used_at is not None

    signed.post(reverse("logout"))
    signed.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    reused = signed.post(reverse("passkey-sign-in"), {"recovery_code": codes[0]})
    assert reused.status_code == 200
    assert "_auth_user_id" not in signed.session
    person.refresh_from_db()
    assert person.require_passkey_after_password


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_removing_the_last_passkey_turns_the_requirement_off_after_reauth():
    user, person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    _register_passkey(client)
    client.post(reverse("passkey-require"), {"require_passkey": "on"})
    passkey_id = Passkey.objects.get().pk

    _expire_recent_auth(client)
    refused = client.post(reverse("passkey-delete", args=(passkey_id,)))
    assert refused.url.startswith(reverse("reauth"))
    assert Passkey.objects.filter(pk=passkey_id).exists()

    stamp_recent_auth(client)
    client.post(reverse("passkey-delete", args=(passkey_id,)))
    person.refresh_from_db()
    assert not Passkey.objects.filter(pk=passkey_id).exists()
    assert person.require_passkey_after_password is False


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_passkey_assertion_stamps_recent_auth():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    device = _register_passkey(client)
    session = client.session
    session["recent_auth_at"] = 1
    session.save()

    options = _json(client, reverse("passkey-reauth-options"))
    credential = device.get(options.json()["options"])
    result = _json(
        client,
        reverse("passkey-reauth-assert"),
        {"credential": credential, "next": reverse("invite")},
    )
    assert result.json()["ok"] is True
    assert client.session["recent_auth_at"] > 1


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_member_data_deletion_removes_passkeys():
    user, person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    _register_passkey(client)
    assert Passkey.objects.filter(member=person).count() == 1
    person_id = person.pk
    delete_member_data(person, {})
    assert not Passkey.objects.filter(member_id=person_id).exists()


@PASSKEY_SETTINGS
@pytest.mark.django_db
@override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_passkey_step_uses_the_login_throttle():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    _register_passkey(client)
    client.post(reverse("passkey-require"), {"require_passkey": "on"})
    client.post(reverse("logout"))

    signed = Client()
    signed.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    _json(signed, reverse("passkey-sign-in-assert"), {"credential": {}})
    _json(signed, reverse("passkey-sign-in-assert"), {"credential": {}})
    blocked = _json(signed, reverse("passkey-sign-in-options"))
    assert blocked.status_code == 429
    assert "_auth_user_id" not in signed.session


@PASSKEY_SETTINGS
@pytest.mark.django_db
def test_registration_requires_recent_auth():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    refused = _json(client, reverse("passkey-register-options"))
    assert refused.status_code == 403
    assert refused.json()["reauth"] is True
