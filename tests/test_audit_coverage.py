"""Coverage matrix for household access, account lifecycle and sensitive settings audit events."""
from datetime import date
from decimal import Decimal

import pytest
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import save_alert_settings
from finance.ai_plan import set_offer_plan_links
from finance.ai_services import (
    connect_api_key,
    connect_harness,
    disconnect_api_key,
    disconnect_harness,
    set_api_defaults,
    set_offer_local_chat,
    set_offer_local_to_household,
    set_shared_local_use,
)
from finance.audit_models import ACTION_SPECS
from finance.audit_services import events_for
from finance.auth_services import accept_invitation, create_invitation, recover_account, seed_first_household
from finance.bills_calendar import save_calendar_settings
from finance.google_auth import disconnect_google_account, remove_member_password
from finance.lifecycle_services import (
    leave_household,
    rename_account,
    update_debt_terms,
)
from finance.models import Account, AuditEvent, Household, Membership, MemberSession, Passkey, Person, RecoveryCode
from finance.pairing_services import set_loan_secured_asset
from finance.passkey_services import set_require_passkey_after_password
from finance.policy_services import accept_policy, current_policy, decline_policy
from finance.security_services import revoke_other_sessions_for, revoke_session_for
from tests.helpers import stamp_recent_auth
from tests.test_chat import harness  # noqa: F401 - fixture
from tests.test_audit import account_for, shared_members
from tests.test_auth_flows import PASSWORD, make_member
from tests.test_google_auth import GOOGLE_SUB, _google_settings
from tests.test_simplefin import account_payload, connect_owner, make_account

pytestmark = pytest.mark.django_db

CANARY = "canary-secret-9f3a"


def rows(person, action):
    return list(events_for(person, action=action))


def only(person, action):
    found = rows(person, action)
    assert len(found) == 1, (action, len(found))
    return found[0]


def test_every_action_has_a_matrix_entry_with_a_known_audience():
    assert set(ACTION_SPECS) == set(AuditEvent.Action)
    assert {audience for _t, audience, _f in ACTION_SPECS.values()} == {"account", "deletion", "personal", "household"}


def test_account_lifecycle_events_record_changed_field_names_only():
    _user, person, household, _other_user, other = shared_members()
    private = account_for(person)
    rename_account(person, private.pk, "Renamed secret account")
    only(person, "account_renamed")
    rename_account(person, private.pk, "Renamed secret account")  # no-op
    assert len(rows(person, "account_renamed")) == 1
    private.account_type = Account.Type.CREDIT_CARD
    private.save(update_fields=("account_type",))
    update_debt_terms(person, private.pk, apr_percent=Decimal("19.5"), minimum_payment_minor=2500, payment_day=None)
    event = only(person, "debt_terms_changed")
    assert event.changed_fields == ["apr_percent", "minimum_payment"]
    update_debt_terms(person, private.pk, apr_percent=Decimal("19.5"), minimum_payment_minor=2500, payment_day=None)
    assert len(rows(person, "debt_terms_changed")) == 1
    assert "Renamed" not in str(list(AuditEvent.objects.values()))


def test_account_creation_by_view_and_loan_pairing():
    user, person, household = make_member("creator")
    client = Client()
    client.force_login(user)
    client.post(reverse("account-list"), {"name": "Synthetic", "account_type": "checking", "sharing": "private"})
    client.post(reverse("account-list"), {"name": "Synthetic shared", "account_type": "savings", "sharing": "co_owned"})
    created = rows(person, "account_created")
    assert len(created) == 2
    assert {event.actor_id for event in created} == {person.pk}
    assert {event.target_id for event in created} == set(Account.objects.values_list("pk", flat=True))
    loan = Account.objects.create(owner=person, name="Loan", account_type="loan", currency="USD", scope="private")
    asset = Account.objects.create(owner=person, name="House", account_type="real_estate", currency="USD", scope="private")
    set_loan_secured_asset(person, loan.pk, asset.pk)
    only(person, "loan_pairing_changed")
    set_loan_secured_asset(person, loan.pk, asset.pk)
    assert len(rows(person, "loan_pairing_changed")) == 1
    set_loan_secured_asset(person, loan.pk, None)
    assert len(rows(person, "loan_pairing_changed")) == 2


def test_invitation_setup_and_leave_distinguish_initiator_from_affected_member():
    user, person, household = make_member("host")
    shared = account_for(person, household)
    code = create_invitation(person)
    created = only(person, "invitation_created")
    assert created.actor_id == person.pk and created.household_id == household.pk
    new_user, _codes = accept_invitation(code, "newbie", "New Person", PASSWORD)
    newcomer = new_user.person
    accepted = only(person, "invitation_accepted")
    assert accepted.target_id == created.target_id
    assert accepted.actor_id == newcomer.pk and accepted.affected_member_id == newcomer.pk
    assert accepted.correlation_id != created.correlation_id
    assert only(newcomer, "invitation_accepted").pk == accepted.pk
    leave_household(person)
    left = only(newcomer, "member_left")
    assert left.actor_id == person.pk and left.affected_member_id == person.pk
    assert left.target_id == person.pk
    owner_change = only(newcomer, "account_owner_changed")
    assert owner_change.target_id == shared.pk and owner_change.changed_fields == ["owner"]
    assert owner_change.actor_id == person.pk
    # The former member keeps no household-wide view.
    assert not rows(person, "member_left")
    assert not rows(person, "invitation_created")


def test_first_member_setup_creates_household_event():
    user, _codes = seed_first_household("first", "First Person", "Synthetic Household", PASSWORD)
    event = only(user.person, "household_created")
    assert event.actor_id == user.person.pk and event.target_type == "household"


def test_sole_member_leave_makes_accounts_private_to_the_leaver():
    _user, person, household = make_member("solo")
    account_for(person, household)
    leave_household(person)
    event = only(person, "account_unshared")
    assert event.changed_fields == ["scope", "share_mode"]


def test_household_events_do_not_reach_other_households():
    _user, person, household = make_member("a")
    _other_user, outsider, _other_household = make_member("b")
    create_invitation(person)
    assert not events_for(outsider).exists()
    assert events_for(person).count() == 1


@_google_settings()
def test_sign_in_methods_are_audited_without_secrets():
    user, person, _household = make_member("signin")
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    client.post(reverse("account-settings"), {"action": "add-password", "password1": CANARY + "-Bb2!y", "password2": CANARY + "-Bb2!y"})
    assert len(rows(person, "password_changed")) == 1
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    assert disconnect_google_account(user)
    only(person, "google_disconnected")
    SocialAccount.objects.create(user=user, provider="google", uid=GOOGLE_SUB, extra_data={"sub": GOOGLE_SUB})
    assert remove_member_password(user)
    only(person, "password_removed")
    fresh = Client()
    fresh.force_login(get_user_model().objects.get(pk=user.pk))
    stamp_recent_auth(fresh)
    fresh.post(reverse("account-settings"), {"action": "add-password", "password1": CANARY + "-Aa1!x", "password2": CANARY + "-Aa1!x"})
    assert len(rows(person, "password_added")) == 1
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_recovery_password_reset_and_failed_recovery():
    user, person, _household = make_member("recoverer")
    RecoveryCode.objects.all().delete()
    from finance.auth_services import create_recovery_codes

    code = create_recovery_codes(user, 1)[0]
    with pytest.raises(Exception):
        recover_account("recoverer", "wrong-code", "New-Password-1!")
    assert not rows(person, "password_changed")
    recover_account("recoverer", code, "New-Password-1!")
    only(person, "password_changed")


def test_passkey_requirement_sessions_and_privacy_choices():
    user, person, _household = make_member("security")
    Passkey.objects.create(member=person, name="Synthetic", credential_id=b"cred", public_key=b"key")
    set_require_passkey_after_password(person, True)
    assert only(person, "passkey_requirement_changed").changed_fields == ["require_passkey"]
    set_require_passkey_after_password(person, True)
    assert len(rows(person, "passkey_requirement_changed")) == 1
    session = MemberSession.objects.create(member=person, session_key="synthetic-key-one")
    MemberSession.objects.create(member=person, session_key="synthetic-key-two")
    revoke_session_for(person, session.pk)
    revoked = only(person, "session_revoked")
    assert revoked.target_type == "session" and revoked.target_id == session.pk
    revoke_other_sessions_for(person, "synthetic-key-two")
    assert only(person, "other_sessions_revoked").target_id == person.pk
    version = current_policy()
    accept_policy(person, version)
    accept_policy(person, version)
    assert len(rows(person, "privacy_accepted")) == 1
    decline_policy(person, version)
    decline_policy(person, version)
    assert len(rows(person, "privacy_declined")) == 1


def test_simplefin_connection_and_link_changes_are_audited(monkeypatch):
    from finance.simplefin_services import disconnect_connection, save_account_links, sync_connection  # noqa: F401

    _user, owner, household = make_member("bank")
    checking = make_account(owner)
    connection = connect_owner(owner, monkeypatch, account_payload())
    assert only(owner, "simplefin_connected").target_id == connection.pk
    link = {"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk,
            "cutover_date": date(2026, 3, 1)}
    save_account_links(owner, connection.pk, [link])
    assert only(owner, "simplefin_links_changed").changed_fields == ["account_links"]
    save_account_links(owner, connection.pk, [link])
    assert len(rows(owner, "simplefin_links_changed")) == 1
    save_account_links(owner, connection.pk, [{**link, "cutover_date": date(2026, 4, 1)}])
    assert rows(owner, "simplefin_links_changed")[0].changed_fields == ["cutover"]
    save_account_links(owner, connection.pk, [{
        "simplefin_account_id": "CON-1:sf-checking", "action": "create", "name": "Synthetic new",
        "account_type": "checking", "sharing": "private", "cutover_date": date(2026, 4, 1)}])
    assert len(rows(owner, "account_created")) == 1
    disconnect_connection(owner, connection.pk)
    assert only(owner, "simplefin_disconnected").target_id == connection.pk
    text = str(list(AuditEvent.objects.values()))
    assert "2026-03" not in text and "2026-04" not in text and "Synthetic new" not in text


def test_simplefin_denied_target_appends_nothing(monkeypatch):
    from finance.simplefin_services import disconnect_connection

    _user, owner, _household = make_member("owner")
    _other_user, other, _other_household = make_member("other")
    connection = connect_owner(owner, monkeypatch, account_payload())
    before = AuditEvent.objects.count()
    with pytest.raises(PermissionDenied):
        disconnect_connection(other, connection.pk)
    assert AuditEvent.objects.count() == before


def test_notification_and_calendar_settings_record_group_names_only():
    _user, person, _household = make_member("notices")
    payload = dict(
        sync_enabled=True, recurring_price_enabled=True, recurring_missed_enabled=True, budget_enabled=True,
        large_transaction_enabled=False, monthly_review_enabled=True, monthly_review_ai_enabled=True,
        large_transaction_minor=None,
    )
    save_alert_settings(person, **payload)  # defaults; may match saved row
    baseline = len(rows(person, "notification_preferences_changed"))
    save_alert_settings(person, **{**payload, "budget_enabled": False})
    assert len(rows(person, "notification_preferences_changed")) == baseline + 1
    assert rows(person, "notification_preferences_changed")[0].changed_fields == ["alert_toggles"]
    save_alert_settings(person, **{**payload, "budget_enabled": False, "large_transaction_minor": 123456})
    assert rows(person, "notification_preferences_changed")[0].changed_fields == ["thresholds"]
    save_alert_settings(person, **{**payload, "budget_enabled": False, "large_transaction_minor": 123456})
    assert len(rows(person, "notification_preferences_changed")) == baseline + 2
    save_calendar_settings(person, account_ids=[], threshold_minor=5000)
    assert only(person, "bills_calendar_changed").changed_fields == ["threshold"]
    save_calendar_settings(person, account_ids=[], threshold_minor=5000)
    assert len(rows(person, "bills_calendar_changed")) == 1
    assert "123456" not in str(list(AuditEvent.objects.values()))


def test_notification_address_change_by_view(settings):
    settings.EMAIL_HOST = "mail.invalid"
    settings.DEFAULT_FROM_EMAIL = "planner@example.invalid"
    settings.ALERT_EMAIL_BASE_URL = "https://planner.example.invalid"
    user, person, _household = make_member("mailer")
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    body = {"action": "save-email", "email_enabled": "on", "notification_email": f"{CANARY}@example.invalid"}
    client.post(reverse("settings-alerts"), body)
    assert only(person, "notification_address_changed").changed_fields == ["email_enabled", "notification_email"]
    client.post(reverse("settings-alerts"), body)
    assert len(rows(person, "notification_address_changed")) == 1
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_ai_api_key_events(settings):
    user, person, household = make_member("keys")
    accept_policy(person, current_policy())
    key = "sk-ant-synthetic-" + CANARY
    connect_api_key(person, kind="anthropic_api", key=key)
    only(person, "ai_key_connected")
    connect_api_key(person, kind="anthropic_api", key=key + "2")
    only(person, "ai_key_replaced")
    set_api_defaults(person, kind="anthropic_api", chat_model="", background_model="", use_chat=True, use_background=False)
    assert only(person, "ai_defaults_changed").changed_fields == ["use_for_chat"]
    set_api_defaults(person, kind="anthropic_api", chat_model="", background_model="", use_chat=True, use_background=False)
    assert len(rows(person, "ai_defaults_changed")) == 1
    disconnect_api_key(person, "anthropic_api")
    only(person, "ai_key_disconnected")
    disconnect_api_key(person, "anthropic_api")
    assert len(rows(person, "ai_key_disconnected")) == 1
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_ai_harness_offers_and_shared_local_choices(harness):  # noqa: F811 - fixture
    from tests.test_chat import TOKEN, make_member as make_ai_member

    _state, url = harness
    user, host, household = make_ai_member("host")
    _user2, guest, _ = make_ai_member("guest", household, policy=current_policy())
    connect_harness(host, base_url=url, token=TOKEN)
    only(host, "ai_harness_connected")
    set_offer_local_to_household(host, True)
    offer = only(guest, "ai_local_offer_changed")
    assert offer.changed_fields == ["offer_local_to_household"] and offer.household_id == household.pk
    set_offer_local_to_household(host, True)
    set_offer_local_chat(host, True)
    assert len(rows(guest, "ai_local_offer_changed")) == 2
    set_offer_plan_links(host, True)
    assert only(guest, "plan_link_offer_changed").changed_fields == ["offer_plan_links"]
    set_shared_local_use(guest, chat=True, background=False)
    choice = only(guest, "ai_shared_local_changed")
    assert choice.changed_fields == ["use_shared_local_chat"]
    assert not rows(host, "ai_shared_local_changed")  # a personal choice stays personal
    set_offer_local_to_household(host, False)
    assert len(rows(guest, "ai_local_offer_changed")) == 3
    disconnect_harness(host)
    only(host, "ai_harness_disconnected")
    assert not rows(guest, "ai_harness_disconnected")
    assert TOKEN not in str(list(AuditEvent.objects.values()))


def test_rollback_removes_event_and_state_together():
    _user, person, _household = make_member("rollback")
    account = account_for(person)
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            rename_account(person, account.pk, "Rolled back")
            raise RuntimeError
    account.refresh_from_db()
    assert account.name != "Rolled back"
    assert not rows(person, "account_renamed")


def test_denied_and_stale_requests_append_nothing():
    _user, person, household = make_member("owner")
    _other_user, outsider, _ = make_member("outsider")
    account = account_for(person, household)
    with pytest.raises(PermissionDenied):
        rename_account(outsider, account.pk, "Hijack")
    with pytest.raises(PermissionDenied):
        leave_household(Person.objects.create(user=get_user_model().objects.create_user("loner", password=PASSWORD), display_name="Loner"))
    assert not AuditEvent.objects.exists()


def test_member_deletion_removes_personal_events_and_anonymizes_household_events():
    from finance.lifecycle_services import delete_member_data

    user, person, household = make_member("deleter")
    other_user, other, _ = make_member("housemate")
    Membership.objects.filter(person=other).update(ended_at=timezone.now())
    Membership.objects.create(person=other, household=household)
    create_invitation(person)
    accept_policy(person, current_policy())
    person_id = person.pk
    delete_member_data(person)
    assert not AuditEvent.objects.filter(private_owner_id=person_id).exists()
    event = only(other, "invitation_created")
    assert event.actor_id is None and event.affected_member_id is None
    assert Household.objects.filter(pk=household.pk).exists()


def test_google_connect_signal_records_only_explicit_connects_and_eviction_adds_no_operator_event():
    from types import SimpleNamespace

    from django.core.management import call_command

    from finance.apps import record_google_connected

    user, person, household = make_member("linker")
    record_google_connected(None, None, SimpleNamespace(state={"process": "login"}, user=user))
    assert not rows(person, "google_connected")
    record_google_connected(None, None, SimpleNamespace(state={"process": "connect"}, user=user))
    only(person, "google_connected")
    _user, other, _ = make_member("evictee")
    Membership.objects.filter(person=other).update(ended_at=timezone.now())
    Membership.objects.create(person=other, household=household)
    before = AuditEvent.objects.count()
    call_command("evict_household_member", username=other.user.username)
    assert AuditEvent.objects.count() == before
    assert not AuditEvent.objects.filter(actor=other).exists()
