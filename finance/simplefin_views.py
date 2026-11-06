import json
from datetime import date, datetime

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .access import request_person as _person
from .category_services import current_household
from .forms import SimpleFinSetupForm
from .models import Account, AccountLink, SimpleFinConnection
from .reauth import requires_recent_auth
from .security_services import EVENT_TYPES, record_security_event
from .simplefin_budget import DAILY_REQUEST_CAP, SimpleFinBudgetExceeded, interactive_requests_left, requests_used
from .simplefin_errors import SimpleFinAlreadyClaimed, SimpleFinError, SimpleFinRateLimited
from .simplefin_flow import BRIDGE_URL, FlowError, cancel_flow, check_flow, start_flow
from .simplefin_health import bank_rows
from .simplefin_schedule import next_scheduled_sync
from .simplefin_services import (
    claim_connection,
    disconnect_connection,
    load_remote_accounts,
    save_account_links,
    stored_listing,
    sync_connection,
)

# The Fix and Add pop-ups must stay reachable from this page so it can see
# them close and close them itself. Django's default, same-origin, would put a
# cross-origin pop-up in another browsing context group, where it reads as
# closed at once and window.close() does nothing.
OPENER_POLICY = "same-origin-allow-popups"


def _linkable_accounts(person):
    return list(
        Account.objects.visible_to(person)
        .filter(status=Account.Status.ACTIVE, archived_at__isnull=True, account_type__in=Account.SIMPLEFIN_TYPES)
        .order_by("name", "pk")
    )


def _posted_indexes(request):
    indexes = []
    for key in request.POST:
        if key.startswith("sf_id_") and key[len("sf_id_"):].isdigit():
            indexes.append(int(key[len("sf_id_"):]))
    return sorted(indexes)


def _choices_from_post(request, remotes):
    # Match each posted row to a remote account by its id, never by position:
    # SimpleFIN may list accounts in a different order on the next fetch.
    remotes_by_id = {remote["id"]: remote for remote in remotes}
    choices = []
    for index in _posted_indexes(request):
        remote = remotes_by_id.get(request.POST.get(f"sf_id_{index}", ""))
        if remote is None:
            continue
        action = request.POST.get(f"action_{index}", "ignore")
        cutover_raw = request.POST.get(f"cutover_{index}", "")
        cutover = date.fromisoformat(cutover_raw) if cutover_raw else None
        account_id = request.POST.get(f"account_id_{index}", "")
        choices.append(
            {
                "simplefin_account_id": remote["id"],
                "action": action,
                "account_id": int(account_id) if account_id.isdigit() else None,
                "cutover_date": cutover,
                "name": (request.POST.get(f"name_{index}", "") or remote["name"]).strip(),
                "account_type": request.POST.get(f"account_type_{index}", Account.Type.CHECKING),
                "sharing": request.POST.get(f"sharing_{index}", Account.Scope.PRIVATE),
            }
        )
    return choices


def _stored_messages(connection):
    read = connection.last_read if isinstance(connection.last_read, dict) else {}
    found = read.get("messages")
    return [message for message in found if isinstance(message, str)] if isinstance(found, list) else []


def _remote_accounts(connection):
    """The account list from the newest stored read; read once if none is stored yet.

    The page otherwise makes no SimpleFIN request: every request draws on the
    connection's small daily budget.
    """
    if connection is None:
        return [], [], ""
    remotes = stored_listing(connection)
    if remotes is not None:
        return remotes, _stored_messages(connection), ""
    try:
        remotes, provider_messages = load_remote_accounts(connection)
        connection.refresh_from_db()
        return remotes, provider_messages, ""
    except SimpleFinError as exc:
        return [], [], str(exc)


def _handle_claim(request, connection, form):
    if connection is not None:
        messages.error(request, "Disconnect the existing SimpleFIN connection before adding another.")
        return redirect("simplefin-connections")
    if not form.is_valid():
        return None
    try:
        claim_connection(request.user, form.cleaned_data["token"])
    except SimpleFinAlreadyClaimed as exc:
        # Usually a double-clicked Connect: the other request claimed the token.
        if SimpleFinConnection.objects.filter(owner=_person(request)).exists():
            messages.success(request, "SimpleFIN is connected. Link the accounts below.")
        else:
            messages.info(request, str(exc))
        return redirect("simplefin-connections")
    except SimpleFinError as exc:
        form.add_error("token", str(exc))
        return None
    except PermissionDenied as exc:
        raise Http404 from exc
    record_security_event(request.user, EVENT_TYPES.SIMPLEFIN_CONNECTION_CHANGED, request=request)
    messages.success(request, "SimpleFIN is connected. Link the accounts below.")
    return redirect("simplefin-connections")


def _handle_link(request, connection, remotes, load_error):
    if not remotes:
        messages.error(request, load_error or "SimpleFIN accounts could not be listed.")
        return redirect("simplefin-connections")
    try:
        save_account_links(request.user, connection.pk, _choices_from_post(request, remotes))
        sync_connection(request.user, connection.pk)
    except SimpleFinBudgetExceeded:
        messages.success(request, "Account links saved. They sync on the next scheduled run.")
        return redirect("simplefin-connections")
    except SimpleFinRateLimited:
        # Saving links never bypasses the manual sync limit.
        messages.success(request, "Account links saved. They sync on the next scheduled run, or with Sync now once the 15-minute wait is over.")
        return redirect("simplefin-connections")
    except SimpleFinError as exc:
        messages.error(request, str(exc))
        return None
    except PermissionDenied as exc:
        raise Http404 from exc
    except ValueError:
        messages.error(request, "Check the cut-over dates and try again.")
        return None
    messages.success(request, "Account links saved and synced.")
    return redirect("simplefin-connections")


def _cutover_for(remote, link_map):
    link = link_map.get(remote["id"])
    if link is not None:
        return link, link.cutover_date.isoformat()
    # Leave it blank for unlinked rows: the server defaults to the day after the
    # *selected* account's latest transaction, which a prefill cannot know.
    return None, ""


def _schedule_state(connection):
    if connection is None:
        return None, False
    try:
        next_sync = next_scheduled_sync(settings.SIMPLEFIN_SYNC_CRON)
    except ValueError:
        next_sync = None
    can_sync_now = connection.last_sync_at is None or (
        (timezone.now() - connection.last_sync_at).total_seconds()
        >= settings.SIMPLEFIN_SYNC_MIN_INTERVAL_SECONDS
    )
    return next_sync, can_sync_now and interactive_requests_left(connection) > 0


@require_http_methods(["GET", "POST"])
@never_cache
@requires_recent_auth(
    "connect-simplefin",
    form_url_name="simplefin-connections",
    action_from_post={"claim": "connect-simplefin"},
    post_field="intent",
)
def connections(request):
    person = _person(request)
    connection = SimpleFinConnection.objects.filter(owner=person).first()
    form = SimpleFinSetupForm(request.POST if request.POST.get("intent") == "claim" else None)
    remotes, provider_messages, load_error = _remote_accounts(connection)
    if request.method == "POST" and request.POST.get("intent") == "claim":
        response = _handle_claim(request, connection, form)
        if response is not None:
            return response
    if request.method == "POST" and request.POST.get("intent") == "link" and connection is not None:
        response = _handle_link(request, connection, remotes, load_error)
        if response is not None:
            return response
    link_map = {
        link.simplefin_account_id: link
        for link in AccountLink.objects.select_related("account").filter(connection=connection)
    } if connection is not None else {}
    accounts = _linkable_accounts(person)
    listed_ids = {account.pk for account in accounts}
    rows = []
    for remote in remotes:
        link, cutover = _cutover_for(remote, link_map)
        # A link to an archived or hidden account cannot be shown in the
        # account list; keep it unchanged unless the member picks another action.
        unlisted = link is not None and link.account_id not in listed_ids
        rows.append({**remote, "link": link, "link_unlisted": unlisted, "default_cutover": cutover})
    next_sync, can_sync_now = _schedule_state(connection)
    response = render(
        request,
        "finance/connections.html",
        {
            "form": form,
            "connection": connection,
            "rows": rows,
            "accounts": accounts,
            "has_household": current_household(person) is not None,
            "provider_messages": provider_messages,
            "load_error": load_error,
            "next_sync": next_sync,
            "can_sync_now": can_sync_now,
            "account_types": [
                (value, label) for value, label in Account.Type.choices if value in Account.SIMPLEFIN_TYPES
            ],
            **_health_context(connection),
        },
    )
    response["Cross-Origin-Opener-Policy"] = OPENER_POLICY
    return response


def _health_context(connection):
    if connection is None:
        return {}
    read = connection.last_read if isinstance(connection.last_read, dict) else {}
    return {
        "banks": bank_rows(connection),
        "read_at": _parse_time(read.get("at")),
        "requests_used": requests_used(connection),
        "request_cap": DAILY_REQUEST_CAP,
        "budget_left": interactive_requests_left(connection),
        "bridge_url": BRIDGE_URL,
    }


def _parse_time(value):
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _owned_connection(request):
    connection = SimpleFinConnection.objects.filter(owner=_person(request)).first()
    if connection is None:
        raise Http404
    return connection


def _json_body(request):
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _flow_response(request, result):
    """JSON for the page; a final outcome is also kept as a message for the reload that follows."""
    message = result.get("message")
    if message and result.get("state") in {"success", "exhausted", "budget", "cancelled"}:
        level = messages.success if result["state"] in {"success", "cancelled"} else messages.info
        level(request, message)
    return JsonResponse(result)


@require_POST
@never_cache
def connections_flow_start(request):
    connection = _owned_connection(request)
    body = _json_body(request)
    try:
        result = start_flow(request.session, connection, str(body.get("kind") or ""), str(body.get("conn_id") or ""))
    except FlowError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)
    return JsonResponse(result)


@require_POST
@never_cache
def connections_flow_check(request):
    connection = _owned_connection(request)
    try:
        return _flow_response(request, check_flow(request.session, request.user, connection))
    except PermissionDenied as exc:
        raise Http404 from exc


@require_POST
@never_cache
def connections_flow_cancel(request):
    connection = _owned_connection(request)
    try:
        return _flow_response(request, cancel_flow(request.session, request.user, connection))
    except PermissionDenied as exc:
        raise Http404 from exc


@require_POST
@never_cache
def connections_sync(request):
    person = _person(request)
    connection = SimpleFinConnection.objects.filter(owner=person).first()
    if connection is None:
        raise Http404
    try:
        result = sync_connection(request.user, connection.pk)
        messages.success(request, result["result"])
    except SimpleFinError as exc:
        messages.error(request, str(exc))
    except PermissionDenied as exc:
        raise Http404 from exc
    return redirect("simplefin-connections")


@require_POST
@never_cache
@requires_recent_auth("disconnect-simplefin", form_url_name="simplefin-connections")
def connections_disconnect(request):
    person = _person(request)
    connection = SimpleFinConnection.objects.filter(owner=person).first()
    if connection is None:
        raise Http404
    try:
        disconnect_connection(request.user, connection.pk)
    except PermissionDenied as exc:
        raise Http404 from exc
    record_security_event(request.user, EVENT_TYPES.SIMPLEFIN_CONNECTION_CHANGED, request=request)
    messages.success(request, "SimpleFIN disconnected. Imported transactions and balances were kept.")
    return redirect("simplefin-connections")
