from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.auth_services import create_recovery_codes
from finance.models import Account, Household, Invitation, LoginThrottle, Membership, Person, RecoveryCode


PASSWORD = "Synthetic-passphrase-42!"
NEW_PASSWORD = "Another-synthetic-passphrase-84!"


def make_member(username="member"):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user, person, household


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["home", "invite", "logout"])
def test_protected_pages_reject_anonymous_requests(url_name):
    response = Client().get(reverse(url_name))

    assert response.status_code in (302, 405)
    if response.status_code == 302:
        assert response.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_login_creates_long_lived_server_side_session_and_logout_revokes_it():
    user, _person, _household = make_member()
    client = Client()

    response = client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert response.status_code == 302
    session_key = client.session.session_key
    assert session_key
    assert Session.objects.filter(session_key=session_key).exists()
    assert client.cookies["sessionid"]["max-age"] == 60 * 60 * 24 * 28

    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert not Session.objects.filter(session_key=session_key).exists()


@pytest.mark.django_db
def test_user_without_person_profile_cannot_sign_in():
    user = get_user_model().objects.create_user(username="orphan", password=PASSWORD)

    response = Client().post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert response.status_code == 200
    assert b"Sign-in failed" in response.content


@pytest.mark.django_db
@override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_repeated_login_failures_block_correct_password_and_show_generic_error():
    user, _person, _household = make_member()
    client = Client()
    for _ in range(2):
        response = client.post(reverse("login"), {"username": user.username, "password": "wrong"})

    blocked = client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    assert blocked.status_code == 200
    assert b"Sign-in failed" in blocked.content
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@override_settings(LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900, LOGIN_FAILURE_WINDOW_SECONDS=900)
def test_retrying_a_blocked_login_does_not_extend_or_clear_the_block():
    user, _person, _household = make_member()
    client = Client()
    for _ in range(2):
        client.post(reverse("login"), {"username": user.username, "password": "wrong"})

    throttle = LoginThrottle.objects.get()
    assert throttle.blocked_until is not None
    failure_count_before = throttle.failure_count
    window_started_before = throttle.window_started_at
    blocked_until_before = throttle.blocked_until

    # A request against an already-blocked key must not itself count as a
    # failure: doing so would let a caller reset record_login_failure's
    # window (and clear blocked_until) simply by retrying, well before the
    # block is meant to expire.
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})

    throttle.refresh_from_db()
    assert throttle.failure_count == failure_count_before
    assert throttle.window_started_at == window_started_before
    assert throttle.blocked_until == blocked_until_before


@pytest.mark.django_db
def test_login_rejects_external_next_url():
    user, _person, _household = make_member()

    response = Client().post(
        reverse("login"),
        {"username": user.username, "password": PASSWORD, "next": "https://example.invalid/steal"},
    )

    assert response.url == reverse("home")


@pytest.mark.django_db
def test_invitation_is_hashed_one_time_and_joins_same_household():
    user, _person, household = make_member()
    client = Client()
    client.force_login(user)
    response = client.post(reverse("invite"))
    code = response.context["invitation_code"]

    invitation = Invitation.objects.get()
    assert code.encode() in response.content
    assert invitation.token_digest != code

    join_data = {
        "invitation_code": code,
        "username": "New-Member",
        "display_name": "New Example",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    joined = Client().post(reverse("join"), join_data)

    new_user = get_user_model().objects.get(username="new-member")
    assert joined.status_code == 200
    assert len(joined.context["recovery_codes"]) == 8
    assert Membership.objects.filter(person=new_user.person, household=household, ended_at__isnull=True).exists()
    invitation.refresh_from_db()
    assert invitation.used_at is not None
    assert joined["Cache-Control"] == "max-age=0, no-cache, no-store, must-revalidate, private"

    reused = Client().post(reverse("join"), {**join_data, "username": "another-member"})
    assert b"could not be used" in reused.content
    assert not get_user_model().objects.filter(username="another-member").exists()


@pytest.mark.django_db
def test_expired_invitation_cannot_be_used():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    created = client.post(reverse("invite"))
    code = created.context["invitation_code"]
    Invitation.objects.update(expires_at=timezone.now())

    response = Client().post(
        reverse("join"),
        {
            "invitation_code": code,
            "username": "late-member",
            "display_name": "Late Example",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )

    assert b"could not be used" in response.content
    assert not get_user_model().objects.filter(username="late-member").exists()


@pytest.mark.django_db
def test_recovery_code_changes_password_is_consumed_and_revokes_all_sessions():
    user, _person, _household = make_member()
    code = create_recovery_codes(user, count=1)[0]
    first_client = Client()
    second_client = Client()
    first_client.force_login(user)
    second_client.force_login(user)
    session_keys = {first_client.session.session_key, second_client.session.session_key}

    response = Client().post(
        reverse("recover"),
        {"username": user.username, "recovery_code": code, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )

    assert response.context["recovered"] is True
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert RecoveryCode.objects.get().used_at is not None
    assert not Session.objects.filter(session_key__in=session_keys).exists()

    reused = Client().post(
        reverse("recover"),
        {"username": user.username, "recovery_code": code, "password1": PASSWORD, "password2": PASSWORD},
    )
    assert b"Recovery failed" in reused.content


@pytest.mark.django_db
def test_home_does_not_render_another_members_private_account():
    viewer, _viewer_person, _household = make_member("viewer")
    other = get_user_model().objects.create_user(username="other", password=PASSWORD)
    other_person = Person.objects.create(user=other, display_name="Other Example")
    Account.objects.create(name="PRIVATE SECRET NAME", account_type="checking", owner=other_person)
    client = Client()
    client.force_login(viewer)

    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert b"PRIVATE SECRET NAME" not in response.content


@pytest.mark.django_db
def test_first_user_command_creates_household_and_prints_recovery_codes():
    output = StringIO()
    with patch("finance.management.commands.seed_first_user.getpass.getpass", side_effect=[PASSWORD, PASSWORD]):
        call_command(
            "seed_first_user",
            username="first-user",
            display_name="First Example",
            household="Synthetic Household",
            stdout=output,
        )

    assert Person.objects.count() == 1
    assert Membership.objects.count() == 1
    assert RecoveryCode.objects.count() == 8
    assert "Save these one-time recovery codes" in output.getvalue()


@pytest.mark.django_db
def test_login_form_enforces_csrf():
    client = Client(enforce_csrf_checks=True)
    response = client.post(reverse("login"), {"username": "nobody", "password": "invalid"})

    assert response.status_code == 403
