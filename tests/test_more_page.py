import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from tests.a11y import assert_accessible


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
