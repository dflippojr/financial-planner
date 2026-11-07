"""Dismissing the investment-activity notice (#699)."""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Person
from .notice_dismissal import dismiss
from .reauth import safe_next_url

STALE_NOTICE = "The notice has changed. Reload the page to read it."


@require_http_methods(["GET", "POST"])
@never_cache
def investment_notice_dismiss(request):
    """Hide the notice for the signed-in member on every device, then return to the page.

    The form's action carries the page it came from as ?next= (path and query
    string). Script sends Accept: application/json and gets JSON; a plain form
    POST is redirected to that page. A GET changes nothing and only returns to
    the page: it is where sign-in lands when the session ended before the POST.
    """
    back = safe_next_url(request, request.GET.get("next", ""))
    if request.method == "GET":
        return redirect(back)
    person = get_object_or_404(Person, user=request.user)
    dismissed = dismiss(person, request.POST.get("notice", ""))
    if "application/json" in request.headers.get("Accept", ""):
        if not dismissed:
            return JsonResponse({"dismissed": False, "error": STALE_NOTICE}, status=409)
        return JsonResponse({"dismissed": True})
    return redirect(back)
