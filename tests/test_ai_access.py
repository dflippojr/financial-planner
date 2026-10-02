import io
import logging
import zipfile
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth

from finance.ai_services import connect_harness, connection_for, run_conversation, run_structured
from finance.ai_tools import default_tools, list_accounts, list_transactions
from finance.ai_types import AUTHORIZATION_REQUIRED
from finance.export import write_export_zip
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction
from finance.policy_services import accept_policy, household_ai_allowed, publish_policy


PASSWORD = "Synthetic-passphrase-42!"
TOKEN = "ha-synthetic-app-token"


def make_user(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    return user, person


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.django_db
def test_member_a_tools_never_return_member_b_private_data(harness):
    _state, url = harness
    user_a, person_a = make_user("alpha")
    user_b, person_b = make_user("beta")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person_a, household=household)
    Membership.objects.create(person=person_b, household=household)
    policy = publish_policy(material=True, body="Synthetic household policy")
    accept_policy(person_a, policy)
    accept_policy(person_b, policy)
    assert household_ai_allowed(household)
    shared = Account.objects.create(
        name="Shared Checking",
        account_type=Account.Type.CHECKING,
        owner=person_a,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    private_b = Account.objects.create(
        name="Beta Private",
        account_type=Account.Type.CHECKING,
        owner=person_b,
        scope=Account.Scope.PRIVATE,
    )
    private_batch = ImportBatch.objects.create(
        account=private_b,
        imported_by=person_b,
        source="huntington",
        source_file_sha256="c" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    shared_batch = ImportBatch.objects.create(
        account=shared,
        imported_by=person_a,
        source="huntington",
        source_file_sha256="d" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    Transaction.objects.create(
        account=private_b,
        import_batch=private_batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-999,
        currency="USD",
        description="Beta private grocery",
        kind=Transaction.Kind.CASH_FLOW,
        fingerprint="b" * 64,
        original_fields={"synthetic": "beta"},
        source_row_number=1,
    )
    Transaction.objects.create(
        account=shared,
        import_batch=shared_batch,
        transaction_date=date(2026, 1, 3),
        amount_minor=-500,
        currency="USD",
        description="Shared rent",
        kind=Transaction.Kind.CASH_FLOW,
        fingerprint="a" * 64,
        original_fields={"synthetic": "shared"},
        source_row_number=1,
    )
    connect_harness(person_a, base_url=url, token=TOKEN)
    accounts = list_accounts(person_a, {})
    txns = list_transactions(person_a, {})
    assert "Shared Checking" in accounts
    assert "Beta Private" not in accounts
    assert "Shared rent" in txns
    assert "Beta private grocery" not in txns
    result = run_conversation(
        person_a,
        "List my accounts",
        feature="chat",
        backend="local",
        tools=default_tools(),
    )
    assert result.ok
    assert "Beta private grocery" not in (result.answer or "")


@pytest.mark.django_db
def test_member_b_cannot_use_member_a_connection(harness):
    state, url = harness
    _user_a, person_a = make_user("alpha")
    _user_b, person_b = make_user("beta")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person_a, household=household)
    Membership.objects.create(person=person_b, household=household)
    policy = publish_policy(material=True, body="Synthetic household policy")
    accept_policy(person_a, policy)
    accept_policy(person_b, policy)
    connect_harness(person_a, base_url=url, token=TOKEN)
    assert connection_for(person_b) is None
    before = list(state.requests)
    result = run_structured(person_b, "synthetic", feature="structured")
    assert not result.ok
    assert result.failure_code == AUTHORIZATION_REQUIRED
    assert state.requests == before


@pytest.mark.django_db
def test_token_never_appears_in_pages_logs_export_or_errors(harness, caplog):
    _state, url = harness
    user, person = make_user("owner")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    policy = publish_policy(material=True, body="Synthetic policy")
    accept_policy(person, policy)
    caplog.set_level(logging.DEBUG)
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    page = client.get(reverse("account-settings"))
    html = page.content.decode()
    assert TOKEN not in html
    export = write_export_zip(person)
    with zipfile.ZipFile(io.BytesIO(export)) as archive:
        names = set(archive.namelist())
        blob = b"".join(archive.read(name) for name in names)
    assert TOKEN.encode() not in blob
    assert "ai_connections.csv" in names
    assert "ai_usage.csv" in names
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
    with pytest.raises(Exception) as caught:
        connect_harness(person, base_url="https://example.invalid", token=TOKEN)
    assert TOKEN not in str(caught.value)
    error_page = client.post(reverse("ai-connect"), {"base_url": "https://example.invalid", "token": TOKEN})
    assert TOKEN not in error_page.content.decode()
