"""POST-only member actions that answer a GET by returning to a page (#705).

When a session ends while a page is open, the member's next POST goes to
sign-in with the action's URL as next=, and sign-in then sends them there with
a GET. require_POST would answer that GET with a 405 page. post_only answers
it with a redirect to a page instead and never runs the view, so a GET never
changes data. A POST reaches the view exactly as before, after the CSRF
middleware and every decorator beneath this one. Other methods still get 405.
"""
from functools import wraps

from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_POST

from .reauth import safe_next_url

# Marks a view wrapped by post_only; tests/test_post_only_routes.py finds the routes by it.
POST_ONLY_ATTR = "post_only_back"


def post_only(back="home", *path_kwargs):
    """Accept POST only; a GET or HEAD returns to a page and changes nothing.

    back names the page to return to, usually where the action's form lives
    or where the view redirects after it succeeds. path_kwargs names the URL
    arguments passed on to it, such as an account id, taken from the path
    without looking the object up, so the answer is the same whether or not
    the member may see it; the page's own view checks access. A safe ?next=
    on the request wins, as for the investment notice's form (#699).
    """

    def decorator(view_func):
        post_view = require_POST(view_func)

        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.method not in ("GET", "HEAD"):
                return post_view(request, *args, **kwargs)
            page = reverse(back, kwargs={name: kwargs[name] for name in path_kwargs})
            response = redirect(safe_next_url(request, request.GET.get("next", ""), default=page))
            add_never_cache_headers(response)
            return response

        setattr(wrapped, POST_ONLY_ATTR, back)
        return wrapped

    return decorator
