"""One privacy sweep over every page in the navigation (#481).

A member signs in and opens every nav page plus the report variants that read
other data. Another member's private account, series, planned item, goal and
debt card must not show up anywhere. The owner sees the same canaries on the
same pages, so the sweep cannot pass by rendering nothing.
"""
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse

from finance.context_processors import NAV_GROUPS
from finance.scenario import CHANGE_AMOUNT, encode_change
from finance.year_end import CSV_SECTIONS
from tests.page_payload import json_script_payload
from tests.privacy_world import (
    CANARY,
    FORBIDDEN,
    PRIVATE_CHARGE_MINOR,
    SHARED_CHARGE_MINOR,
    UNCATEGORIZED_MINOR,
    body_text,
    signed_in,
    build_report_world,
)


@pytest.fixture
def world(db):
    return build_report_world()


def _variant_urls(world):
    month_start = world.when.replace(day=1)
    window = {"date_from": month_start.isoformat(), "date_to": world.when.replace(day=28).isoformat()}
    year = {"year": str(world.when.year)}
    token = encode_change(SimpleNamespace(type=CHANGE_AMOUNT, source_id=world.planned.pk, amount_minor=1))
    urls = [
        (reverse("spending-by-category"), {**window, "scope": "household", "tab": "trends"}),
        (reverse("spending-category-detail", args=[world.groceries.pk]), window),
        (reverse("spending-category-uncategorized"), window),
        (reverse("year-end"), year),
        (reverse("monthly-review"), {"month": month_start.strftime("%Y-%m")}),
        (reverse("home"), {"horizon": "3", "sc": token}),
    ]
    urls += [(reverse("year-end-csv", args=[section]), year) for section in CSV_SECTIONS]
    urls += [(reverse("savings-goal-plan"), {"scope": scope}) for scope in ("", "private", "household")]
    return urls


def _sweep_urls(client, world):
    home = client.get(reverse("home"))
    assert home.status_code == 200
    nav = [item["url"] for item in home.context["nav_items"]]
    assert len(nav) >= 15
    urls = [(url, {}) for url in nav] + [(url, {"scope": "household"}) for url in nav]
    return urls + _variant_urls(world)


def _fetch(client, url, params):
    response = client.get(url, params)
    assert response.status_code == 200, f"{url} {params} answered {response.status_code}"
    return body_text(response)


def test_member_never_sees_another_members_private_data_on_any_report(world):
    client = signed_in(world.member)
    for url, params in _sweep_urls(client, world):
        body = _fetch(client, url, params)
        leaked = [needle for needle in FORBIDDEN if needle in body]
        assert not leaked, f"{url} {params} leaked {leaked}"


def test_owner_sees_the_canaries_on_the_same_pages(world):
    client = signed_in(world.owner)
    seen = {}
    for url, params in _sweep_urls(client, world):
        body = _fetch(client, url, params)
        seen.setdefault(url, False)
        seen[url] = seen[url] or CANARY in body
    # Without these the member sweep could pass on pages that render nothing.
    for name in (
        "transaction-list",
        "spending-by-category",
        "net-worth",
        "account-list",
        "recurring-review",
        "bills-calendar",
        "savings-goals",
        "savings-goal-plan",
        "planned-items",
        "debt-payoff",
        "year-end",
    ):
        assert seen[reverse(name)], f"the owner does not see the canary on {name}"
    assert seen[reverse("year-end-csv", args=["accounts"])]
    assert seen[reverse("spending-category-uncategorized")]


def _trend_payload(client, world, scope):
    params = {
        "date_from": world.when.replace(day=1).isoformat(),
        "date_to": world.when.replace(day=28).isoformat(),
        "scope": scope,
        "tab": "trends",
    }
    html = _fetch(client, reverse("spending-by-category"), params)
    return json_script_payload(html, "spending-trend-chart-data")


def test_member_aggregates_hold_shared_spending_only(world):
    member = signed_in(world.member)
    for scope in ("", "household"):
        assert _trend_payload(member, world, scope)["total_spending_minor"] == SHARED_CHARGE_MINOR
    year_end = member.get(reverse("year-end"), {"year": str(world.when.year)})
    assert year_end.context["report"].cash_flow.summary.spending_minor == SHARED_CHARGE_MINOR
    accounts = [row.name for row in year_end.context["report"].accounts]
    assert not [name for name in accounts if CANARY in name]


def test_owner_trends_count_every_visible_charge(world):
    owner = signed_in(world.owner)
    everything = _trend_payload(owner, world, "")
    assert everything["total_spending_minor"] == SHARED_CHARGE_MINOR + PRIVATE_CHARGE_MINOR + UNCATEGORIZED_MINOR
    assert [(item["name"], item["values"][0]) for item in everything["chart_series"]] == [
        ("Groceries", SHARED_CHARGE_MINOR + PRIVATE_CHARGE_MINOR),
        ("Uncategorized", UNCATEGORIZED_MINOR),
    ]
    assert _trend_payload(owner, world, "household")["total_spending_minor"] == SHARED_CHARGE_MINOR


def _anonymous_urls():
    nav = [reverse(key) for _group, _label, entries in NAV_GROUPS for key, _item_label, _icon in entries]
    extra = [
        reverse("spending-category-uncategorized"),
        reverse("savings-goal-plan"),
        reverse("savings-goal-import"),
        reverse("sheet-comparison"),
        *[reverse("year-end-csv", args=[section]) for section in CSV_SECTIONS],
    ]
    return sorted(set(nav + extra))


@pytest.mark.django_db
@pytest.mark.parametrize("url", _anonymous_urls())
def test_anonymous_visitor_is_sent_to_sign_in(url):
    response = Client().get(url)
    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))
