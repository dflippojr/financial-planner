"""The Fix and Add pop-up flows on Settings -> Connections (#694).

SimpleFIN's API is read-only, so banks are fixed and added on SimpleFIN Bridge
in a pop-up the app cannot see into. The app learns the outcome only from
later /accounts reads: a Fix succeeds when the bank's error is gone or a
newer balance-date appears; an Add succeeds when a bank it had not listed
before appears.

Each check is a full sync, so a successful check has already imported what
changed and needs no second request. One flow makes at most
FLOW_MAX_REQUESTS requests (checks plus the closing sync on Cancel), at
least FLOW_MIN_SPACING seconds apart, and every one of them also draws on the
connection's daily budget (finance.simplefin_budget). The flow is kept on
the connection row (one flow at a time per connection) and every request is
claimed under that row's lock, so overlapping checks from two tabs or a
double click can never pass the per-flow limits. The daily budget is the hard
limit across flows.
"""

from __future__ import annotations

import time

from django.db import transaction

from finance.models import SimpleFinConnection
from finance.simplefin_budget import BUDGET_USED, SimpleFinBudgetExceeded, interactive_requests_left
from finance.simplefin_errors import SimpleFinError
from finance.simplefin_health import balance_moment, find_bank, stored_banks

# SimpleFIN documents no deep link for re-authenticating one bank or for
# adding an institution, and `connections[]` carries no manage URL
# (`sfin_url` is the server root). Bridge's home page is the most specific
# documented page: after sign-in it lists the member's banks with their fix
# and add controls. bridge.simplefin.org currently redirects to
# beta-bridge.simplefin.org; the canonical host keeps working if that changes.
BRIDGE_URL = "https://bridge.simplefin.org/"

FLOW_MAX_REQUESTS = 4
FLOW_MIN_SPACING = 60
# Seconds before the first automatic check, then between later ones.
CHECK_DELAYS = (120, 180, 300)
FLOW_TTL = 60 * 60
FIX, ADD = "fix", "add"

NOT_REFRESHED = "SimpleFIN hasn't refreshed yet; we'll pick it up at the next sync."
NO_NEW_BANK = "SimpleFIN hasn't listed a new bank yet; we'll pick it up at the next sync."
NO_FLOW = "That SimpleFIN window is no longer being watched. Reload the page to start again."


class FlowError(Exception):
    """The request does not match a flow this member may start."""


def _now() -> float:
    return time.time()


def _locked(connection_id):
    return SimpleFinConnection.objects.select_for_update().get(pk=connection_id)


def _live(flow) -> dict | None:
    if not isinstance(flow, dict) or not flow.get("kind"):
        return None
    if _now() - float(flow.get("started") or 0) > FLOW_TTL:
        return None
    return flow


def start_flow(connection, kind: str, conn_id: str = "") -> dict:
    """Start the connection's flow with a baseline from the stored read."""
    with transaction.atomic():
        connection = _locked(connection.pk)
        if kind == FIX:
            bank = find_bank(connection, conn_id)
            if bank is None:
                raise FlowError("That bank is not in the last SimpleFIN read.")
            baseline = {"balance_date": bank.get("balance_date"), "had_errors": bool(bank.get("errors"))}
            name = bank.get("name") or "The bank"
        elif kind == ADD:
            baseline = {"conn_ids": [bank.get("conn_id") for bank in stored_banks(connection)]}
            name, conn_id = "", ""
        else:
            raise FlowError("Unknown SimpleFIN action.")
        # A flow still open (another tab, or a second Fix before the first
        # finished) hands on its request count and spacing: a new baseline
        # never buys new requests.
        active = _live(connection.flow) or {}
        flow = {"kind": kind, "conn_id": conn_id, "name": name, "baseline": baseline, "started": _now(),
                "requests": int(active.get("requests") or 0), "last_request": active.get("last_request")}
        SimpleFinConnection.objects.filter(pk=connection.pk).update(flow=flow)
    return {"ok": True, "url": BRIDGE_URL, "first_check_in": CHECK_DELAYS[0], "max_checks": FLOW_MAX_REQUESTS}


def _end(connection_id, flow) -> None:
    """Clear the flow, unless a newer one has started since."""
    with transaction.atomic():
        current = _locked(connection_id).flow
        if isinstance(current, dict) and current.get("started") == flow.get("started"):
            SimpleFinConnection.objects.filter(pk=connection_id).update(flow={})


def _spacing_left(flow) -> int:
    last = flow.get("last_request")
    if last is None:
        return 0
    elapsed = _now() - float(last)
    return 0 if elapsed >= FLOW_MIN_SPACING else int(FLOW_MIN_SPACING - elapsed) + 1


def _claim(connection_id, *, closing: bool):
    """Under the row lock, decide whether the flow may send one more request, and record it.

    Returns (decision, flow, wait): decision is "go", "gone", "exhausted",
    "wait" or "budget". A closing claim (Cancel) always ends the flow.
    """
    with transaction.atomic():
        connection = _locked(connection_id)
        flow = _live(connection.flow)
        if flow is None:
            decision, wait = "gone", 0
        elif flow["requests"] >= FLOW_MAX_REQUESTS:
            decision, wait = "exhausted", 0
        elif _spacing_left(flow):
            decision, wait = "wait", _spacing_left(flow)
        elif interactive_requests_left(connection) <= 0:
            decision, wait = "budget", 0
        else:
            decision, wait = "go", 0
            flow = {**flow, "requests": flow["requests"] + 1, "last_request": _now()}
        ends = closing or decision in {"gone", "exhausted", "budget"}
        SimpleFinConnection.objects.filter(pk=connection_id).update(flow={} if ends else flow)
    return decision, flow, wait


def _sync(user, connection) -> str:
    """One sync for the flow: "ok", "failed" (recorded on the connection) or the budget message."""
    from finance.simplefin_services import sync_connection

    try:
        sync_connection(user, connection.pk, ignore_rate_limit=True, scheduled=False)
    except SimpleFinBudgetExceeded as exc:
        return str(exc)
    except SimpleFinError:
        connection.refresh_from_db()
        return "failed"
    connection.refresh_from_db()
    return "ok"


def _fixed(flow, connection) -> bool:
    bank = find_bank(connection, flow["conn_id"])
    if bank is None or bank.get("errors"):
        return False
    baseline = flow["baseline"]
    if baseline.get("had_errors"):
        return True
    newest = balance_moment(bank)
    before = balance_moment(baseline)
    return newest is not None and (before is None or newest > before)


def _new_banks(flow, connection) -> list[dict]:
    known = set(flow["baseline"].get("conn_ids") or [])
    return [bank for bank in stored_banks(connection) if bank.get("conn_id") not in known]


def _success_message(flow, connection) -> str | None:
    if flow["kind"] == FIX:
        return f"{flow['name']} is up to date." if _fixed(flow, connection) else None
    added = _new_banks(flow, connection)
    if not added:
        return None
    names = ", ".join(bank.get("name") or "A bank" for bank in added)
    return f"SimpleFIN now lists {names}. Link its accounts below."


def _gave_up_message(flow) -> str:
    return NOT_REFRESHED if flow["kind"] == FIX else NO_NEW_BANK


def check_flow(user, connection) -> dict:
    decision, flow, wait = _claim(connection.pk, closing=False)
    if decision == "gone" or flow is None:
        return {"state": "gone", "message": NO_FLOW}
    if decision == "exhausted":
        return {"state": "exhausted", "message": _gave_up_message(flow)}
    if decision == "wait":
        return {"state": "wait", "retry_in": wait}
    if decision == "budget":
        return {"state": "budget", "message": BUDGET_USED}
    outcome = _sync(user, connection)
    if outcome not in {"ok", "failed"}:
        _end(connection.pk, flow)
        return {"state": "budget", "message": outcome}
    # Success needs a sync that completed: a failed import leaves the bank unconfirmed.
    message = _success_message(flow, connection) if outcome == "ok" else None
    if message is not None:
        _end(connection.pk, flow)
        return {"state": "success", "message": message}
    if flow["requests"] >= FLOW_MAX_REQUESTS:
        _end(connection.pk, flow)
        return {"state": "exhausted", "message": _gave_up_message(flow)}
    delay = CHECK_DELAYS[min(flow["requests"], len(CHECK_DELAYS) - 1)]
    return {"state": "pending", "next_check_in": delay, "checks_left": FLOW_MAX_REQUESTS - flow["requests"]}


def cancel_flow(user, connection) -> dict:
    """Stop watching; sync once if the flow and the daily budget still allow it."""
    decision, flow, _wait = _claim(connection.pk, closing=True)
    if flow is None:
        return {"state": "cancelled", "message": "Cancelled."}
    if decision != "go":
        return {"state": "cancelled", "message": "Cancelled. The next scheduled sync will pick up any changes."}
    outcome = _sync(user, connection)
    if outcome not in {"ok", "failed"}:
        return {"state": "cancelled", "message": f"Cancelled. {outcome}"}
    message = _success_message(flow, connection) if outcome == "ok" else None
    if message is not None:
        return {"state": "cancelled", "message": message}
    return {"state": "cancelled", "message": f"Cancelled. {connection.last_sync_result}".strip()}
