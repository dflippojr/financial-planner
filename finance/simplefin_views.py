from datetime import date

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .category_services import current_household
from .forms import SimpleFinSetupForm
from .models import Account, AccountLink, Person, SimpleFinConnection
from .simplefin_errors import SimpleFinError
from .simplefin_schedule import next_scheduled_sync
from .simplefin_services import (
    claim_connection,
    default_cutover_date,
    disconnect_connection,
    load_remote_accounts,
    save_account_links,
    sync_connection,
)


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _linkable_accounts(person):
    return list(
        Account.objects.visible_to(person)
        .filter(status=Account.Status.ACTIVE, archived_at__isnull=True)
        .order_by("name", "pk")
    )


def _choices_from_post(request, remotes):
    choices = []
    for index, remote in enumerate(remotes):
        posted_id = request.POST.get(f"sf_id_{index}", "")
        if posted_id != remote["id"]:
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


@require_http_methods(["GET", "POST"])
@never_cache
def connections(request):
    person = _person(request)
    household = current_household(person)
    connection = SimpleFinConnection.objects.filter(owner=person).first()
    form = SimpleFinSetupForm(request.POST if request.POST.get("intent") == "claim" else None)
    remotes = []
    provider_messages = []
    load_error = ""
    if connection is not None:
        try:
            remotes, provider_messages = load_remote_accounts(connection)
        except SimpleFinError as exc:
            load_error = str(exc)
    if request.method == "POST" and request.POST.get("intent") == "claim":
        if connection is not None:
            messages.error(request, "Disconnect the existing SimpleFIN connection before adding another.")
            return redirect("simplefin-connections")
        if form.is_valid():
            try:
                claim_connection(request.user, form.cleaned_data["token"])
                messages.success(request, "SimpleFIN is connected. Link the accounts below.")
                return redirect("simplefin-connections")
            except SimpleFinError as exc:
                form.add_error("token", str(exc))
            except PermissionDenied as exc:
                raise Http404 from exc
    if request.method == "POST" and request.POST.get("intent") == "link" and connection is not None:
        if not remotes:
            messages.error(request, load_error or "SimpleFIN accounts could not be listed.")
            return redirect("simplefin-connections")
        try:
            save_account_links(request.user, connection.pk, _choices_from_post(request, remotes))
            sync_connection(request.user, connection.pk, ignore_rate_limit=True)
            messages.success(request, "Account links saved and synced.")
            return redirect("simplefin-connections")
        except SimpleFinError as exc:
            messages.error(request, str(exc))
        except PermissionDenied as exc:
            raise Http404 from exc
        except ValueError:
            messages.error(request, "Check the cut-over dates and try again.")

    link_map = {}
    if connection is not None:
        link_map = {
            link.simplefin_account_id: link
            for link in AccountLink.objects.select_related("account").filter(connection=connection)
        }
    accounts = _linkable_accounts(person)
    fallback_cutover = timezone.localdate().isoformat()
    rows = []
    for remote in remotes:
        link = link_map.get(remote["id"])
        if link is not None:
            cutover = link.cutover_date.isoformat()
        elif accounts:
            cutover = default_cutover_date(accounts[0]).isoformat()
        else:
            cutover = fallback_cutover
        rows.append({**remote, "link": link, "default_cutover": cutover})

    next_sync = None
    can_sync_now = False
    if connection is not None:
        try:
            next_sync = next_scheduled_sync(settings.SIMPLEFIN_SYNC_CRON)
        except ValueError:
            next_sync = None
        can_sync_now = connection.last_sync_at is None or (
            (timezone.now() - connection.last_sync_at).total_seconds()
            >= settings.SIMPLEFIN_SYNC_MIN_INTERVAL_SECONDS
        )

    return render(
        request,
        "finance/connections.html",
        {
            "form": form,
            "connection": connection,
            "rows": rows,
            "accounts": accounts,
            "has_household": household is not None,
            "provider_messages": provider_messages,
            "load_error": load_error,
            "next_sync": next_sync,
            "can_sync_now": can_sync_now,
            "account_types": Account.Type.choices,
        },
    )


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
def connections_disconnect(request):
    person = _person(request)
    connection = SimpleFinConnection.objects.filter(owner=person).first()
    if connection is None:
        raise Http404
    try:
        disconnect_connection(request.user, connection.pk)
    except PermissionDenied as exc:
        raise Http404 from exc
    messages.success(request, "SimpleFIN disconnected. Imported transactions and balances were kept.")
    return redirect("simplefin-connections")
