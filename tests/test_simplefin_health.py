"""Per-bank SimpleFIN health, the shared request budget, the Fix/Add flows and health pushes (#694).

Synthetic feeds only; every SimpleFIN request is a monkeypatched fetch_accounts.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from finance import simplefin_flow
from finance.alert_ntfy import fake_transport
from finance.alert_services import run_daily_alert_pass, settings_for
from finance.models import SimpleFinConnection
from finance.simplefin_budget import (
    DAILY_REQUEST_CAP,
    INTERACTIVE_LIMIT,
    SimpleFinBudgetExceeded,
    requests_used,
    reserve_request,
)
from finance.simplefin_health import ERROR, OK, STALE, bank_rows, bank_state, parse_health, push_health_changes
from finance.simplefin_services import load_remote_accounts, sync_all_connections, sync_connection
from tests.test_simplefin import connect_owner, make_household, make_person, signed_in

TOPIC = "synthetic-health_1"


def stamp(moment):
    return int(moment.timestamp())


def feed(*, north_at, north_errors=(), errlist_extra=(), errors=(), add_bank=False, fresh_at=None):
    """Two synthetic banks: North (two accounts) and Credit Union (one)."""
    fresh_at = fresh_at or timezone.now() - timedelta(hours=2)
    connections = [
        {"conn_id": "CON-N", "name": "Synthetic North Bank", "org_id": "ORG-N", "sfin_url": "https://n.example.test/sf"},
        {"conn_id": "CON-C", "name": "Synthetic Credit Union", "org_id": "ORG-C", "sfin_url": "https://c.example.test/sf"},
    ]
    accounts = [
        {"id": "n-check", "name": "Synthetic North Checking", "conn_id": "CON-N", "currency": "USD", "balance": "10.00",
         "balance-date": stamp(north_at), "transactions": []},
        {"id": "n-save", "name": "Synthetic North Savings", "conn_id": "CON-N", "currency": "USD", "balance": "20.00",
         "balance-date": stamp(north_at - timedelta(hours=3)), "transactions": []},
        {"id": "c-check", "name": "Synthetic Union Checking", "conn_id": "CON-C", "currency": "USD", "balance": "30.00",
         "balance-date": stamp(fresh_at), "transactions": []},
    ]
    if add_bank:
        connections.append({"conn_id": "CON-A", "name": "Synthetic Added Bank", "org_id": "ORG-A",
                            "sfin_url": "https://a.example.test/sf"})
        accounts.append({"id": "a-check", "name": "Synthetic Added Checking", "conn_id": "CON-A", "currency": "USD",
                         "balance": "40.00", "balance-date": stamp(fresh_at), "transactions": []})
    errlist = [{"code": "con.auth", "msg": message, "conn_id": "CON-N"} for message in north_errors]
    return {"errlist": errlist + list(errlist_extra), "errors": list(errors), "connections": connections,
            "accounts": accounts}


@pytest.fixture(autouse=True)
def outbox():
    fake_transport.messages.clear()
    yield fake_transport.messages
    fake_transport.messages.clear()


@pytest.fixture
def ntfy(settings):
    settings.NTFY_URL = "https://ntfy.example.invalid"
    settings.ALERT_EMAIL_BASE_URL = "https://planner.example.invalid"


def opt_in(person):
    prefs = settings_for(person)
    prefs.ntfy_enabled = True
    prefs.ntfy_topic = TOPIC
    prefs.save()
    return prefs


class Feed:
    """A swappable stub for fetch_accounts that counts calls."""

    def __init__(self, monkeypatch, payload):
        self.payload = payload
        self.calls = []
        monkeypatch.setattr("finance.simplefin_services.fetch_accounts", self)

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def connected(monkeypatch, payload, username="owner"):
    owner = make_person(username)
    make_household(owner)
    connection = connect_owner(owner, monkeypatch, payload)
    return owner, connection, Feed(monkeypatch, payload)


def fresh(connection):
    return SimpleFinConnection.objects.get(pk=connection.pk)


@pytest.fixture
def scheduled_sync(django_capture_on_commit_callbacks):
    """A scheduled sync, running the after-commit health push as a real commit would."""

    def run(owner, connection):
        with django_capture_on_commit_callbacks(execute=True):
            sync_connection(owner, connection.pk, ignore_rate_limit=True)

    return run


# Health parsing -------------------------------------------------------------


def test_banks_group_accounts_by_conn_id_with_the_newest_balance_date():
    now = timezone.now()
    banks, general = parse_health(feed(north_at=now - timedelta(hours=40)))

    assert [bank["name"] for bank in banks] == ["Synthetic Credit Union", "Synthetic North Bank"]
    north = banks[1]
    assert north["accounts"] == 2
    # The newer of the two accounts' balance dates.
    assert north["balance_date"] == (now - timedelta(hours=40)).replace(microsecond=0).isoformat()
    assert general == []


def test_stale_means_more_than_36_hours_behind():
    now = timezone.now()
    banks, _ = parse_health(feed(north_at=now - timedelta(hours=36, minutes=1), fresh_at=now - timedelta(hours=35)))
    states = {bank["conn_id"]: bank_state(bank, now) for bank in banks}

    assert states == {"CON-N": STALE, "CON-C": OK}


def test_errors_attach_to_their_bank_and_unnamed_ones_stay_general():
    now = timezone.now()
    payload = feed(
        north_at=now,
        north_errors=["Synthetic North Bank needs you to sign in again."],
        errlist_extra=[
            {"code": "act.failed", "msg": "Synthetic union account failed.", "account_id": "c-check"},
            {"code": "gen.api", "msg": "Synthetic developer notice."},
            {"code": "gen.", "msg": "Synthetic general outage."},
        ],
        errors=["Synthetic Credit Union is slow today.", "Synthetic notice for nobody."],
    )
    banks, general = parse_health(payload)
    by_id = {bank["conn_id"]: bank for bank in banks}

    assert by_id["CON-N"]["errors"] == ["Synthetic North Bank needs you to sign in again."]
    # By account id, and a deprecated string that names exactly one bank.
    assert by_id["CON-C"]["errors"] == ["Synthetic union account failed.", "Synthetic Credit Union is slow today."]
    # gen.api is advice to the developer and is left out.
    assert general == ["Synthetic general outage.", "Synthetic notice for nobody."]
    assert {bank_state(bank, now) for bank in banks} == {ERROR}


def test_an_account_id_two_banks_share_never_lands_on_the_wrong_bank():
    now = timezone.now()
    payload = feed(north_at=now, errlist_extra=[
        {"code": "act.failed", "msg": "Synthetic shared id failed.", "account_id": "checking"},
        {"code": "act.failed", "msg": "Synthetic qualified id failed.", "account_id": "CON-C:checking"},
    ])
    payload["accounts"][0]["id"] = "checking"  # North
    payload["accounts"][2]["id"] = "checking"  # Credit Union
    banks, general = parse_health(payload)
    by_id = {bank["conn_id"]: bank for bank in banks}

    assert by_id["CON-N"]["errors"] == []
    assert by_id["CON-C"]["errors"] == ["Synthetic qualified id failed."]
    assert general == ["Synthetic shared id failed."]


def test_provider_error_text_is_sanitized_before_it_is_stored():
    payload = feed(north_at=timezone.now(), north_errors=[
        "<script>alert(1)</script>Sign in at https://demo:secret@bridge.example.test/x again",
    ])
    banks, _ = parse_health(payload)
    message = {bank["conn_id"]: bank for bank in banks}["CON-N"]["errors"][0]

    assert "<script>" not in message and "secret" not in message
    assert "[redacted]" in message


# Stored reads and the page ----------------------------------------------------


@pytest.mark.django_db
def test_a_sync_stores_bank_health_and_the_page_renders_it_without_a_live_request(monkeypatch):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(
        north_at=now - timedelta(hours=40), north_errors=["<b>Synthetic</b> sign-in needed"]))
    sync_connection(owner, connection.pk)
    stub.calls.clear()

    response = signed_in(owner).get(reverse("simplefin-connections"))
    html = response.content.decode()

    assert stub.calls == []
    assert response["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"
    assert "Synthetic North Bank" in html and "Synthetic Credit Union" in html
    assert "Synthetic sign-in needed" in html and "<b>Synthetic</b>" not in html
    assert html.count("data-simplefin-fix=") == 1
    assert 'data-simplefin-fix="CON-N"' in html
    assert "data-simplefin-add" in html
    assert f"SimpleFIN requests in the last 24 hours: 1 of {DAILY_REQUEST_CAP}. The last 2 are kept" in html
    assert "noopener" in html


@pytest.mark.django_db
def test_the_first_page_view_reads_the_account_list_once_then_uses_the_stored_copy(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now()))
    client = signed_in(owner)

    client.get(reverse("simplefin-connections"))
    client.get(reverse("simplefin-connections"))

    assert len(stub.calls) == 1 and stub.calls[0].get("balances_only") is True
    assert requests_used(fresh(connection)) == 1
    assert {row["conn_id"] for row in bank_rows(fresh(connection))} == {"CON-N", "CON-C"}


@pytest.mark.django_db
def test_other_household_members_see_no_bank_controls(monkeypatch):
    owner, connection, _stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(days=3)))
    sync_connection(owner, connection.pk)
    member = make_person("member")
    from finance.models import Membership

    Membership.objects.create(person=member, household=owner.memberships.first().household)
    client = signed_in(member)

    html = client.get(reverse("simplefin-connections")).content.decode()
    assert "data-simplefin-fix" not in html and "Synthetic North Bank" not in html
    for name in ("simplefin-flow-start", "simplefin-flow-check", "simplefin-flow-cancel"):
        assert client.post(reverse(name), "{}", content_type="application/json").status_code == 404


# The request budget -------------------------------------------------------------


@pytest.mark.django_db
def test_member_requests_stop_at_the_interactive_limit_and_scheduled_syncs_still_run(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now()))
    for _ in range(INTERACTIVE_LIMIT):
        sync_connection(owner, connection.pk, ignore_rate_limit=True, scheduled=False)

    with pytest.raises(SimpleFinBudgetExceeded):
        sync_connection(owner, connection.pk, ignore_rate_limit=True, scheduled=False)
    with pytest.raises(SimpleFinBudgetExceeded):
        load_remote_accounts(fresh(connection))
    assert len(stub.calls) == INTERACTIVE_LIMIT
    # A refused request is not a failed sync: no failure is recorded.
    assert fresh(connection).last_sync_result.startswith("Synced ")

    assert sync_all_connections() == 1
    assert len(stub.calls) == INTERACTIVE_LIMIT + 1
    assert requests_used(fresh(connection)) == INTERACTIVE_LIMIT + 1 <= DAILY_REQUEST_CAP


@pytest.mark.django_db
def test_sync_now_reports_a_used_budget_without_calling_simplefin(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now()))
    now = timezone.now().isoformat()
    SimpleFinConnection.objects.filter(pk=connection.pk).update(request_log=[now] * INTERACTIVE_LIMIT)

    response = signed_in(owner).post(reverse("simplefin-sync"), follow=True)

    assert stub.calls == []
    assert b"daily request budget" in response.content


@pytest.mark.django_db
def test_requests_older_than_24_hours_leave_the_budget(monkeypatch):
    _owner, connection, _stub = connected(monkeypatch, feed(north_at=timezone.now()))
    old = (timezone.now() - timedelta(hours=24, minutes=1)).isoformat()
    SimpleFinConnection.objects.filter(pk=connection.pk).update(request_log=[old] * 30 + ["not a time"])

    reserve_request(connection.pk, scheduled=False)

    assert len(fresh(connection).request_log) == 1


# Fix and Add flows --------------------------------------------------------------


def post_json(client, name, body=None):
    """POST JSON. A start remembers its flow id on the client; check and cancel send it, as the page does."""
    if body is None and name in {"simplefin-flow-check", "simplefin-flow-cancel"}:
        body = {"flow_id": getattr(client, "flow_id", "")}
    data = client.post(reverse(name), json.dumps(body or {}), content_type="application/json").json()
    if name == "simplefin-flow-start" and data.get("flow_id"):
        client.flow_id = data["flow_id"]
    return data


def rewind_flow(connection, seconds):
    flow = fresh(connection).flow
    flow["last_request"] -= seconds
    SimpleFinConnection.objects.filter(pk=connection.pk).update(flow=flow)


@pytest.mark.django_db
def test_fix_succeeds_when_the_error_clears_and_needs_no_second_request(monkeypatch):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(north_at=now, north_errors=["Synthetic sign in again"]))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)

    started = post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    assert started["url"] == simplefin_flow.BRIDGE_URL
    assert started["first_check_in"] == simplefin_flow.CHECK_DELAYS[0]

    stub.calls.clear()
    stub.payload = feed(north_at=now)
    result = post_json(client, "simplefin-flow-check")

    assert result == {"state": "success", "message": "Synthetic North Bank is up to date."}
    assert len(stub.calls) == 1  # The check was the sync.
    assert fresh(connection).flow == {}
    assert b"Synthetic North Bank is up to date." in client.get(reverse("simplefin-connections")).content


@pytest.mark.django_db
def test_fix_of_a_stale_bank_succeeds_on_a_newer_balance_date(monkeypatch):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(north_at=now - timedelta(hours=40)))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)
    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})

    # Same balance date: not fixed yet.
    assert post_json(client, "simplefin-flow-check")["state"] == "pending"
    rewind_flow(connection, simplefin_flow.FLOW_MIN_SPACING)
    stub.payload = feed(north_at=now - timedelta(minutes=5))
    assert post_json(client, "simplefin-flow-check")["state"] == "success"


@pytest.mark.django_db
def test_checks_are_spaced_and_capped_then_say_simplefin_has_not_refreshed(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=40)))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)
    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    stub.calls.clear()

    first = post_json(client, "simplefin-flow-check")
    assert first == {"state": "pending", "next_check_in": simplefin_flow.CHECK_DELAYS[1],
                     "checks_left": simplefin_flow.FLOW_MAX_REQUESTS - 1}
    too_soon = post_json(client, "simplefin-flow-check")
    assert too_soon["state"] == "wait" and 0 < too_soon["retry_in"] <= simplefin_flow.FLOW_MIN_SPACING
    assert len(stub.calls) == 1

    states = []
    for _ in range(simplefin_flow.FLOW_MAX_REQUESTS - 1):
        rewind_flow(connection, simplefin_flow.FLOW_MIN_SPACING)
        states.append(post_json(client, "simplefin-flow-check"))

    assert [state["state"] for state in states] == ["pending", "pending", "exhausted"]
    assert states[-1]["message"] == simplefin_flow.NOT_REFRESHED
    assert len(stub.calls) == simplefin_flow.FLOW_MAX_REQUESTS
    assert post_json(client, "simplefin-flow-check")["state"] == "gone"


@pytest.mark.django_db
def test_a_check_whose_import_fails_is_not_a_success(monkeypatch):
    from finance.simplefin_errors import SimpleFinError

    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(north_at=now, north_errors=["Synthetic sign in again"]))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)
    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})

    def import_fails(*args, **kwargs):
        raise SimpleFinError("Synthetic import failure.")

    stub.payload = feed(north_at=now)
    monkeypatch.setattr("finance.simplefin_services._import_links", import_fails)
    result = post_json(client, "simplefin-flow-check")

    assert result["state"] == "pending"
    # The read itself was stored, but the flow waits for a sync that completes.
    assert bank_rows(fresh(connection))[1]["errors"] == []


@pytest.mark.django_db
def test_the_flow_lives_on_the_connection_so_repeated_checks_share_its_limits(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=40)))
    sync_connection(owner, connection.pk)
    first, second = signed_in(owner), signed_in(owner)
    flow_id = post_json(first, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})["flow_id"]
    stub.calls.clear()

    assert post_json(first, "simplefin-flow-check")["state"] == "pending"
    # Another session sending the same flow's id (a double click, a retried request) shares its spacing.
    assert post_json(second, "simplefin-flow-check", {"flow_id": flow_id})["state"] == "wait"
    assert post_json(second, "simplefin-flow-check", {"flow_id": "not-this-flow"})["state"] == "gone"
    assert len(stub.calls) == 1


@pytest.mark.django_db
def test_starting_again_keeps_the_open_flows_request_count_and_spacing(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=40)))
    sync_connection(owner, connection.pk)
    first, second = signed_in(owner), signed_in(owner)
    post_json(first, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    stub.calls.clear()
    assert post_json(first, "simplefin-flow-check")["state"] == "pending"

    post_json(second, "simplefin-flow-start", {"kind": "add"})

    assert post_json(second, "simplefin-flow-check")["state"] == "wait"
    assert len(stub.calls) == 1
    assert fresh(connection).flow["requests"] == 1


@pytest.mark.django_db
def test_a_replaced_flow_can_neither_check_nor_cancel_the_new_one(monkeypatch):
    now = timezone.now()
    payload = feed(north_at=now - timedelta(hours=40), errlist_extra=[
        {"code": "con.auth", "msg": "Synthetic union sign in again", "conn_id": "CON-C"}])
    owner, connection, stub = connected(monkeypatch, payload)
    sync_connection(owner, connection.pk)
    first, second = signed_in(owner), signed_in(owner)
    post_json(first, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    post_json(second, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-C"})
    stub.calls.clear()

    # The first tab's flow (North) was replaced: it never reports Credit Union fixed.
    assert post_json(first, "simplefin-flow-check")["state"] == "gone"
    assert post_json(first, "simplefin-flow-cancel") == {"state": "cancelled", "message": "Cancelled."}
    assert stub.calls == []
    assert fresh(connection).flow["conn_id"] == "CON-C"

    stub.payload = feed(north_at=now - timedelta(hours=40))
    assert post_json(second, "simplefin-flow-check") == {
        "state": "success", "message": "Synthetic Credit Union is up to date."}


@pytest.mark.django_db
def test_a_newer_balance_date_is_success_even_while_an_error_remains(monkeypatch):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(
        north_at=now - timedelta(hours=40), north_errors=["Synthetic sign in again"]))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)
    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})

    stub.payload = feed(north_at=now - timedelta(minutes=5), north_errors=["Synthetic sign in again"])

    assert post_json(client, "simplefin-flow-check")["state"] == "success"


@pytest.mark.django_db
def test_a_check_stops_when_the_daily_budget_is_used(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=40)))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)
    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    SimpleFinConnection.objects.filter(pk=connection.pk).update(request_log=[timezone.now().isoformat()] * INTERACTIVE_LIMIT)
    stub.calls.clear()

    result = post_json(client, "simplefin-flow-check")

    assert result["state"] == "budget" and "budget" in result["message"]
    assert stub.calls == []


@pytest.mark.django_db
def test_cancel_syncs_once_when_allowed_and_not_right_after_a_check(monkeypatch):
    owner, connection, stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=40)))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)

    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    stub.calls.clear()
    cancelled = post_json(client, "simplefin-flow-cancel")
    assert cancelled["state"] == "cancelled" and cancelled["message"].startswith("Cancelled. Synced ")
    assert len(stub.calls) == 1

    post_json(client, "simplefin-flow-start", {"kind": "fix", "conn_id": "CON-N"})
    post_json(client, "simplefin-flow-check")
    stub.calls.clear()
    cancelled = post_json(client, "simplefin-flow-cancel")
    assert cancelled["message"] == "Cancelled. The next scheduled sync will pick up any changes."
    assert stub.calls == []


@pytest.mark.django_db
def test_add_a_bank_succeeds_when_a_new_bank_appears_ready_to_link(monkeypatch):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(north_at=now))
    sync_connection(owner, connection.pk)
    client = signed_in(owner)
    post_json(client, "simplefin-flow-start", {"kind": "add"})

    assert post_json(client, "simplefin-flow-check")["state"] == "pending"
    rewind_flow(connection, simplefin_flow.FLOW_MIN_SPACING)
    stub.payload = feed(north_at=now, add_bank=True)
    result = post_json(client, "simplefin-flow-check")

    assert result == {"state": "success", "message": "SimpleFIN now lists Synthetic Added Bank. Link its accounts below."}
    html = client.get(reverse("simplefin-connections")).content.decode()
    assert "Synthetic Added Checking" in html  # In the link table, from the stored read.


@pytest.mark.django_db
def test_a_fix_for_an_unknown_bank_is_refused(monkeypatch):
    owner, connection, _stub = connected(monkeypatch, feed(north_at=timezone.now()))
    sync_connection(owner, connection.pk)
    response = signed_in(owner).post(
        reverse("simplefin-flow-start"), json.dumps({"kind": "fix", "conn_id": "CON-X"}), content_type="application/json"
    )
    assert response.status_code == 400


# Health pushes ------------------------------------------------------------------


@pytest.mark.django_db
def test_one_push_when_a_bank_turns_stale_none_on_repeat_and_one_on_recovery(monkeypatch, ntfy, outbox, scheduled_sync):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(north_at=now - timedelta(hours=40)))
    opt_in(owner)

    scheduled_sync(owner, connection)
    scheduled_sync(owner, connection)
    push_health_changes(connection.pk)

    assert [message["title"] for message in outbox] == ["Synthetic North Bank needs attention"]
    assert "has not refreshed Synthetic North Bank since" in outbox[0]["body"]
    assert outbox[0]["topic"] == TOPIC
    assert outbox[0]["click"] == "https://planner.example.invalid" + reverse("simplefin-connections")
    assert "10.00" not in outbox[0]["body"] and "Checking" not in outbox[0]["body"]

    stub.payload = feed(north_at=now - timedelta(minutes=10))
    scheduled_sync(owner, connection)
    scheduled_sync(owner, connection)

    assert [message["title"] for message in outbox] == [
        "Synthetic North Bank needs attention", "Synthetic North Bank is up to date",
    ]
    assert fresh(connection).health_alerts == {"CON-N": OK, "CON-C": OK}


@pytest.mark.django_db
def test_an_error_pushes_once_and_clearing_it_while_still_stale_is_not_a_recovery(monkeypatch, ntfy, outbox, scheduled_sync):
    now = timezone.now()
    owner, connection, stub = connected(monkeypatch, feed(north_at=now - timedelta(hours=40),
                                                          north_errors=["Synthetic sign in again"]))
    opt_in(owner)
    scheduled_sync(owner, connection)
    stub.payload = feed(north_at=now - timedelta(hours=40))
    scheduled_sync(owner, connection)

    assert [message["title"] for message in outbox] == ["Synthetic North Bank needs attention"]
    assert "reports a problem" in outbox[0]["body"]
    assert "Synthetic sign in again" not in outbox[0]["body"]
    assert fresh(connection).health_alerts["CON-N"] == ERROR


@pytest.mark.django_db
def test_the_daily_alert_pass_pushes_a_bank_that_went_stale_without_a_new_read(monkeypatch, ntfy, outbox, scheduled_sync):
    owner, connection, _stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=30)))
    opt_in(owner)
    scheduled_sync(owner, connection)
    assert outbox == []

    later = timezone.now() + timedelta(hours=8)
    monkeypatch.setattr("finance.simplefin_health.timezone.now", lambda: later)
    run_daily_alert_pass()

    assert [message["title"] for message in outbox] == ["Synthetic North Bank needs attention"]


@pytest.mark.django_db
def test_no_push_without_ntfy_and_the_state_waits_for_a_delivered_push(monkeypatch, ntfy, outbox, scheduled_sync):
    owner, connection, _stub = connected(monkeypatch, feed(north_at=timezone.now() - timedelta(hours=40)))
    scheduled_sync(owner, connection)

    assert outbox == []
    assert fresh(connection).health_alerts == {"CON-C": OK}

    opt_in(owner)
    push_health_changes(connection.pk)
    push_health_changes(connection.pk)
    assert len(outbox) == 1
