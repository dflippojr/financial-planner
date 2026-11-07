"""Dismissing the investment-activity notice (#699)."""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .models import Person
from .notice_dismissal import dismiss
from .reauth import safe_next_url

STALE_NOTICE = "The notice has changed. Reload the page to read it."


@require_POST
@never_cache
def investment_notice_dismiss(request):
    """Hide the notice for the signed-in member on every device, then return to the page.

    Script sends Accept: application/json and gets JSON; a plain form POST is
    redirected to the page it came from, query string included.
    """
    person = get_object_or_404(Person, user=request.user)
    dismissed = dismiss(person, request.POST.get("notice", ""))
    if "application/json" in request.headers.get("Accept", ""):
        if not dismissed:
            return JsonResponse({"dismissed": False, "error": STALE_NOTICE}, status=409)
        return JsonResponse({"dismissed": True})
    return redirect(safe_next_url(request, request.POST.get("next", "")))
