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
connection's daily budget (finance.simplefin_budget). The flow lives in the
member's session; the daily budget is the hard limit across flows.
"""

from __future__ import annotations

import time

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

SESSION_KEY = "simplefin_flow"
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


def start_flow(session, connection, kind: str, conn_id: str = "") -> dict:
    if kind == FIX:
        bank = find_bank(connection, conn_id)
        if bank is None:
            raise FlowError("That bank is not in the last SimpleFIN read.")
        baseline = {"balance_date": bank.get("balance_date"), "had_errors": bool(bank.get("errors"))}
        name = bank.get("name") or "The bank"
    elif kind == ADD:
        baseline = {"conn_ids": [bank.get("conn_id") for bank in stored_banks(connection)]}
        name = ""
        conn_id = ""
    else:
        raise FlowError("Unknown SimpleFIN action.")
    session[SESSION_KEY] = {
        "connection": connection.pk,
        "kind": kind,
        "conn_id": conn_id,
        "name": name,
        "baseline": baseline,
        "started": _now(),
        "requests": 0,
        "last_request": None,
    }
    return {"ok": True, "url": BRIDGE_URL, "first_check_in": CHECK_DELAYS[0], "max_checks": FLOW_MAX_REQUESTS}


def _current_flow(session, connection) -> dict | None:
    flow = session.get(SESSION_KEY)
    if not isinstance(flow, dict) or flow.get("connection") != connection.pk:
        return None
    if _now() - float(flow.get("started") or 0) > FLOW_TTL:
        session.pop(SESSION_KEY, None)
        return None
    return flow


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


def _spacing_left(flow) -> int:
    last = flow.get("last_request")
    if last is None:
        return 0
    elapsed = _now() - float(last)
    return 0 if elapsed >= FLOW_MIN_SPACING else int(FLOW_MIN_SPACING - elapsed) + 1


def _sync(session, user, connection, flow) -> str:
    """Spend one of the flow's requests on a sync. Returns a problem message or ""."""
    from finance.simplefin_services import sync_connection

    flow["requests"] += 1
    flow["last_request"] = _now()
    session[SESSION_KEY] = flow
    # Saved before the slow request, so a second tab sees the spent request.
    session.save()
    try:
        sync_connection(user, connection.pk, ignore_rate_limit=True, scheduled=False)
    except SimpleFinBudgetExceeded as exc:
        return str(exc)
    except SimpleFinError:
        pass  # Recorded on the connection like any failed sync; the flow keeps checking.
    connection.refresh_from_db()
    return ""


def check_flow(session, user, connection) -> dict:
    flow = _current_flow(session, connection)
    if flow is None:
        return {"state": "gone", "message": NO_FLOW}
    if flow["requests"] >= FLOW_MAX_REQUESTS:
        session.pop(SESSION_KEY, None)
        return {"state": "exhausted", "message": _gave_up_message(flow)}
    wait = _spacing_left(flow)
    if wait:
        return {"state": "wait", "retry_in": wait}
    if interactive_requests_left(connection) <= 0:
        session.pop(SESSION_KEY, None)
        return {"state": "budget", "message": BUDGET_USED}
    problem = _sync(session, user, connection, flow)
    if problem:
        session.pop(SESSION_KEY, None)
        return {"state": "budget", "message": problem}
    message = _success_message(flow, connection)
    if message is not None:
        session.pop(SESSION_KEY, None)
        return {"state": "success", "message": message}
    if flow["requests"] >= FLOW_MAX_REQUESTS:
        session.pop(SESSION_KEY, None)
        return {"state": "exhausted", "message": _gave_up_message(flow)}
    delay = CHECK_DELAYS[min(flow["requests"], len(CHECK_DELAYS) - 1)]
    return {"state": "pending", "next_check_in": delay, "checks_left": FLOW_MAX_REQUESTS - flow["requests"]}


def cancel_flow(session, user, connection) -> dict:
    """Stop watching; sync once if the flow and the daily budget still allow it."""
    flow = _current_flow(session, connection)
    if flow is None:
        return {"state": "cancelled", "message": "Cancelled."}
    can_sync = (
        flow["requests"] < FLOW_MAX_REQUESTS
        and not _spacing_left(flow)
        and interactive_requests_left(connection) > 0
    )
    if not can_sync:
        session.pop(SESSION_KEY, None)
        return {"state": "cancelled", "message": "Cancelled. The next scheduled sync will pick up any changes."}
    problem = _sync(session, user, connection, flow)
    session.pop(SESSION_KEY, None)
    if problem:
        return {"state": "cancelled", "message": f"Cancelled. {problem}"}
    message = _success_message(flow, connection)
    if message is not None:
        return {"state": "cancelled", "message": message}
    return {"state": "cancelled", "message": f"Cancelled. {connection.last_sync_result}".strip()}
