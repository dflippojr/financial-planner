from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme


RECENT_AUTH_SESSION_KEY = "recent_auth_at"

ACTION_LABELS = {
    "invite": "Invite a household member",
    "leave-household": "Leave the household",
    "connect-google": "Connect Google",
    "disconnect-google": "Disconnect Google",
    "add-password": "Add a password",
    "remove-password": "Remove password",
    "share-account": "Share an account",
    "unshare-account": "Make an account private",
    "change-share-mode": "Change co-owned or lent",
    "delete-account": "Delete an account",
    "export-data": "Download the data export",
    "delete-my-data": "Delete my data",
    "connect-simplefin": "Connect SimpleFIN",
    "disconnect-simplefin": "Disconnect SimpleFIN",
    "connect-ai": "Connect an AI backend",
    "disconnect-ai": "Disconnect the AI backend",
    "ai-defaults": "Change AI backend defaults",
    "ai-offer-local": "Offer the household local model",
    "ai-shared-local": "Use the household local model",
}

ACCOUNT_SETTINGS_ACTIONS = {
    "connect-google": "connect-google",
    "disconnect-google": "disconnect-google",
    "add-password": "add-password",
    "remove-password": "remove-password",
}


def action_label(slug):
    return ACTION_LABELS.get(slug, "this action")


def stamp_recent_auth(request):
    request.session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()


def recent_auth_is_fresh(request):
    raw = request.session.get(RECENT_AUTH_SESSION_KEY)
    try:
        stamped_at = float(raw)
    except (TypeError, ValueError):
        return False
    age = timezone.now().timestamp() - stamped_at
    return 0 <= age <= settings.REAUTH_WINDOW_SECONDS


def safe_next_url(request, target, default=None):
    if default is None:
        default = reverse("home")
    candidate = target or ""
    if not url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return default
    return candidate


def reauth_redirect(request, action, form_url):
    next_url = safe_next_url(request, form_url, default=reverse("home"))
    query = urlencode({"next": next_url, "action": action})
    return redirect(f"{reverse('reauth')}?{query}")


def requires_recent_auth(action, *, form_url_name=None, action_from_post=None, post_field="action"):
    """Refuse a stale POST and send the member to /reauth/ instead.

    GET requests pass through. The member returns to the form page and
    submits again; the original POST is never replayed.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.method != "POST":
                return view_func(request, *args, **kwargs)
            resolved_action = action
            if action_from_post is not None:
                posted = request.POST.get(post_field)
                if posted not in action_from_post:
                    return view_func(request, *args, **kwargs)
                resolved_action = action_from_post[posted]
            if recent_auth_is_fresh(request):
                return view_func(request, *args, **kwargs)
            if form_url_name:
                form_url = reverse(form_url_name)
            else:
                form_url = request.path
            return reauth_redirect(request, resolved_action, form_url)

        return wrapped

    return decorator
