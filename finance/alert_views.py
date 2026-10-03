from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .alert_services import alerts_for, mark_alert_read, mark_all_alerts_read
from .models import Person


@require_http_methods(["GET"])
@never_cache
def alert_list(request):
    person = get_object_or_404(Person, user=request.user)
    return render(
        request,
        "finance/alerts.html",
        {"alerts": list(alerts_for(person))},
    )


@require_POST
@never_cache
def alert_mark_read(request, alert_id):
    try:
        mark_alert_read(request.user, alert_id)
    except PermissionDenied as exc:
        raise Http404 from exc
    return redirect("alert-list")


@require_POST
@never_cache
def alert_mark_all_read(request):
    mark_all_alerts_read(request.user)
    return redirect("alert-list")
