from datetime import date, datetime, timedelta
from datetime import timezone as dt_utc
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.category_services import ensure_household_categories, exclusion_exists_for
from finance.csv_import.services import undo_import_batch
from finance.encryption import decrypt_access_url, encrypt_access_url
from finance.models import (
    Account,
    AccountLink,
    BalanceSnapshot,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringSeries,
    SimpleFinConnection,
    Transaction,
    TransferPair,
)
from finance.recurring_services import refresh_recurring_series
from finance.simplefin_errors import CLAIM_COMPROMISED, SimpleFinError
from finance.simplefin_services import (
    claim_connection,
    save_account_links,
    sync_all_connections,
    sync_connection,
)
from finance.simplefin_client import claim_access_url

PASSWORD = "Synthetic-passphrase-42!"
ACCESS_URL = "https://demo:synthetic-access-secret@bridge.example.test/simplefin"
CLAIM_URL = "https://bridge.example.test/simplefin/claim/synthetic-demo"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def setup_token():
    import base64

    return base64.b64encode(CLAIM_URL.encode("utf-8")).decode("ascii")


def epoch(year, month, day, hour=15):
    return int(datetime(year, month, day, hour, tzinfo=dt_utc.utc).timestamp())


def account_payload(*, account_id="sf-checking", transactions=None, extra=None, currency="USD"):
    return {
        "errlist": [],
        "connections": [
            {
                "conn_id": "CON-1",
                "name": "Synthetic Bank - Pat",
                "org_id": "ORG-1",
                "sfin_url": "https://bank.example.test/simplefin",
            }
        ],
        "accounts": [
            {
                "id": account_id,
                "name": "Synthetic Checking",
                "conn_id": "CON-1",
                "currency": currency,
                "balance": "100.23",
                "balance-date": epoch(2026, 3, 20),
                "transactions": transactions or [],
                "extra": extra or {},
            }
        ],
    }


def posted_txn(*, txn_id, day, amount, pending=False, posted=None):
    item = {
        "id": txn_id,
        "posted": posted if posted is not None else epoch(2026, 3, day),
        "amount": amount,
        "description": "Synthetic Stream",
    }
    if pending:
        item["pending"] = True
    return item


def connect_owner(owner, monkeypatch, payload=None):
    monkeypatch.setattr("finance.simplefin_services.claim_access_url", lambda url: ACCESS_URL)
    payload = payload or account_payload()
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: payload)
    return claim_connection(owner, setup_token())


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_claim_success_encrypts_access_url(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    owner = make_person("owner")
    make_household(owner)
    captured = []

    def fake_claim(url):
        captured.append(url)
        return ACCESS_URL

    monkeypatch.setattr("finance.simplefin_services.claim_access_url", fake_claim)
    connection = claim_connection(owner, setup_token())

    assert captured == [CLAIM_URL]
    assert bytes(connection.encrypted_access_url) != ACCESS_URL.encode()
    assert ACCESS_URL not in bytes(connection.encrypted_access_url).decode("latin1", errors="ignore")
    assert decrypt_access_url(connection.encrypted_access_url) == ACCESS_URL
    assert ACCESS_URL not in str(connection)
    assert ACCESS_URL not in caplog.text


@pytest.mark.django_db
def test_claim_403_on_client_reports_compromise(monkeypatch):
    owner = make_person("owner")
    make_household(owner)

    def fake_open(*args, **kwargs):
        raise HTTPError(CLAIM_URL, 403, "Forbidden", hdrs=None, fp=BytesIO())

    monkeypatch.setattr("finance.simplefin_client.urlopen", fake_open)
    with pytest.raises(SimpleFinError) as raised:
        claim_access_url(CLAIM_URL)
    assert str(raised.value) == CLAIM_COMPROMISED
    assert CLAIM_URL not in str(raised.value)


@pytest.mark.django_db
def test_claim_403_does_not_store_a_connection(monkeypatch):
    owner = make_person("owner")
    make_household(owner)

    def fake_open(*args, **kwargs):
        raise HTTPError(CLAIM_URL, 403, "Forbidden", hdrs=None, fp=BytesIO())

    monkeypatch.setattr("finance.simplefin_client.urlopen", fake_open)
    token = setup_token()
    with pytest.raises(SimpleFinError) as wrapped:
        claim_connection(owner, token)
    assert ACCESS_URL not in str(wrapped.value)
    assert SimpleFinConnection.objects.count() == 0


@pytest.mark.django_db
def test_connections_page_never_shows_access_url(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    connect_owner(owner, monkeypatch)
    client = signed_in(owner)
    response = client.get(reverse("simplefin-connections"))
    body = response.content.decode()

    assert response.status_code == 200
    assert ACCESS_URL not in body
    assert "synthetic-access-secret" not in body
    assert "Synthetic Bank - Pat" in body


@pytest.mark.django_db
def test_sync_dedupes_repeated_runs_and_skips_pending_and_cutover(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    Transaction.objects.create(
        account=checking,
        import_batch=ImportBatch.objects.create(
            account=checking,
            imported_by=owner,
            source=ImportBatch.Source.HUNTINGTON,
            source_file_sha256="a" * 64,
            date_range_start=date(2026, 1, 1),
            date_range_end=date(2026, 1, 31),
        ),
        transaction_date=date(2026, 3, 10),
        amount_minor=-500,
        description="Synthetic prior CSV",
        source_row_number=1,
        fingerprint="c" * 64,
        original_fields={"Synthetic Amount": "-5.00"},
    )
    payload = account_payload(
        transactions=[
            posted_txn(txn_id="old", day=9, amount="-1.00"),
            posted_txn(txn_id="pending-1", day=16, amount="-2.00", pending=True, posted=0),
            posted_txn(txn_id="new-1", day=16, amount="-3.00"),
        ]
    )
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            }
        ],
    )
    first = sync_connection(owner, connection.pk, ignore_rate_limit=True)
    second = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    ids = list(
        Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).values_list(
            "source_transaction_id", flat=True
        )
    )
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert ids.count("new-1") == 1
    assert "old" not in ids
    assert "pending-1" not in ids
    snapshot = BalanceSnapshot.objects.get(account=checking, source=BalanceSnapshot.Source.SIMPLEFIN)
    assert snapshot.amount_minor == 10023
    assert snapshot.currency == "USD"


@pytest.mark.django_db
def test_balances_only_ignores_transactions(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    investment = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    payload = account_payload(
        account_id="sf-invest",
        transactions=[posted_txn(txn_id="invest-1", day=16, amount="-99.00")],
    )
    payload["accounts"][0]["name"] = "Synthetic Brokerage"
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-invest",
                "action": "link",
                "account_id": investment.pk,
            }
        ],
    )
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert AccountLink.objects.get(account=investment).mode == AccountLink.Mode.BALANCES_ONLY
    assert not Transaction.objects.filter(account=investment).exists()
    assert BalanceSnapshot.objects.filter(account=investment).exists()


@pytest.mark.django_db
def test_sync_pairs_transfers_and_refreshes_recurring(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    payload = {
        "errlist": [],
        "connections": [{"conn_id": "CON-1", "name": "Synthetic Bank - Pat", "org_id": "ORG-1", "sfin_url": "https://bank.example.test/simplefin"}],
        "accounts": [
            {
                "id": "sf-checking",
                "name": "Synthetic Checking",
                "conn_id": "CON-1",
                "currency": "USD",
                "balance": "10.00",
                "balance-date": epoch(2026, 4, 20),
                "transactions": [
                    {
                        "id": "out-1",
                        "posted": epoch(2026, 4, 10),
                        "amount": "-25.00",
                        "description": "Synthetic to savings",
                    },
                    posted_txn(txn_id="sub-1", day=None, amount="-15.99", posted=epoch(2026, 1, 15)),
                    posted_txn(txn_id="sub-2", day=None, amount="-15.99", posted=epoch(2026, 2, 15)),
                    posted_txn(txn_id="sub-3", day=None, amount="-15.99", posted=epoch(2026, 3, 15)),
                ],
            },
            {
                "id": "sf-savings",
                "name": "Synthetic Savings",
                "conn_id": "CON-1",
                "currency": "USD",
                "balance": "40.00",
                "balance-date": epoch(2026, 4, 20),
                "transactions": [
                    {
                        "id": "in-1",
                        "posted": epoch(2026, 4, 10),
                        "amount": "25.00",
                        "description": "Synthetic from checking",
                    }
                ],
            },
        ],
    }
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk, "cutover_date": date(2026, 1, 1)},
            {"simplefin_account_id": "CON-1:sf-savings", "action": "link", "account_id": savings.pk, "cutover_date": date(2026, 1, 1)},
        ],
    )
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    outbound = Transaction.objects.get(source_transaction_id="out-1")
    inbound = Transaction.objects.get(source_transaction_id="in-1")

    assert exclusion_exists_for(outbound)
    assert TransferPair.objects.filter(leg_a__in=(outbound, inbound)).exists() or TransferPair.objects.filter(
        leg_b__in=(outbound, inbound)
    ).exists()
    refresh_recurring_series(owner)
    assert RecurringSeries.objects.filter(person=owner, is_active=True).exists()


@pytest.mark.django_db
def test_undo_sync_batch_archives_transactions_and_removes_snapshot(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(transactions=[posted_txn(txn_id="new-1", day=16, amount="-3.00")])
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk, "cutover_date": date(2026, 3, 1)}],
    )
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    batch = ImportBatch.objects.get(account=checking, source=ImportBatch.Source.SIMPLEFIN, status=ImportBatch.Status.ACTIVE)
    undo_import_batch(owner, checking.pk, batch.pk)

    assert not Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE, source_transaction_id="new-1").exists()
    assert not BalanceSnapshot.objects.filter(account=checking).exists()
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.ARCHIVED


@pytest.mark.django_db
def test_scheduler_skips_when_no_connection(capsys):
    call_command("sync_simplefin")
    assert "No SimpleFIN connections." in capsys.readouterr().out
    assert sync_all_connections() == 0


@pytest.mark.django_db
def test_sync_now_rate_limit(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    connection = connect_owner(owner, monkeypatch, account_payload(transactions=[]))
    save_account_links(
        owner,
        connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk}],
    )
    sync_connection(owner, connection.pk)
    with pytest.raises(SimpleFinError, match="15 minutes"):
        sync_connection(owner, connection.pk)


@pytest.mark.django_db
def test_access_rules_hide_another_members_connection(monkeypatch):
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    make_household(owner, member)
    make_household(outsider, name="Other Household")
    connect_owner(owner, monkeypatch)
    owner_page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()
    member_page = signed_in(member).get(reverse("simplefin-connections")).content.decode()
    outsider_page = signed_in(outsider).get(reverse("simplefin-connections")).content.decode()
    anonymous = Client().get(reverse("simplefin-connections"))

    assert "Synthetic Bank - Pat" in owner_page
    assert "Synthetic Bank - Pat" not in member_page
    assert "Synthetic Bank - Pat" not in outsider_page
    assert ACCESS_URL not in owner_page + member_page + outsider_page
    assert anonymous.status_code == 302
    assert SimpleFinConnection.objects.filter(owner=member).count() == 0


@pytest.mark.django_db
def test_rejects_custom_currency(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(currency="https://example.test/points")
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk}],
    )
    with pytest.raises(SimpleFinError, match="currency"):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)


@pytest.mark.django_db
def test_disconnect_keeps_imported_rows(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(transactions=[posted_txn(txn_id="keep-1", day=16, amount="-3.00")])
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk, "cutover_date": date(2026, 3, 1)}],
    )
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    from finance.simplefin_services import disconnect_connection

    disconnect_connection(owner, connection.pk)
    assert not SimpleFinConnection.objects.filter(pk=connection.pk).exists()
    assert Transaction.objects.filter(source_transaction_id="keep-1").exists()


@pytest.mark.django_db
def test_encrypt_roundtrip_does_not_embed_plaintext():
    token = encrypt_access_url(ACCESS_URL)
    assert ACCESS_URL.encode() not in token
    assert decrypt_access_url(token) == ACCESS_URL


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_claim_and_fetch_http_paths(monkeypatch):
    from finance.simplefin_client import fetch_accounts
    from finance.simplefin_errors import provider_errors
    from finance.simplefin_schedule import next_scheduled_sync, parse_five_field_cron
    from urllib.error import URLError

    monkeypatch.setattr(
        "finance.simplefin_client.urlopen",
        lambda *args, **kwargs: _FakeResponse(ACCESS_URL.encode()),
    )
    assert claim_access_url(CLAIM_URL) == ACCESS_URL

    monkeypatch.setattr(
        "finance.simplefin_client.urlopen",
        lambda *args, **kwargs: _FakeResponse(b'{"accounts": []}'),
    )
    assert fetch_accounts(ACCESS_URL, start_date=1, end_date=2, balances_only=True) == {"accounts": []}

    def boom(*args, **kwargs):
        raise HTTPError(ACCESS_URL, 402, "Payment Required", hdrs=None, fp=BytesIO())

    monkeypatch.setattr("finance.simplefin_client.urlopen", boom)
    with pytest.raises(SimpleFinError, match="payment"):
        fetch_accounts(ACCESS_URL)

    def denied(*args, **kwargs):
        raise HTTPError(ACCESS_URL, 403, "Forbidden", hdrs=None, fp=BytesIO())

    monkeypatch.setattr("finance.simplefin_client.urlopen", denied)
    with pytest.raises(SimpleFinError, match="denied"):
        fetch_accounts(ACCESS_URL)

    def other(*args, **kwargs):
        raise HTTPError(ACCESS_URL, 500, "Error", hdrs=None, fp=BytesIO())

    monkeypatch.setattr("finance.simplefin_client.urlopen", other)
    with pytest.raises(SimpleFinError, match="could not return"):
        fetch_accounts(ACCESS_URL)

    monkeypatch.setattr("finance.simplefin_client.urlopen", lambda *a, **k: (_ for _ in ()).throw(URLError("offline")))
    with pytest.raises(SimpleFinError, match="could not be reached"):
        fetch_accounts(ACCESS_URL)

    monkeypatch.setattr("finance.simplefin_client.urlopen", lambda *a, **k: _FakeResponse(b"not-json"))
    with pytest.raises(SimpleFinError, match="could not be read"):
        fetch_accounts(ACCESS_URL)

    monkeypatch.setattr("finance.simplefin_client.urlopen", lambda *a, **k: _FakeResponse(b"[1]"))
    with pytest.raises(SimpleFinError, match="could not be read"):
        fetch_accounts(ACCESS_URL)

    with pytest.raises(SimpleFinError, match="HTTPS"):
        claim_access_url("http://bridge.example.test/claim")

    msgs = provider_errors(
        {
            "errlist": [
                {"code": "con.auth", "msg": 'Re-auth <b>Huntington</b> at https://secret.example/path'},
                "plain errlist",
            ],
            "errors": ["deprecated https://also.example"],
        }
    )
    assert msgs[0] == "Re-auth Huntington at [redacted]"
    assert "secret" not in "".join(msgs)
    parse_five_field_cron("30 6 * * *")
    nxt = next_scheduled_sync("30 6 * * *", datetime(2026, 10, 1, 6, 0))
    assert nxt.hour == 6
    assert nxt.minute == 30
    with pytest.raises(ValueError):
        parse_five_field_cron("not a cron")
    from finance.simplefin_schedule import cron_matches

    assert cron_matches("0 * * * *", datetime(2026, 10, 1, 6, 1)) is False
    assert cron_matches("*/15 6-7 1,2 * *", datetime(2026, 10, 1, 6, 0))
    assert cron_matches("0 * * * *", datetime(2026, 10, 1, 6, 0))


@pytest.mark.django_db
def test_connections_views_claim_sync_disconnect_and_create(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    monkeypatch.setattr("finance.simplefin_services.claim_access_url", lambda url: ACCESS_URL)
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: account_payload())
    client = signed_in(owner)
    claim = client.post(reverse("simplefin-connections"), {"intent": "claim", "token": setup_token()})
    assert claim.status_code == 302
    assert ACCESS_URL not in claim.content.decode()
    page = client.get(reverse("simplefin-connections"))
    assert b"Synthetic Bank - Pat" in page.content
    assert ACCESS_URL.encode() not in page.content
    saved = client.post(
        reverse("simplefin-connections"),
        {
            "intent": "link",
            "sf_id_0": "CON-1:sf-checking",
            "action_0": "create",
            "name_0": "Linked Checking",
            "account_type_0": Account.Type.CHECKING,
            "sharing_0": Account.Scope.HOUSEHOLD,
            "cutover_0": "2026-03-01",
        },
    )
    assert saved.status_code == 302
    assert Account.objects.filter(name="Linked Checking", scope=Account.Scope.HOUSEHOLD).exists()
    limited = client.post(reverse("simplefin-sync"))
    follow = client.get(limited.url)
    assert b"15 minutes" in follow.content
    gone = client.post(reverse("simplefin-disconnect"))
    assert gone.status_code == 302
    assert not SimpleFinConnection.objects.filter(owner=owner).exists()


@pytest.mark.django_db
def test_sync_all_and_claim_http_failure_on_page(monkeypatch):
    owner = make_person("owner")
    make_household(owner)

    def fail_claim(*args, **kwargs):
        raise HTTPError(CLAIM_URL, 500, "Error", hdrs=None, fp=BytesIO())

    monkeypatch.setattr("finance.simplefin_client.urlopen", fail_claim)
    client = signed_in(owner)
    response = client.post(reverse("simplefin-connections"), {"intent": "claim", "token": setup_token()})
    assert response.status_code == 200
    assert b"could not claim" in response.content
    assert CLAIM_URL.encode() not in response.content
    payload = account_payload(transactions=[posted_txn(txn_id="n1", day=16, amount="-1.00")])
    connection = connect_owner(owner, monkeypatch, payload)
    checking = make_account(owner, name="Other Checking")
    save_account_links(
        owner,
        connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk, "cutover_date": date(2026, 3, 1)}],
    )
    assert sync_all_connections() == 1


def test_fetch_sends_access_url_credentials_as_basic_auth(monkeypatch):
    import base64
    import json as jsonlib

    from finance.simplefin_client import fetch_accounts

    seen = {}

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout=None, context=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(jsonlib.dumps({"accounts": [], "errlist": []}).encode())

    monkeypatch.setattr("finance.simplefin_client.urlopen", fake_open)

    fetch_accounts("https://syn%40user:s3cr%3At@bridge.example.test/simplefin")

    assert seen["url"].startswith("https://bridge.example.test/simplefin/accounts?")
    assert "syn" not in seen["url"]
    assert "s3cr" not in seen["url"]
    assert seen["auth"] == "Basic " + base64.b64encode(b"syn@user:s3cr:t").decode()


@pytest.mark.django_db
def test_failed_sync_state_is_saved_and_only_denied_access_disables(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    connection = connect_owner(owner, monkeypatch)

    def unreachable(*args, **kwargs):
        raise SimpleFinError("SimpleFIN could not be reached.")

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", unreachable)
    with pytest.raises(SimpleFinError):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)
    connection.refresh_from_db()
    assert connection.last_sync_at is not None
    assert connection.last_sync_result == "SimpleFIN could not be reached."
    assert connection.disabled is False

    def denied(*args, **kwargs):
        raise SimpleFinError("SimpleFIN access was denied. Reconnect if access was revoked.", access_denied=True)

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", denied)
    with pytest.raises(SimpleFinError):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)
    connection.refresh_from_db()
    assert connection.disabled is True


@pytest.mark.django_db
def test_same_account_id_at_two_institutions_stays_separate(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    first = make_account(owner, name="Synthetic First")
    second = make_account(owner, name="Synthetic Second")
    payload = account_payload(account_id="acct-1", transactions=[posted_txn(txn_id="t-a", day=10, amount="-10.00")])
    other = dict(payload["accounts"][0])
    other.update({"conn_id": "CON-2", "transactions": [posted_txn(txn_id="t-b", day=11, amount="-20.00")]})
    payload["accounts"].append(other)
    payload["connections"].append({"conn_id": "CON-2", "name": "Synthetic Bank Two", "org_id": "ORG-2"})
    connection = connect_owner(owner, monkeypatch, payload=payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {"simplefin_account_id": "CON-1:acct-1", "action": "link", "account_id": first.pk, "cutover_date": date(2026, 3, 1)},
            {"simplefin_account_id": "CON-2:acct-1", "action": "link", "account_id": second.pk, "cutover_date": date(2026, 3, 1)},
        ],
    )

    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert list(Transaction.objects.filter(account=first).values_list("amount_minor", flat=True)) == [-1000]
    assert list(Transaction.objects.filter(account=second).values_list("amount_minor", flat=True)) == [-2000]


def _linked_owner_and_checking(monkeypatch, *, scope=None, household=None, owner=None, payload=None):
    owner = owner or make_person("owner")
    checking = make_account(owner, scope=scope or Account.Scope.PRIVATE, household=household)
    connection = connect_owner(owner, monkeypatch, payload=payload)
    save_account_links(
        owner,
        connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk, "cutover_date": date(2026, 3, 1)}],
    )
    return owner, checking, connection


@pytest.mark.django_db
def test_unlinked_rows_have_no_cutover_prefill_and_server_defaults_per_account(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    early = make_account(owner, name="Synthetic A Early")
    late = make_account(owner, name="Synthetic B Late")
    for account, day in ((early, date(2026, 6, 30)), (late, date(2026, 9, 30))):
        batch = ImportBatch.objects.create(
            account=account, imported_by=owner, source=ImportBatch.Source.HUNTINGTON,
            source_file_sha256="c" * 64, date_range_start=date(2026, 1, 1), date_range_end=day,
        )
        Transaction.objects.create(
            account=account, import_batch=batch, transaction_date=day, amount_minor=-100,
            description="Synthetic", kind=Transaction.Kind.CASH_FLOW, source_row_number=2,
            fingerprint=f"{account.pk}".ljust(64, "d"), original_fields={},
        )
    connection = connect_owner(owner, monkeypatch)
    page = signed_in(owner).get(reverse("simplefin-connections"))
    assert b'name="cutover_0" value=""' in page.content

    save_account_links(
        owner, connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": late.pk, "cutover_date": None}],
    )

    assert AccountLink.objects.get(account=late).cutover_date == date(2026, 10, 1)


@pytest.mark.django_db
def test_sync_skips_archived_and_no_longer_visible_accounts(monkeypatch):
    from finance.lifecycle_services import archive_account, unshare_account

    payload = account_payload(transactions=[posted_txn(txn_id="t-1", day=10, amount="-5.00")])
    owner, checking, connection = _linked_owner_and_checking(monkeypatch, payload=payload)
    make_household(owner)
    archive_account(owner, checking.pk)

    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 0
    assert not Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).exists()

    member = make_person("member")
    other_owner = make_person("sharer")
    household = make_household(member, other_owner, name="Synthetic Household Two")
    shared = make_account(other_owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    member_connection = connect_owner(member, monkeypatch, payload=payload)
    save_account_links(
        member, member_connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": shared.pk, "cutover_date": date(2026, 3, 1)}],
    )
    Transaction.objects.filter(account=shared).delete()
    unshare_account(other_owner, shared.pk)

    result = sync_connection(member, member_connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 0
    assert not Transaction.objects.filter(account=shared).exists()


@pytest.mark.django_db
def test_linking_one_account_to_two_remote_accounts_is_refused_without_500(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload()
    second = dict(payload["accounts"][0])
    second["id"] = "sf-savings"
    payload["accounts"].append(second)
    connection = connect_owner(owner, monkeypatch, payload=payload)

    cutover = date(2026, 3, 1)
    choices = [
        {"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk, "cutover_date": cutover},
        {"simplefin_account_id": "CON-1:sf-savings", "action": "link", "account_id": checking.pk, "cutover_date": cutover},
    ]
    with pytest.raises(SimpleFinError):
        save_account_links(owner, connection.pk, choices)
    assert AccountLink.objects.filter(account=checking).count() <= 1


@pytest.mark.django_db
def test_export_after_a_sync_includes_the_snapshot(monkeypatch):
    import io
    import json as jsonlib
    import zipfile

    from finance.export import write_export_zip

    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    assert BalanceSnapshot.objects.filter(account=checking).exists()

    archive = zipfile.ZipFile(io.BytesIO(write_export_zip(owner)))
    snapshots = jsonlib.loads(archive.read("balance_snapshots.json"))

    assert snapshots[0]["account_id"] == checking.pk
    assert snapshots[0]["source"] == "simplefin"
    assert snapshots[0]["snapshot_date"]


@pytest.mark.django_db
def test_ignoring_a_linked_account_unlinks_it(monkeypatch):
    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)

    save_account_links(owner, connection.pk, [{"simplefin_account_id": "CON-1:sf-checking", "action": "ignore"}])

    assert not AccountLink.objects.filter(account=checking).exists()


def test_time_zone_follows_the_tz_environment_variable(monkeypatch):
    import importlib

    import financial_planner.settings as settings_module

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-only-secret-key-with-enough-entropy-not-for-production-12345")
    reloaded = importlib.reload(settings_module)
    try:
        assert reloaded.TIME_ZONE == "America/Los_Angeles"
    finally:
        monkeypatch.delenv("TZ")
        importlib.reload(settings_module)


def test_stalled_response_body_becomes_a_safe_error(monkeypatch):
    from finance.simplefin_client import fetch_accounts

    class StallingResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            raise TimeoutError("read timed out")

    monkeypatch.setattr("finance.simplefin_client.urlopen", lambda *a, **k: StallingResponse())

    with pytest.raises(SimpleFinError) as caught:
        fetch_accounts(ACCESS_URL)
    assert "did not finish responding" in str(caught.value)


@pytest.mark.django_db
def test_create_new_account_on_a_linked_row_repoints_the_link(monkeypatch):
    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)

    save_account_links(
        owner, connection.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "create", "name": "Synthetic New", "account_type": Account.Type.CHECKING}],
    )

    link = AccountLink.objects.get(connection=connection, simplefin_account_id="CON-1:sf-checking")
    assert link.account.name == "Synthetic New"
    assert Account.objects.filter(pk=checking.pk).exists()


def test_cron_range_with_step_matches():
    from finance.simplefin_schedule import cron_matches

    tz = dt_utc.utc
    assert cron_matches("0 6-18/6 * * *", datetime(2026, 10, 1, 12, 0, tzinfo=tz))
    assert cron_matches("0 6-18/6 * * *", datetime(2026, 10, 1, 18, 0, tzinfo=tz))
    assert not cron_matches("0 6-18/6 * * *", datetime(2026, 10, 1, 9, 0, tzinfo=tz))
    assert not cron_matches("0 6-18/6 * * *", datetime(2026, 10, 1, 0, 0, tzinfo=tz))


def test_cron_accepts_seven_as_sunday():
    from finance.simplefin_schedule import cron_matches, next_cron_datetime

    sunday = datetime(2026, 10, 4, 6, 30, tzinfo=dt_utc.utc)
    assert cron_matches("30 6 * * 7", sunday)
    assert cron_matches("30 6 * * 0", sunday)
    assert not cron_matches("30 6 * * 7", sunday + timedelta(days=1))
    assert next_cron_datetime("30 6 * * 7", sunday - timedelta(days=1)) == sunday


@pytest.mark.django_db
def test_unreadable_connection_shows_a_safe_message_instead_of_500(monkeypatch):
    from django.core.exceptions import ImproperlyConfigured

    owner, _checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)

    def wrong_key(token):
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY cannot decrypt this connection.")

    monkeypatch.setattr("finance.simplefin_services.decrypt_access_url", wrong_key)
    page = signed_in(owner).get(reverse("simplefin-connections"))

    assert page.status_code == 200
    assert b"can no longer be read with the current encryption key" in page.content
    with pytest.raises(SimpleFinError):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)


def test_redirects_are_refused_and_reported_safely(monkeypatch):
    from email.message import Message

    from finance.simplefin_client import _RefuseRedirects, fetch_accounts

    assert _RefuseRedirects().redirect_request(None, None, 302, "Found", Message(), "https://elsewhere.example.test/") is None

    def redirected(*args, **kwargs):
        raise HTTPError("https://bridge.example.test/simplefin/accounts", 302, "Found", Message(), None)

    monkeypatch.setattr("finance.simplefin_client.urlopen", redirected)
    with pytest.raises(SimpleFinError) as caught:
        fetch_accounts(ACCESS_URL)
    assert "unexpected redirect" in str(caught.value)


@pytest.mark.django_db
def test_sync_does_not_treat_csv_ids_as_simplefin_duplicates(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    Transaction.objects.create(
        account=checking,
        import_batch=ImportBatch.objects.create(
            account=checking,
            imported_by=owner,
            source=ImportBatch.Source.HUNTINGTON,
            source_file_sha256="b" * 64,
            date_range_start=date(2026, 1, 1),
            date_range_end=date(2026, 1, 31),
        ),
        transaction_date=date(2026, 3, 10),
        amount_minor=-500,
        description="Synthetic prior CSV",
        source_row_number=1,
        source_transaction_id="123",
        fingerprint="d" * 64,
        original_fields={"Synthetic Amount": "-5.00"},
    )
    payload = account_payload(transactions=[posted_txn(txn_id="123", day=16, amount="-3.00")])
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            }
        ],
    )

    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 1
    assert Transaction.objects.filter(
        account=checking, status=Transaction.Status.ACTIVE, source_transaction_id="123"
    ).count() == 2


@pytest.mark.django_db
def test_sync_does_not_dedupe_against_a_previous_remote_account_after_relink(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    first = account_payload(
        account_id="sf-a",
        transactions=[posted_txn(txn_id="123", day=16, amount="-3.00")],
    )
    connection = connect_owner(owner, monkeypatch, first)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-a",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            }
        ],
    )
    assert sync_connection(owner, connection.pk, ignore_rate_limit=True)["imported"] == 1

    second = account_payload(
        account_id="sf-b",
        transactions=[posted_txn(txn_id="123", day=17, amount="-4.00")],
    )
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: second)
    save_account_links(
        owner,
        connection.pk,
        [
            {"simplefin_account_id": "CON-1:sf-a", "action": "ignore"},
            {
                "simplefin_account_id": "CON-1:sf-b",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            },
        ],
    )
    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 1
    amounts = list(
        Transaction.objects.filter(
            account=checking, status=Transaction.Status.ACTIVE, source_transaction_id="123"
        ).values_list("amount_minor", flat=True)
    )
    assert sorted(amounts) == [-400, -300]


def test_cron_day_of_month_or_weekday_when_both_restricted():
    from finance.simplefin_schedule import cron_matches

    tz = dt_utc.utc
    assert cron_matches("30 6 1 * 1", datetime(2026, 10, 5, 6, 30, tzinfo=tz))  # a Monday, not the 1st
    assert cron_matches("30 6 1 * 1", datetime(2026, 10, 1, 6, 30, tzinfo=tz))  # the 1st, a Thursday
    assert not cron_matches("30 6 1 * 1", datetime(2026, 10, 6, 6, 30, tzinfo=tz))  # Tuesday the 6th
    assert cron_matches("30 6 * * 1", datetime(2026, 10, 5, 6, 30, tzinfo=tz))
    assert not cron_matches("30 6 * * 1", datetime(2026, 10, 1, 6, 30, tzinfo=tz))


def test_sync_loop_schedules_from_due_time_and_runs_immediately_if_already_due():
    from finance.simplefin_schedule import schedule_after_sync

    due = timezone.make_aware(datetime(2026, 10, 1, 10, 1))
    finished = due + timedelta(seconds=2)
    nxt, wait = schedule_after_sync("* * * * *", due, finished)
    assert nxt == timezone.make_aware(datetime(2026, 10, 1, 10, 2))
    assert wait == 58

    late = due + timedelta(seconds=70)
    late_due, late_wait = schedule_after_sync("* * * * *", due, late)
    assert late_due == timezone.make_aware(datetime(2026, 10, 1, 10, 2))
    assert late_wait == 0


def test_link_rows_match_by_id_when_simplefin_reorders_accounts():
    from django.test import RequestFactory

    from finance.simplefin_views import _choices_from_post

    rendered = [{"id": "CON-1:a", "name": "A"}, {"id": "CON-1:b", "name": "B"}]
    refetched = list(reversed(rendered))
    request = RequestFactory().post("/", {"sf_id_0": "CON-1:a", "action_0": "ignore", "sf_id_1": "CON-1:b", "action_1": "ignore"})

    choices = _choices_from_post(request, refetched)

    assert sorted(choice["simplefin_account_id"] for choice in choices) == ["CON-1:a", "CON-1:b"]


@pytest.mark.django_db
def test_provider_account_errors_do_not_create_covering_import_batch(monkeypatch):
    from finance.cash_flow import GROUPING_MONTH, cash_flow_report

    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload()
    payload["accounts"][0]["transactions"] = []
    payload["errlist"] = [
        {
            "code": "act.missingdata",
            "msg": "Failed to get all transactions. Try again later.",
            "account_id": "sf-checking",
        }
    ]
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            }
        ],
    )

    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert not ImportBatch.objects.filter(
        account=checking, source=ImportBatch.Source.SIMPLEFIN, status=ImportBatch.Status.ACTIVE
    ).exists()
    snapshot = BalanceSnapshot.objects.get(account=checking, source=BalanceSnapshot.Source.SIMPLEFIN)
    assert snapshot.amount_minor == 10023
    assert snapshot.import_batch_id is None
    assert "Failed to get all transactions" in result["result"]
    report = cash_flow_report(
        owner,
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 31),
        grouping=GROUPING_MONTH,
        account=checking,
        today=date(2026, 4, 1),
    )
    assert report.periods[0].missing_import is True


@pytest.mark.django_db
def test_missing_transaction_list_does_not_create_covering_import_batch(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(transactions=[posted_txn(txn_id="hidden-1", day=16, amount="-3.00")])
    del payload["accounts"][0]["transactions"]
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            }
        ],
    )

    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert not ImportBatch.objects.filter(
        account=checking, source=ImportBatch.Source.SIMPLEFIN, status=ImportBatch.Status.ACTIVE
    ).exists()
    assert BalanceSnapshot.objects.filter(account=checking).exists()
    assert not Transaction.objects.filter(account=checking, source_transaction_id="hidden-1").exists()


def test_account_error_only_applies_to_its_own_connection():
    from finance.simplefin_services import _error_applies_to_remote

    error = {"conn_id": "CON-1", "account_id": "acct-1", "msg": "Failed to get all transactions."}
    failing = {"id": "acct-1", "conn_id": "CON-1"}
    healthy = {"id": "acct-1", "conn_id": "CON-2"}

    assert _error_applies_to_remote(error, failing)
    assert not _error_applies_to_remote(error, healthy)


def test_scheduler_waits_real_time_across_a_daylight_saving_change():
    from zoneinfo import ZoneInfo

    from finance.simplefin_schedule import next_cron_datetime, seconds_until

    new_york = ZoneInfo("America/New_York")
    synced = datetime(2027, 3, 13, 6, 30, tzinfo=new_york)
    due = next_cron_datetime("30 6 * * *", synced)

    # Clocks spring forward overnight, so 06:30 the next day is 23 real hours away.
    assert due.hour == 6
    assert seconds_until(due, synced) == 23 * 3600


@pytest.mark.django_db
def test_a_sync_with_only_stored_rows_does_not_hide_an_undone_import(monkeypatch):
    from finance.cash_flow import GROUPING_MONTH, cash_flow_report

    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(transactions=[posted_txn(txn_id="t-1", day=12, amount="-25.00")])
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 1),
            }
        ],
    )
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    first = ImportBatch.objects.get(account=checking, source=ImportBatch.Source.SIMPLEFIN)
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    undo_import_batch(owner, checking.pk, first.pk)

    report = cash_flow_report(
        owner,
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 31),
        grouping=GROUPING_MONTH,
        account=checking,
        today=date(2026, 4, 1),
    )
    assert report.periods[0].missing_import is True


def _link_checking(owner, monkeypatch, payloads):
    """Connect and link a checking account; each sync returns the next payload."""
    checking = make_account(owner)
    connection = connect_owner(owner, monkeypatch, payloads[0])
    queue = list(payloads)
    monkeypatch.setattr(
        "finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: queue.pop(0) if len(queue) > 1 else queue[0]
    )
    save_account_links(
        owner,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 1),
            }
        ],
    )
    return checking, connection


def _sync_on(owner, connection, monkeypatch, day):
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: day)
    sync_connection(owner, connection.pk, ignore_rate_limit=True)


def _month_missing(owner, account, first, last):
    from finance.cash_flow import GROUPING_MONTH, cash_flow_report

    report = cash_flow_report(
        owner, date_from=first, date_to=last, grouping=GROUPING_MONTH, account=account, today=date(2026, 5, 1)
    )
    return report.periods[0].missing_import


@pytest.mark.django_db
def test_a_later_sync_covers_only_dates_not_already_imported(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    march = posted_txn(txn_id="t-1", day=12, amount="-25.00")
    april = posted_txn(txn_id="t-2", day=1, amount="-30.00", posted=epoch(2026, 4, 10))
    checking, connection = _link_checking(
        owner, monkeypatch, [account_payload(transactions=[march]), account_payload(transactions=[march, april])]
    )
    _sync_on(owner, connection, monkeypatch, date(2026, 3, 31))
    first = ImportBatch.objects.get(account=checking, source=ImportBatch.Source.SIMPLEFIN)
    _sync_on(owner, connection, monkeypatch, date(2026, 4, 30))

    undo_import_batch(owner, checking.pk, first.pk)

    assert _month_missing(owner, checking, date(2026, 3, 1), date(2026, 3, 31)) is True
    assert _month_missing(owner, checking, date(2026, 4, 1), date(2026, 4, 30)) is False


@pytest.mark.django_db
def test_a_sync_with_only_stored_rows_still_covers_newly_elapsed_dates(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    march = posted_txn(txn_id="t-1", day=12, amount="-25.00")
    checking, connection = _link_checking(owner, monkeypatch, [account_payload(transactions=[march])])
    _sync_on(owner, connection, monkeypatch, date(2026, 3, 31))
    _sync_on(owner, connection, monkeypatch, date(2026, 4, 30))

    assert _month_missing(owner, checking, date(2026, 4, 1), date(2026, 4, 30)) is False


@pytest.mark.django_db
def test_a_repeat_sync_keeps_the_balance_snapshots_original_batch(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    march = posted_txn(txn_id="t-1", day=12, amount="-25.00")
    checking, connection = _link_checking(owner, monkeypatch, [account_payload(transactions=[march])])
    _sync_on(owner, connection, monkeypatch, date(2026, 3, 31))
    first = ImportBatch.objects.get(account=checking, source=ImportBatch.Source.SIMPLEFIN)
    _sync_on(owner, connection, monkeypatch, date(2026, 3, 31))

    undo_import_batch(owner, checking.pk, first.pk)

    assert not BalanceSnapshot.objects.filter(account=checking, source=BalanceSnapshot.Source.SIMPLEFIN).exists()
