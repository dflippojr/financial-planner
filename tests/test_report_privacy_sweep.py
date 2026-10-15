"""One privacy sweep over every page in the navigation (#481).

A member signs in and opens every nav page plus the report variants that read
other data. Another member's private account, series, planned item, goal and
debt card must not show up anywhere. The owner sees the same canaries on the
same pages, so the sweep cannot pass by rendering nothing.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.category_services import ensure_household_categories
from finance.context_processors import NAV_GROUPS
from finance.lifecycle_services import update_debt_terms
from finance.models import (
    Account,
    BalanceSnapshot,
    Household,
    ImportBatch,
    Membership,
    PlannedItem,
    Person,
    RecurringSeries,
    RecurringSeriesMember,
    SavingsGoal,
    Transaction,
)
from finance.scenario import CHANGE_AMOUNT, encode_change
from finance.year_end import CSV_SECTIONS
from tests.page_payload import json_script_payload


PASSWORD = "Synthetic-passphrase-42!"
CANARY = "SYN-CANARY"
PRIVATE_CHARGE_MINOR = 888_888
SHARED_CHARGE_MINOR = 1_100
UNCATEGORIZED_MINOR = 7_777
CARD_BALANCE_MINOR = 432_109
PLANNED_MINOR = 654_321
GOAL_MINOR = 9_876_543


def _forms(minor):
    value = Decimal(minor) / 100
    return (f"{value:,.2f}", f"{value:.2f}", str(minor))


FORBIDDEN = (
    CANARY,
    *_forms(PRIVATE_CHARGE_MINOR),
    "8,899.88",
    *_forms(UNCATEGORIZED_MINOR),
    *_forms(CARD_BALANCE_MINOR),
    *_forms(PLANNED_MINOR),
    *_forms(GOAL_MINOR),
)


def _person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def _transaction(account, owner, *, amount_minor, when, description, category=None, index):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=when,
        date_range_end=when,
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=when,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=f"{index:064x}",
        original_fields={},
        category=category,
    )


def _signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def _body(response):
    if response.streaming:
        return b"".join(response.streaming_content).decode("utf-8")
    return response.content.decode("utf-8")


@pytest.fixture
def world(db):
    today = timezone.localdate()
    # The last full month is closed, so monthly review and year-end include it.
    when = (today.replace(day=1) - timedelta(days=1)).replace(day=15)
    owner = _person("owner")
    member = _person("member")
    household = Household.objects.create(name="Synthetic Household")
    for person in (owner, member):
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    groceries = household.categories.get(name="Groceries")

    shared = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    private = Account.objects.create(name=f"{CANARY}-PRIVATE", account_type=Account.Type.CHECKING, owner=owner)
    card = Account.objects.create(name=f"{CANARY}-CARD", account_type=Account.Type.CREDIT_CARD, owner=owner)
    _transaction(shared, owner, amount_minor=-SHARED_CHARGE_MINOR, when=when, description="Synthetic shared spend", category=groceries, index=1)
    secret_charge = _transaction(
        private, owner, amount_minor=-PRIVATE_CHARGE_MINOR, when=when, description=f"{CANARY} charge", category=groceries, index=2
    )
    _transaction(private, owner, amount_minor=-UNCATEGORIZED_MINOR, when=when, description=f"{CANARY} loose", index=3)

    series = RecurringSeries.objects.create(
        person=owner,
        merchant_key="syn canary stream",
        display_name=f"{CANARY} stream",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-PRIVATE_CHARGE_MINOR,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="d" * 64,
    )
    RecurringSeriesMember.objects.create(series=series, transaction=secret_charge)
    planned = PlannedItem.objects.create(
        owner=owner,
        name=f"{CANARY} planned",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=PLANNED_MINOR,
        start_date=today + timedelta(days=20),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    SavingsGoal.objects.create(
        owner=owner,
        name=f"{CANARY} goal",
        target_amount_minor=GOAL_MINOR,
        target_date=today + timedelta(days=400),
    )
    BalanceSnapshot.objects.create(
        account=card,
        snapshot_date=today,
        amount_minor=CARD_BALANCE_MINOR,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    update_debt_terms(owner.user, card.pk, apr_percent=Decimal("19.990"), minimum_payment_minor=3_500, payment_day=None)
    return SimpleNamespace(owner=owner, member=member, groceries=groceries, when=when, planned=planned)


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
    return _body(response)


def test_member_never_sees_another_members_private_data_on_any_report(world):
    client = _signed_in(world.member)
    for url, params in _sweep_urls(client, world):
        body = _fetch(client, url, params)
        leaked = [needle for needle in FORBIDDEN if needle in body]
        assert not leaked, f"{url} {params} leaked {leaked}"


def test_owner_sees_the_canaries_on_the_same_pages(world):
    client = _signed_in(world.owner)
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
    member = _signed_in(world.member)
    for scope in ("", "household"):
        assert _trend_payload(member, world, scope)["total_spending_minor"] == SHARED_CHARGE_MINOR
    year_end = member.get(reverse("year-end"), {"year": str(world.when.year)})
    assert year_end.context["report"].cash_flow.summary.spending_minor == SHARED_CHARGE_MINOR
    accounts = [row.name for row in year_end.context["report"].accounts]
    assert not [name for name in accounts if CANARY in name]


def test_owner_trends_count_every_visible_charge(world):
    owner = _signed_in(world.owner)
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
