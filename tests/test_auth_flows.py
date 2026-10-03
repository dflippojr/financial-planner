import logging
import threading
import time
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from tests.helpers import stamp_recent_auth
from finance.auth_services import create_recovery_codes
from finance.models import (
    Account,
    Category,
    Household,
    Invitation,
    LoginThrottle,
    Membership,
    Person,
    RecoveryCode,
)


PASSWORD = "Synthetic-passphrase-42!"
NEW_PASSWORD = "Another-synthetic-passphrase-84!"
SYNTHETIC_SETUP_CODE = "synthetic-setup-code-53"


def make_member(username="member"):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user, person, household


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["home", "spending-by-category", "spending-category-uncategorized", "invite", "logout"])
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
    assert abs(int(client.cookies["sessionid"]["max-age"]) - 60 * 60 * 24 * 28) <= 5

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
    stamp_recent_auth(client)
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
def test_username_normalization_is_consistent_between_join_and_sign_in():
    # U+210C "ℌ" NFKC-decomposes to "H", but str.casefold() alone leaves
    # it unchanged. Django's create_user() applies its own NFKC
    # normalize_username() to whatever we pass it, so casefolding before
    # that (rather than after) let account creation and sign-in disagree.
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    code = client.post(reverse("invite")).context["invitation_code"]

    join_data = {
        "invitation_code": code,
        "username": "ℌenry",
        "display_name": "Henry Example",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    Client().post(reverse("join"), join_data)

    new_user = get_user_model().objects.exclude(pk=user.pk).get()
    assert new_user.username == "henry"

    response = Client().post(reverse("login"), {"username": "Henry", "password": PASSWORD})
    assert response.status_code == 302


@pytest.mark.django_db
def test_username_normalization_survives_a_casefold_that_decomposes():
    # U+01F0 "ǰ" is precomposed; full case folding maps it to "j" plus a
    # combining caron (decomposed), which create_user()'s own NFKC
    # normalization then recomposes back to "ǰ". A single
    # NFKC-then-casefold pass would compute the decomposed form and never
    # match what actually gets stored.
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    code = client.post(reverse("invite")).context["invitation_code"]

    join_data = {
        "invitation_code": code,
        "username": "ǰane",
        "display_name": "Jane Example",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    Client().post(reverse("join"), join_data)

    response = Client().post(reverse("login"), {"username": "ǰane", "password": PASSWORD})
    assert response.status_code == 302


@pytest.mark.django_db
def test_expired_invitation_cannot_be_used():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
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
def test_recovery_succeeds_with_the_original_unnormalized_username():
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    code = client.post(reverse("invite")).context["invitation_code"]

    join_data = {
        "invitation_code": code,
        "username": "ℌenry",
        "display_name": "Henry Example",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    joined = Client().post(reverse("join"), join_data)
    recovery_code = joined.context["recovery_codes"][0]

    # Submitting the original, un-normalized username (as registered) rather
    # than its stored/normalized form must still find the account.
    response = Client().post(
        reverse("recover"),
        {"username": "Henry", "recovery_code": recovery_code, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )

    assert response.context["recovered"] is True
    new_user = get_user_model().objects.get(username="henry")
    assert new_user.check_password(NEW_PASSWORD)


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
@pytest.mark.parametrize(
    "bad_username",
    [
        "ß" * 100,  # 100 chars in, but casefolds to 200 -- over the 150-char column
        "has space",
    ],
)
def test_join_rejects_usernames_that_are_invalid_once_normalized(bad_username):
    user, _person, _household = make_member()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    code = client.post(reverse("invite")).context["invitation_code"]

    response = Client().post(
        reverse("join"),
        {
            "invitation_code": code,
            "username": bad_username,
            "display_name": "Bad Example",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )

    assert response.status_code == 200
    assert "username" in response.context["form"].errors
    assert get_user_model().objects.count() == 1
    assert Invitation.objects.get().used_at is None


@pytest.mark.django_db
def test_first_user_command_rejects_a_username_that_is_invalid_once_normalized():
    too_long_once_casefolded = "ß" * 100
    output = StringIO()

    with patch("finance.management.commands.seed_first_user.getpass.getpass", side_effect=[PASSWORD, PASSWORD]):
        with pytest.raises(CommandError):
            call_command(
                "seed_first_user",
                username=too_long_once_casefolded,
                display_name="First Example",
                household="Synthetic Household",
                stdout=output,
            )

    assert get_user_model().objects.count() == 0


@pytest.mark.django_db
def test_login_form_enforces_csrf():
    client = Client(enforce_csrf_checks=True)
    response = client.post(reverse("login"), {"username": "nobody", "password": "invalid"})

    assert response.status_code == 403


@pytest.mark.django_db
def test_session_expiry_is_fixed_at_sign_in_and_later_writes_do_not_extend_it():
    user, _person, _household = make_member()
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    session_key = client.session.session_key
    at_sign_in = Session.objects.get(session_key=session_key).expire_date

    time.sleep(0.05)
    session = client.session
    session["written_later"] = "value"
    session.save()
    after_write = Session.objects.get(session_key=session_key).expire_date

    assert after_write == at_sign_in
    assert abs((at_sign_in - timezone.now()).total_seconds() - 60 * 60 * 24 * 28) < 30


def _setup_form(username="first-member", setup_code=SYNTHETIC_SETUP_CODE, **overrides):
    data = {
        "setup_code": setup_code,
        "username": username,
        "display_name": "First Example",
        "household_name": "Synthetic Household",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_sign_in_redirects_to_setup_while_no_member_exists():
    response = Client().get(reverse("login"))

    assert response.status_code == 302
    assert response.url == reverse("setup")


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_creates_member_household_categories_codes_and_signs_in():
    client = Client()
    response = client.post(reverse("setup"), _setup_form())

    user = get_user_model().objects.get()
    household = Membership.objects.get().household
    assert response.status_code == 200
    assert len(response.context["recovery_codes"]) == 8
    assert response["Cache-Control"] == "max-age=0, no-cache, no-store, must-revalidate, private"
    assert user.check_password(PASSWORD)
    assert user.person.display_name == "First Example"
    assert household.name == "Synthetic Household"
    assert Category.objects.filter(household=household, name="Income").exists()
    assert RecoveryCode.objects.filter(user=user).count() == 8
    assert client.session.get("_auth_user_id") == str(user.pk)

    assert Client().get(reverse("setup")).status_code == 404
    assert Client().post(reverse("setup"), _setup_form(username="second")).status_code == 404
    assert get_user_model().objects.count() == 1


@pytest.mark.django_db
@override_settings(SETUP_CODE="")
def test_unset_setup_code_explains_and_creates_nothing():
    response = Client().post(reverse("setup"), _setup_form())

    assert response.status_code == 200
    assert b"SETUP_CODE" in response.content
    assert get_user_model().objects.count() == 0
    assert Person.objects.count() == 0


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_wrong_or_missing_setup_code_creates_nothing():
    secret = SYNTHETIC_SETUP_CODE
    wrong = Client().post(reverse("setup"), _setup_form(setup_code="not-the-setup-code"))
    missing = Client().post(reverse("setup"), _setup_form(setup_code=""))

    assert get_user_model().objects.count() == 0
    assert b"not-the-setup-code" not in wrong.content
    assert secret.encode() not in wrong.content
    assert secret.encode() not in missing.content
    assert b"Setup could not be completed" in wrong.content
    assert b"Setup could not be completed" in missing.content


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_is_404_once_a_user_or_person_exists():
    get_user_model().objects.create_user(username="orphan", password=PASSWORD)

    assert Client().get(reverse("setup")).status_code == 404
    assert Client().post(reverse("setup"), _setup_form()).status_code == 404
    assert get_user_model().objects.count() == 1
    assert Person.objects.count() == 0


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE, LOGIN_FAILURE_LIMIT=2, LOGIN_BLOCK_SECONDS=900)
def test_setup_failures_use_the_login_throttle():
    client = Client()
    for _ in range(2):
        client.post(reverse("setup"), _setup_form(setup_code="wrong"))

    blocked = client.post(reverse("setup"), _setup_form())

    assert get_user_model().objects.count() == 0
    assert b"Setup could not be completed" in blocked.content
    assert LoginThrottle.objects.count() == 1
    assert LoginThrottle.objects.get().blocked_until is not None


@pytest.mark.django_db
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_setup_code_is_absent_from_logs(caplog):
    caplog.set_level(logging.DEBUG)
    Client().post(reverse("setup"), _setup_form(setup_code="wrong-synthetic-code"))

    assert SYNTHETIC_SETUP_CODE not in caplog.text
    assert "wrong-synthetic-code" not in caplog.text


@pytest.mark.django_db(transaction=True)
@override_settings(SETUP_CODE=SYNTHETIC_SETUP_CODE)
def test_two_concurrent_setup_submissions_create_exactly_one_member():
    if connection.vendor != "postgresql":
        pytest.skip("concurrent first-member creation is serialized with a PostgreSQL advisory lock")

    barrier = threading.Barrier(2)
    statuses = []
    errors = []

    def submit(username):
        try:
            barrier.wait(timeout=10)
            client = Client()
            response = client.post(reverse("setup"), _setup_form(username=username))
            statuses.append(response.status_code)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    first = threading.Thread(target=submit, args=("first-a",))
    second = threading.Thread(target=submit, args=("first-b",))
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert get_user_model().objects.count() == 1
    assert Person.objects.count() == 1
    assert RecoveryCode.objects.count() == 8
    assert sorted(statuses) == [200, 404]
