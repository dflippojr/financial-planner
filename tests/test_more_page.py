import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from tests.a11y import PageParser, assert_accessible


@pytest.fixture
def member_client(client, db):
    call_command("loaddata", "synthetic_demo", verbosity=0)
    client.force_login(get_user_model().objects.get(username="synthetic_alex"))
    return client


def test_more_page_lists_the_pages_the_tabs_do_not_cover(member_client):
    response = member_client.get(reverse("more"))
    html = response.content.decode()
    assert response.status_code == 200
    assert_accessible(html)
    for name in ("net-worth", "alert-list", "debt-payoff", "year-end", "account-settings"):
        assert reverse(name) in html


def test_bottom_tabs_mark_the_current_page(member_client):
    tabs = {tab["key"]: tab["active"] for tab in member_client.get(reverse("budgets")).context["dock_tabs"]}
    assert tabs == {"home": False, "transaction-list": False, "budgets": True, "chat": False, "more": False}
    tabs = {tab["key"]: tab["active"] for tab in member_client.get(reverse("net-worth")).context["dock_tabs"]}
    assert tabs["more"] and not tabs["home"]


def test_signed_out_pages_have_no_tabs(client, db):
    assert "Main tabs" not in client.get(reverse("login")).content.decode()


def _groups(html, attribute="data-nav-group"):
    """Each sidebar (or More page) group's link targets, in page order."""
    parser = PageParser()
    parser.feed(html)
    groups = {}
    for element in parser.elements:
        if element.tag == "a" and element.attrs.get("href"):
            owner = next((node for node in reversed(element.ancestors) if attribute in node.attrs), None)
            if owner is not None:
                groups.setdefault(owner.attrs[attribute], []).append(element.attrs["href"])
    return groups


def test_sidebar_and_more_page_list_the_same_destinations_in_the_same_groups(member_client):
    sidebar = _groups(member_client.get(reverse("net-worth")).content.decode())
    more = _groups(member_client.get(reverse("more")).content.decode(), "data-more-group")
    assert list(sidebar) == ["main", "money", "planning"]
    assert {key: sidebar[key] for key in ("money", "planning")} == more
    assert sidebar["main"] == [reverse(name) for name in ("home", "transaction-list", "budgets", "chat")]
    assert reverse("budgets") not in more["money"] + more["planning"]


def test_sidebar_uses_dock_names_and_marks_the_current_page(member_client):
    response = member_client.get(reverse("transaction-list"))
    groups = response.context["nav_groups"]
    assert [item["label"] for item in groups[0]["items"]] == ["Home", "Activity", "Budgets", "Chat"]
    assert [tab["label"] for tab in response.context["dock_tabs"]] == ["Home", "Activity", "Budgets", "Chat", "More"]
    assert all(item["icon"] for group in groups for item in group["items"])
    html = response.content.decode()
    assert f'href="{reverse("transaction-list")}" class="nav-row" aria-current="page"' in html
    # More is phone-only: the dock is the one link to it.
    assert html.count(f'href="{reverse("more")}"') == 1


def test_no_page_renders_the_floating_chat_button(member_client):
    html = member_client.get(reverse("home")).content.decode()
    assert 'label for="finance-chat-drawer"' not in html
    assert 'data-chat-drawer-open aria-controls="finance-chat-panel"' in html
    assert 'name="page_route"' in html or "Settings → AI" in html


def test_more_page_points_to_the_sidebar_on_wide_screens(member_client):
    html = member_client.get(reverse("more")).content.decode()
    assert "Every page is in the sidebar" in html
    assert '<div class="lg:hidden">' in html
