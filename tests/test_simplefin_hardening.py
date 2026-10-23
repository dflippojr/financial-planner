"""Malformed SimpleFIN payloads fail as SimpleFinError or skip a row, never anything else."""

import logging
from datetime import date

import pytest
from django.core.management import call_command

from finance.models import AccountLink, SimpleFinConnection, Transaction
from finance.simplefin_errors import SimpleFinError, clean_text, sanitize_provider_message
from finance.simplefin_services import (
    UNEXPECTED_FAILURE,
    decimal_to_minor,
    listed_accounts,
    posted_date,
    save_account_links,
    sync_all_connections,
    sync_connection,
)
from tests.test_simplefin import (
    _linked_owner_and_checking,
    account_payload,
    epoch,
    make_account,
    make_household,
    make_person,
    posted_txn,
)

FAR_FUTURE = epoch(9999, 6, 15)
PAST_YEAR_9999 = 253402300800 + 86400 * 400


def payload_with(item):
    return account_payload(transactions=[item])


def use_payload(monkeypatch, payload):
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: payload)


@pytest.mark.parametrize(
    "posted",
    [float("inf"), float("-inf"), float("nan"), 1e300, 10**400, FAR_FUTURE, PAST_YEAR_9999, True, "soon", [1], -5],
)
def test_unusable_posted_values_have_no_date(posted):
    assert posted_date(posted) is None


@pytest.mark.parametrize(
    "amount",
    ["1e1000000", "1e999999999", "-9e19", "Infinity", "NaN", float("inf"), "1.001", "\ud800", "12" * 30,
     "1." + "0" * 40 + "1"],
)
def test_unstorable_amounts_raise_simplefin_error(amount):
    with pytest.raises(SimpleFinError):
        decimal_to_minor(amount)


def test_storable_amounts_convert_exactly():
    assert decimal_to_minor("-12.30") == -1230
    assert decimal_to_minor("92233720368547758.07") == 2**63 - 1
    assert decimal_to_minor("1.000000000000000000000000000000") == 100


def test_clean_text_keeps_text_storable():
    assert clean_text("Synthetic\ud800 Payee\x00") == "Synthetic? Payee"
    assert sanitize_provider_message("bad\udc80 msg") == "bad? msg"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "posted",
    [float("inf"), float("nan"), 1e300, FAR_FUTURE, PAST_YEAR_9999, True],
)
def test_sync_skips_rows_with_unusable_posted_dates(monkeypatch, posted):
    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    use_payload(monkeypatch, payload_with(posted_txn(txn_id="sf-bad-date", day=10, amount="-1.00", posted=posted)))

    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 0
    assert not Transaction.objects.filter(account=checking).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [
    ("amount", "1e1000000"),
    ("amount", "9" * 25),
    ("amount", float("inf")),
    ("id", "x" * 300),
])
def test_sync_rejects_unstorable_rows_as_simplefin_error(monkeypatch, field, value):
    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    item = posted_txn(txn_id="sf-bad", day=10, amount="-1.00")
    item[field] = value
    use_payload(monkeypatch, payload_with(item))

    with pytest.raises(SimpleFinError):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)

    connection.refresh_from_db()
    assert connection.last_sync_result
    assert not Transaction.objects.filter(account=checking).exists()


@pytest.mark.django_db
def test_sync_stores_surrogates_and_nuls_as_clean_text(monkeypatch):
    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    item = posted_txn(txn_id="sf-\udc80id", day=10, amount="-1.00")
    item["description"] = "Synthetic\ud800 Shop\x00" + "y" * 2000
    item["pending"] = {"nested": float("nan")}
    use_payload(monkeypatch, payload_with(item))

    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    row = Transaction.objects.get(account=checking)
    assert row.source_transaction_id == "sf-?id"
    assert row.description.startswith("Synthetic? Shop")
    assert len(row.description) == 500
    assert row.original_fields["pending"] is None
    assert row.original_fields["posted"] == epoch(2026, 3, 10)


def test_account_listing_drops_unstorable_ids_and_cleans_names():
    payload = account_payload()
    payload["accounts"][0]["name"] = "Synthetic\ud800 Checking"
    long_id = dict(payload["accounts"][0], id="z" * 300)
    payload["accounts"].append(long_id)

    rows = listed_accounts(payload)

    assert [row["id"] for row in rows] == ["CON-1:sf-checking"]
    assert rows[0]["name"] == "Synthetic? Checking"


def _two_connections(monkeypatch):
    first_owner, first_checking, first = _linked_owner_and_checking(monkeypatch)
    make_household(first_owner)
    second_owner = make_person("second")
    make_household(second_owner, name="Second Synthetic Household")
    second_checking = make_account(second_owner, name="Synthetic Second Checking")
    from tests.test_simplefin import connect_owner

    second = connect_owner(second_owner, monkeypatch)
    save_account_links(
        second_owner,
        second.pk,
        [{"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": second_checking.pk,
          "cutover_date": date(2026, 3, 1)}],
    )
    return (first, first_checking), (second, second_checking)


def _failed_and_synced(*pairs):
    """Split the connections after a pass; the daily job does not promise an order."""
    for connection, _checking in pairs:
        connection.refresh_from_db()
    failed = [pair for pair in pairs if pair[0].last_sync_result == UNEXPECTED_FAILURE]
    synced = [pair for pair in pairs if pair[0].last_sync_result != UNEXPECTED_FAILURE]
    assert len(failed) == 1, [connection.last_sync_result for connection, _checking in pairs]
    return failed[0], synced[0]


@pytest.mark.django_db
def test_unexpected_failure_on_one_connection_does_not_stop_the_daily_pass(monkeypatch):
    (first, first_checking), (second, second_checking) = _two_connections(monkeypatch)
    healthy = payload_with(posted_txn(txn_id="sf-ok", day=10, amount="-2.00"))
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic unexpected failure with provider text")
        return healthy

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", fetch)
    alert_passes = []
    monkeypatch.setattr("finance.management.commands.sync_simplefin.run_daily_alert_pass",
                        lambda: alert_passes.append(1))

    call_command("sync_simplefin")

    (failed, _), (synced, synced_checking) = _failed_and_synced((first, first_checking), (second, second_checking))
    assert not failed.disabled
    assert synced.last_sync_result.startswith("Synced 1 ")
    assert Transaction.objects.filter(account=synced_checking).count() == 1
    assert alert_passes == [1]


@pytest.mark.django_db
def test_failure_during_import_is_contained_per_connection(monkeypatch):
    first_pair, second_pair = _two_connections(monkeypatch)
    healthy = payload_with(posted_txn(txn_id="sf-ok", day=10, amount="-2.00"))
    use_payload(monkeypatch, healthy)
    original = Transaction.objects.bulk_create
    calls = []

    def flaky_bulk_create(rows, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OverflowError("synthetic")
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(Transaction.objects, "bulk_create", flaky_bulk_create)

    assert sync_all_connections() == 2

    _failed, (_synced, synced_checking) = _failed_and_synced(first_pair, second_pair)
    assert Transaction.objects.filter(account=synced_checking).count() == 1


@pytest.mark.django_db
def test_alert_pass_runs_when_the_sync_stage_fails(monkeypatch):
    owner, _checking, _connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)

    def broken():
        raise RuntimeError("synthetic")

    alert_passes = []
    monkeypatch.setattr("finance.management.commands.sync_simplefin.sync_all_connections", broken)
    monkeypatch.setattr("finance.management.commands.sync_simplefin.run_daily_alert_pass",
                        lambda: alert_passes.append(1))

    call_command("sync_simplefin")

    assert alert_passes == [1]


@pytest.mark.django_db
def test_recording_an_unexpected_failure_never_escapes(monkeypatch):
    owner, _checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", broken)
    monkeypatch.setattr("finance.simplefin_services._record_failure", broken)

    assert sync_all_connections() == 1
    assert SimpleFinConnection.objects.filter(pk=connection.pk).exists()


@pytest.mark.django_db
def test_a_link_saved_mid_fetch_with_an_earlier_cutover_is_not_applied(monkeypatch):
    owner, checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)

    def fetch_while_links_change(*args, **kwargs):
        AccountLink.objects.filter(connection=connection).update(cutover_date=date(2025, 1, 1))
        return payload_with(posted_txn(txn_id="sf-ok", day=10, amount="-2.00"))

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", fetch_while_links_change)

    with pytest.raises(SimpleFinError, match="Account links changed"):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)
    assert not Transaction.objects.filter(account=checking).exists()


def _member_write_completes_while_provider_blocks(member, provider_call):
    """Run provider_call in a thread while its outbound request blocks; return member write timing."""
    import threading

    from django.db import connections, transaction

    from finance.lifecycle_services import lock_actor_household

    blocked = threading.Event()
    release = threading.Event()
    member_done = threading.Event()
    errors = []

    def run(action, done=None):
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()
            if done is not None:
                done.set()

    def member_write():
        with transaction.atomic():
            lock_actor_household(member)
            make_account(member, name="Synthetic Member Savings")

    provider = threading.Thread(target=run, args=(lambda: provider_call(blocked, release),))
    provider.start()
    try:
        assert blocked.wait(timeout=10)
        writer = threading.Thread(target=run, args=(member_write, member_done))
        writer.start()
        finished_while_blocked = member_done.wait(timeout=10)
    finally:
        release.set()
        provider.join(timeout=30)
    writer.join(timeout=30)
    assert errors == []
    return finished_while_blocked


@pytest.mark.django_db(transaction=True)
def test_sync_fetch_does_not_hold_household_locks(monkeypatch):
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("row locks can only be exercised on PostgreSQL")
    member = make_person("member")
    owner, checking, sf_connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner, member)
    healthy = payload_with(posted_txn(txn_id="sf-ok", day=10, amount="-2.00"))

    def sync(blocked, release):
        def slow_fetch(*args, **kwargs):
            blocked.set()
            release.wait(timeout=20)
            return healthy

        monkeypatch.setattr("finance.simplefin_services.fetch_accounts", slow_fetch)
        sync_connection(owner, sf_connection.pk, ignore_rate_limit=True)

    assert _member_write_completes_while_provider_blocks(member, sync)
    assert Transaction.objects.filter(account=checking).count() == 1
    sf_connection.refresh_from_db()
    assert sf_connection.last_sync_result.startswith("Synced 1 ")


@pytest.mark.django_db(transaction=True)
def test_claim_request_does_not_hold_household_locks(monkeypatch):
    from django.db import connection

    from finance.simplefin_services import claim_connection
    from tests.test_simplefin import ACCESS_URL, setup_token

    if connection.vendor != "postgresql":
        pytest.skip("row locks can only be exercised on PostgreSQL")
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)

    def claim(blocked, release):
        def slow_claim(url):
            blocked.set()
            release.wait(timeout=20)
            return ACCESS_URL

        monkeypatch.setattr("finance.simplefin_services.claim_access_url", slow_claim)
        claim_connection(owner, setup_token())

    assert _member_write_completes_while_provider_blocks(member, claim)
    assert SimpleFinConnection.objects.filter(owner=owner).count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_database_error_on_one_connection_does_not_stop_the_daily_pass(monkeypatch):
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("only PostgreSQL enforces column lengths")
    (first, _first_checking), (_second, second_checking) = _two_connections(monkeypatch)
    # Let an over-long id past validation so the database itself rejects it.
    monkeypatch.setattr("finance.simplefin_services.MAX_ID_CHARS", 10_000)
    rows = iter([
        payload_with(posted_txn(txn_id="x" * 300, day=10, amount="-2.00")),
        payload_with(posted_txn(txn_id="sf-ok", day=10, amount="-2.00")),
    ])
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: next(rows))
    alert_passes = []
    monkeypatch.setattr("finance.management.commands.sync_simplefin.run_daily_alert_pass",
                        lambda: alert_passes.append(1))

    call_command("sync_simplefin")

    first.refresh_from_db()
    assert first.last_sync_result == UNEXPECTED_FAILURE
    assert Transaction.objects.filter(account=second_checking).count() == 1
    assert alert_passes == [1]


def _make_unreadable(connection):
    SimpleFinConnection.objects.filter(pk=connection.pk).update(encrypted_access_url=b"not-a-fernet-token")


@pytest.mark.django_db
def test_scheduled_sync_records_and_alerts_an_unreadable_connection(monkeypatch, caplog):
    from finance.models import Alert, AuditEvent
    from finance.simplefin_services import UNREADABLE_CONNECTION
    from tests.test_simplefin import ACCESS_URL

    caplog.set_level(logging.DEBUG)
    owner, _checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    assert sync_all_connections() == 1
    connection.refresh_from_db()
    before = connection.last_sync_at
    _make_unreadable(connection)

    assert sync_all_connections() == 1
    connection.refresh_from_db()
    assert connection.last_sync_at != before
    assert connection.last_sync_result == UNREADABLE_CONNECTION
    assert not connection.disabled
    assert sync_all_connections() == 1

    alerts = Alert.objects.filter(recipient=owner, kind=Alert.Kind.SYNC)
    assert alerts.count() == 1
    stored = [repr(row) for row in alerts.values()] + [repr(row) for row in AuditEvent.objects.values()]
    for text in [*stored, caplog.text]:
        assert ACCESS_URL not in text
        assert "synthetic-access-secret" not in text


@pytest.mark.django_db
def test_rate_limited_sync_of_an_unreadable_connection_records_nothing(monkeypatch):
    from finance.models import Alert
    from finance.simplefin_errors import SimpleFinRateLimited

    owner, _checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    sync_connection(owner, connection.pk)
    connection.refresh_from_db()
    before = (connection.last_sync_at, connection.last_sync_result)
    _make_unreadable(connection)

    with pytest.raises(SimpleFinRateLimited):
        sync_connection(owner, connection.pk)

    connection.refresh_from_db()
    assert (connection.last_sync_at, connection.last_sync_result) == before
    assert not Alert.objects.filter(recipient=owner, kind=Alert.Kind.SYNC).exists()


UNUSABLE_KEYS = pytest.mark.parametrize("bad_key", ["", "not-a-fernet-key"], ids=["empty", "malformed"])


def _no_secret_in(texts, *secrets):
    for text in texts:
        for secret in secrets:
            if secret:
                assert secret not in text


@pytest.mark.parametrize("bad_key", ["", "not-a-fernet-key", "x" * 44], ids=["empty", "malformed", "wrong-length"])
def test_an_unusable_key_is_improperly_configured_without_quoting_it(settings, bad_key):
    from django.core.exceptions import ImproperlyConfigured

    from finance.encryption import encrypt_secret

    settings.FIELD_ENCRYPTION_KEY = bad_key
    with pytest.raises(ImproperlyConfigured) as raised:
        encrypt_secret("synthetic-secret")
    _no_secret_in([str(raised.value), repr(raised.value)], bad_key)


@UNUSABLE_KEYS
@pytest.mark.django_db
def test_connect_with_an_unusable_key_never_spends_the_setup_token(monkeypatch, settings, caplog, bad_key):
    import html

    from django.urls import reverse

    from finance.models import AuditEvent, SimpleFinClaim
    from tests.test_simplefin import ACCESS_URL, CLAIM_URL, setup_token, signed_in

    caplog.set_level(logging.DEBUG)
    good_key = settings.FIELD_ENCRYPTION_KEY
    owner = make_person("owner")
    make_household(owner)
    claimed = []

    def fake_claim(url):
        claimed.append(url)
        return ACCESS_URL

    monkeypatch.setattr("finance.simplefin_services.claim_access_url", fake_claim)
    use_payload(monkeypatch, account_payload())
    client = signed_in(owner)
    client.raise_request_exception = False
    settings.FIELD_ENCRYPTION_KEY = bad_key

    response = client.post(reverse("simplefin-connections"), {"intent": "claim", "token": setup_token()})

    assert claimed == [], "the setup token was spent before the key was checked"
    assert response.status_code == 200
    page = html.unescape(response.content.decode())
    assert "encryption key is not configured" in page
    assert "setup token was not used" in page
    assert not SimpleFinConnection.objects.filter(owner=owner).exists()
    assert not SimpleFinClaim.objects.exists()
    _no_secret_in([page, caplog.text, *(repr(row) for row in AuditEvent.objects.values())], bad_key, ACCESS_URL)

    settings.FIELD_ENCRYPTION_KEY = good_key
    fixed = client.post(reverse("simplefin-connections"), {"intent": "claim", "token": setup_token()})

    assert fixed.status_code == 302
    assert claimed == [CLAIM_URL]
    assert SimpleFinConnection.objects.filter(owner=owner).exists()


@UNUSABLE_KEYS
@pytest.mark.django_db
def test_sync_with_an_unusable_key_asks_the_member_to_reconnect(monkeypatch, settings, caplog, bad_key):
    import html

    from django.urls import reverse

    from finance.models import Alert
    from finance.simplefin_services import UNREADABLE_CONNECTION
    from tests.test_simplefin import ACCESS_URL, signed_in

    caplog.set_level(logging.DEBUG)
    owner, _checking, connection = _linked_owner_and_checking(monkeypatch)
    make_household(owner)
    fetched = []
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: fetched.append(args))
    settings.FIELD_ENCRYPTION_KEY = bad_key

    with pytest.raises(SimpleFinError) as raised:
        sync_connection(owner, connection.pk)

    assert str(raised.value) == UNREADABLE_CONNECTION
    connection.refresh_from_db()
    assert connection.last_sync_result == UNREADABLE_CONNECTION

    SimpleFinConnection.objects.filter(pk=connection.pk).update(last_sync_result="")
    assert sync_all_connections() == 1
    connection.refresh_from_db()
    assert connection.last_sync_result == UNREADABLE_CONNECTION
    assert not connection.disabled

    response = signed_in(owner).get(reverse("simplefin-connections"))
    assert response.status_code == 200
    page = html.unescape(response.content.decode())
    assert UNREADABLE_CONNECTION in page
    assert fetched == []
    alerts = [repr(row) for row in Alert.objects.filter(recipient=owner).values()]
    _no_secret_in([page, caplog.text, str(raised.value), *alerts], bad_key, ACCESS_URL, "synthetic-access-secret")
