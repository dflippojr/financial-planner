from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from finance.context_processors import navigation
from tests.a11y import PageParser, accessibility_errors, assert_accessible


@pytest.mark.parametrize("snippet", [
    '<label for="x">Name</label><input id="x">',
    '<label>Name <select><option>One</option></select></label>',
    '<textarea aria-label="Notes"></textarea>',
    '<input aria-labelledby="a b"><span id="a">First</span><span id="b">Last</span>',
    '<input type="hidden"><input type="submit"><input type="button">',
    '<div hidden><input></div><input aria-hidden="true"><div class="hidden"><select></select></div>',
    '<button><span>Save</span></button><a href="/">Home</a>',
    '<button aria-label="Close"><svg aria-hidden="true"></svg></button>',
    '<img alt=""><img alt="Synthetic illustration">',
    '<table><thead><tr><th scope="col">Date</th></tr></thead><tbody><tr><th scope="row">Today</th></tr></tbody></table>',
    '<span id="first"></span><span id="second"></span>',
])
def test_valid_accessibility_snippets(snippet):
    assert_accessible('<h1>Page</h1>' + snippet)


@pytest.mark.parametrize(("snippet", "error"), [
    ('', 'exactly one h1'),
    ('<h1>One</h1><h1>Two</h1>', 'exactly one h1'),
    ('<h1>Page</h1><input>', 'control has no accessible name'),
    ('<h1>Page</h1><select></select>', 'control has no accessible name'),
    ('<h1>Page</h1><textarea></textarea>', 'control has no accessible name'),
    ('<h1>Page</h1><input aria-label=" ">', 'control has no accessible name'),
    ('<h1>Page</h1><label for="other">Name</label><input id="x">', 'control has no accessible name'),
    ('<h1>Page</h1><label><input></label>', 'control has no accessible name'),
    ('<h1>Page</h1><input aria-labelledby="missing">', 'control has no accessible name'),
    ('<h1>Page</h1><input aria-labelledby="empty"><span id="empty"></span>', 'control has no accessible name'),
    ('<h1>Page</h1><input aria-labelledby="a missing"><span id="a">Name</span>', 'control has no accessible name'),
    ('<h1>Page</h1><button><svg aria-hidden="true"><title>Save</title></svg></button>', 'link/button has no accessible name'),
    ('<h1>Page</h1><a href="/"></a>', 'link/button has no accessible name'),
    ('<h1>Page</h1><img>', 'image has no alt'),
    ('<h1>Page</h1><table><tr><td>Value</td></tr></table>', 'table has no th'),
    ('<h1>Page</h1><table><thead><tr><th>Date</th></tr></thead></table>', 'thead header needs scope=col'),
    ('<h1>Page</h1><table><thead><tr><th scope="row">Date</th></tr></thead></table>', 'thead header needs scope=col'),
    ('<h1 id="x">Page</h1><input id="x" aria-label="Name">', 'duplicate id'),
])
def test_invalid_accessibility_snippets(snippet, error):
    assert any(error in message for message in accessibility_errors(snippet))
    with pytest.raises(AssertionError, match=error):
        assert_accessible(snippet)


def _page_urls():
    # Derive the cases from the live navigation so new menu entries get checked.
    request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
    from unittest.mock import patch

    with patch("finance.alert_services.unread_alert_count", return_value=0):
        context = navigation(request)
    return sorted({item["url"] for item in context["nav_items"] + context["settings_tabs"]})


@pytest.fixture
def member_client(client, db):
    call_command("loaddata", "synthetic_demo", verbosity=0)
    client.force_login(get_user_model().objects.get(username="synthetic_alex"))
    return client


@pytest.mark.parametrize("url", _page_urls())
def test_navigation_pages_are_accessible(member_client, url):
    response = member_client.get(url, follow=True)
    assert response.status_code == 200
    html = response.content.decode()
    assert_accessible(html)
    parser = PageParser()
    parser.feed(html)
    focusable = [node for node in parser.elements if node.tag in {"a", "button", "input", "select", "textarea"} and not node.hidden and node.attrs.get("type") != "hidden"]
    assert focusable[0].tag == "a"
    assert focusable[0].attrs["href"] == "#main-content"
    assert focusable[0].text == "Skip to main content"
    main = [node for node in parser.elements if node.tag == "main"]
    assert len(main) == 1
    assert main[0].attrs["id"] == "main-content"
    assert main[0].attrs["tabindex"] == "-1"


def test_signed_out_sign_in_is_accessible(member_client):
    member_client.logout()
    response = member_client.get(reverse("login"))
    assert response.status_code == 200
    assert_accessible(response.content.decode())
