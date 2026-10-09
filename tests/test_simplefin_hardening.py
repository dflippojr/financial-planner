"""Malformed SimpleFIN payloads fail as SimpleFinError or skip a row, never anything else."""

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
    assert len(row.description) == 1000
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

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.last_sync_result == UNEXPECTED_FAILURE
    assert not first.disabled
    assert second.last_sync_result.startswith("Synced 1 ")
    assert Transaction.objects.filter(account=second_checking).count() == 1
    assert alert_passes == [1]


@pytest.mark.django_db
def test_failure_during_import_is_contained_per_connection(monkeypatch):
    (first, _first_checking), (second, second_checking) = _two_connections(monkeypatch)
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

    first.refresh_from_db()
    assert first.last_sync_result == UNEXPECTED_FAILURE
    assert Transaction.objects.filter(account=second_checking).count() == 1


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
