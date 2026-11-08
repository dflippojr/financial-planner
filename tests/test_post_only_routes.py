"""A GET on a POST-only member route returns to a page instead of a 405 (#705).

When a session ends while a page is open and the member submits a POST-only
action, sign-in sends them back to that action's URL with a GET. Every route a
signed-in member can reach therefore answers a GET with something other than
405; a POST-only route (finance.post_only) answers it with a redirect to a
page on this site, changes nothing, and still refuses a POST without a CSRF
token. The routes come from the URL resolver, so a new route is swept as soon
as it exists: test_route_access_sweep requires every route to be classified,
and its tables give the object ids the URLs here need.
"""
import re
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.urls import get_resolver, reverse
from django.utils.http import url_has_allowed_host_and_scheme

from finance.post_only import POST_ONLY_ATTR
from tests.privacy_world import PASSWORD, make_person, signed_in
from tests.test_route_access_sweep import (
    OBJECT_ROUTES,
    PAGE_ROUTES,
    _changes,
    _client,
    _ledger,
    _route_views,
    world,  # noqa: F401  (pytest fixture)
)

# A sample value for each path converter of a route that names no object. The
# guard answers before the view reads the value, and a GET view may 404 on it.
SAMPLE_PATH_VALUES = {"int": "1", "str": "synthetic", "slug": "synthetic", "path": "synthetic"}
_CONVERTER = re.compile(r"<(?:(?P<converter>\w+):)?(?P<name>\w+)>")


def _sample_path(pattern):
    def value(match):
        converter = match.group("converter") or "str"
        assert converter in SAMPLE_PATH_VALUES, f"add a sample value for the <{converter}:> converter"
        return SAMPLE_PATH_VALUES[converter]

    return "/" + _CONVERTER.sub(value, pattern)


def _url(seeded, key, pattern):
    if key in OBJECT_ROUTES:
        route = OBJECT_ROUTES[key]
        return reverse(key, kwargs=route.kwargs(seeded.sides[(route.owner_sides or route.sides)[0]]))
    if key in PAGE_ROUTES:
        return reverse(key, kwargs=PAGE_ROUTES[key].kwargs[0])
    return _sample_path(pattern)


# Every route LoginRequiredMiddleware guards: the ones an expired session sends to sign-in.
MEMBER_ROUTES = [
    (key, pattern, view)
    for key, pattern, view in _route_views(get_resolver().url_patterns)
    if getattr(view, "login_required", True)
]
POST_ONLY_ROUTES = [(key, pattern) for key, pattern, view in MEMBER_ROUTES if hasattr(view, POST_ONLY_ATTR)]


def _assert_returns_to_a_page(client, url, response, before, requester):
    label = f"GET {url}"
    assert response.status_code == 302, f"{label} answered {response.status_code}, not a redirect"
    location = response["Location"]
    assert url_has_allowed_host_and_scheme(location, allowed_hosts={"testserver"}), f"{label} left the site: {location}"
    assert urlparse(location).path != urlparse(url).path, f"{label} redirected to itself"
    changed = _changes(before, _ledger(requester))
    assert not changed, f"{label} changed {changed}"
    # The page's own view may record things of its own, such as a monthly review, so this comes last.
    landed = client.get(location, follow=True)
    assert landed.status_code == 200, f"{label} landed on {landed.status_code} at {location}"


@pytest.mark.parametrize("key, pattern, view", [pytest.param(*route, id=route[0]) for route in MEMBER_ROUTES])
def test_a_signed_in_get_never_answers_405(world, key, pattern, view):  # noqa: F811
    """Sign-in after an expired session lands on any of these with a GET."""
    url = _url(world, key, pattern)
    client = _client(world.owner)
    before = _ledger(world.owner)
    response = client.get(url)
    assert response.status_code != 405, (
        f"GET {url} answered 405; decorate the POST-only view with finance.post_only.post_only"
    )
    if hasattr(view, POST_ONLY_ATTR):
        _assert_returns_to_a_page(client, url, response, before, world.owner)


def test_the_sweep_finds_the_post_only_routes():
    """The sweep below runs on the routes that use the guard; an empty list would check nothing."""
    names = {key for key, _pattern in POST_ONLY_ROUTES}
    assert {"account-sort", "privacy-policy-respond", "investment-notice-dismiss", "account-archive"} <= names


@pytest.mark.parametrize("key, pattern", [pytest.param(*route, id=route[0]) for route in POST_ONLY_ROUTES])
def test_an_expired_session_signs_in_and_lands_on_a_page(world, key, pattern):  # noqa: F811
    """The POST goes to sign-in, signing in follows next= with a GET, and nothing is recorded."""
    url = _url(world, key, pattern)
    before = _ledger(world.owner)
    anonymous = Client()
    refused = anonymous.post(url, {})
    assert refused.status_code == 302, f"the signed-out POST {url} answered {refused.status_code}"
    login = urlparse(refused["Location"])
    assert login.path == reverse("login")
    next_url = parse_qs(login.query)["next"][0]
    assert next_url == url
    assert not _changes(before, _ledger(world.owner)), f"the signed-out POST {url} changed something"

    sign_in = anonymous.post(
        f"{reverse('login')}?{login.query}",
        {"username": world.owner.user.username, "password": PASSWORD, "next": next_url},
    )
    assert sign_in.status_code == 302 and sign_in["Location"] == next_url, "sign-in did not follow next="
    after_sign_in = _ledger(world.owner)
    response = anonymous.get(next_url)
    _assert_returns_to_a_page(anonymous, next_url, response, after_sign_in, world.owner)


@pytest.mark.parametrize("key, pattern", [pytest.param(*route, id=route[0]) for route in POST_ONLY_ROUTES])
def test_a_post_without_a_csrf_token_is_still_refused(world, key, pattern):  # noqa: F811
    url = _url(world, key, pattern)
    client = Client(enforce_csrf_checks=True)
    client.force_login(world.owner.user)
    before = _ledger(world.owner)
    response = client.post(url, {})
    assert response.status_code == 403, f"POST {url} without a CSRF token answered {response.status_code}"
    assert not _changes(before, _ledger(world.owner)), f"the refused POST {url} changed something"


@pytest.mark.django_db
def test_the_guard_prefers_a_safe_next_and_refuses_other_methods():
    client = signed_in(make_person("owner"))
    url = reverse("account-sort")
    response = client.get(url)
    assert response.status_code == 302 and response["Location"] == reverse("account-list")
    assert "no-store" in response["Cache-Control"]
    assert client.get(url, {"next": "/transactions/?q=SYN"})["Location"] == "/transactions/?q=SYN"
    assert client.get(url, {"next": "https://elsewhere.example/"})["Location"] == reverse("account-list")
    assert client.get(url, {"next": "//elsewhere.example/"})["Location"] == reverse("account-list")
    assert client.head(url)["Location"] == reverse("account-list")
    assert client.put(url).status_code == 405
    assert client.delete(url).status_code == 405


@pytest.mark.django_db
def test_a_get_on_sign_out_keeps_the_member_signed_in():
    client = signed_in(make_person("owner"))
    response = client.get(reverse("logout"))
    assert response.status_code == 302 and response["Location"] == reverse("home")
    assert client.get(reverse("home")).status_code == 200
    assert client.post(reverse("logout"))["Location"] == reverse("login")
    assert client.get(reverse("home")).status_code == 302
