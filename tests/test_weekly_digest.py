from datetime import date, datetime, time
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance.models import Account, AlertSettings, RecurringSeries, TransferPair
from finance.weekly_digest import build_weekly_digest, week_window
from tests.test_recurring_review import (
    add_member,
    make_account,
    make_household,
    make_person,
    make_priced_series,
    make_series,
    make_transaction,
)

# A Wednesday: the current week is Mon Oct 12 - Wed Oct 14, the last week Mon Oct 5 - Sun Oct 11.
TODAY = date(2026, 10, 14)


def first_seen(series, day, at=time(12, 0)):
    stamp = timezone.make_aware(datetime.combine(day, at))
    RecurringSeries.objects.filter(pk=series.pk).update(created_at=stamp)


def suggested(person, name):
    series = make_series(person, name=name, cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1299)
    RecurringSeries.objects.filter(pk=series.pk).update(status=RecurringSeries.Status.SUGGESTED)
    return series


def titles(items):
    return [item.title for item in items]


def test_week_window_is_monday_to_sunday_and_current_week_ends_today():
    assert week_window(TODAY, "last") == (date(2026, 10, 5), date(2026, 10, 11))
    assert week_window(TODAY, "current") == (date(2026, 10, 12), TODAY)
    monday = date(2026, 10, 12)
    assert week_window(monday, "current") == (monday, monday)
    assert week_window(monday, "last") == (date(2026, 10, 5), date(2026, 10, 11))
    with pytest.raises(ValueError):
        week_window(TODAY, "next")


@pytest.mark.django_db
def test_new_series_inside_outside_and_across_the_sunday_monday_boundary():
    owner = make_person("owner")
    make_household(owner)
    sunday_late = suggested(owner, "Synthetic Sunday Stream")
    first_seen(sunday_late, date(2026, 10, 11), time(23, 59))
    monday_early = make_series(owner, name="Synthetic Monday Gym", cadence=RecurringSeries.Cadence.WEEKLY)
    first_seen(monday_early, date(2026, 10, 12), time(0, 0))
    before = suggested(owner, "Synthetic Old Box")
    first_seen(before, date(2026, 10, 4), time(23, 59))
    dismissed = suggested(owner, "Synthetic Dismissed")
    RecurringSeries.objects.filter(pk=dismissed.pk).update(status=RecurringSeries.Status.DISMISSED)
    first_seen(dismissed, date(2026, 10, 7))

    last = build_weekly_digest(owner, today=TODAY, week="last")
    current = build_weekly_digest(owner, today=TODAY, week="current")

    assert titles(last.new_series) == ["Synthetic Sunday Stream"]
    assert last.new_series[0].detail == "Monthly · Suggested"
    assert last.new_series[0].amount_display == "12.99 USD"
    assert last.new_series[0].url == f"{reverse('recurring-review')}#series-{sunday_late.pk}"
    assert titles(current.new_series) == ["Synthetic Monday Gym"]
    assert current.new_series[0].detail == "Weekly · Confirmed"


@pytest.mark.django_db
def test_price_change_counts_in_the_week_of_its_latest_charge():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    rise = make_priced_series(owner, account, "Synthetic Rise", (-1000, -1000, -1000, -1500), start=date(2026, 7, 8))
    make_priced_series(owner, account, "Synthetic Old Rise", (-1000, -1000, -1500), start=date(2026, 7, 1))
    make_priced_series(owner, account, "Synthetic Steady", (-1000, -1000, -1000, -1000), start=date(2026, 7, 9))

    last = build_weekly_digest(owner, today=TODAY, week="last")
    current = build_weekly_digest(owner, today=TODAY, week="current")

    assert titles(last.price_changes) == ["Synthetic Rise"]
    change = last.price_changes[0]
    assert change.on == date(2026, 10, 8)
    assert change.amount_display == "15.00 USD"
    assert change.detail == "Usually 10.00 USD · 50.0%"
    assert change.url == f"{reverse('recurring-review')}#series-{rise.pk}"
    assert current.price_changes == []


@pytest.mark.django_db
def test_large_transactions_at_threshold_not_just_under_and_only_in_window():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, large_transaction_minor=10_000)
    make_transaction(owner, account, transaction_date=date(2026, 10, 6), amount_minor=-9_999, description="Synthetic Under")
    at = make_transaction(owner, account, transaction_date=date(2026, 10, 7), amount_minor=-10_000, description="Synthetic At")
    make_transaction(owner, account, transaction_date=date(2026, 10, 9), amount_minor=25_000, description="Synthetic Bonus")
    make_transaction(owner, account, transaction_date=date(2026, 10, 12), amount_minor=-50_000, description="Synthetic Monday")

    last = build_weekly_digest(owner, today=TODAY, week="last")

    assert titles(last.large_transactions) == ["Synthetic Bonus", "Synthetic At"]
    assert last.large_transactions[1].amount_display == "-100.00 USD"
    assert last.large_transactions[1].url == reverse("transaction-edit", args=[at.pk])
    assert last.large_threshold_display == "100.00 USD"
    assert titles(build_weekly_digest(owner, today=TODAY, week="current").large_transactions) == ["Synthetic Monday"]


@pytest.mark.django_db
def test_large_transactions_leave_out_transfers_between_visible_accounts():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    AlertSettings.objects.create(person=owner, large_transaction_minor=10_000)
    out_leg = make_transaction(owner, checking, transaction_date=date(2026, 10, 7), amount_minor=-50_000, description="Synthetic Move Out")
    in_leg = make_transaction(owner, savings, transaction_date=date(2026, 10, 7), amount_minor=50_000, description="Synthetic Move In")
    TransferPair.objects.create(
        leg_a=out_leg,
        leg_b=in_leg,
        status=TransferPair.Status.CONFIRMED,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.HIGH,
        reasons=["synthetic"],
    )
    make_transaction(owner, checking, transaction_date=date(2026, 10, 8), amount_minor=-20_000, description="Synthetic Sofa")

    assert titles(build_weekly_digest(owner, today=TODAY, week="last").large_transactions) == ["Synthetic Sofa"]


@pytest.mark.django_db
def test_no_threshold_lists_no_large_transactions():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, transaction_date=date(2026, 10, 7), amount_minor=-900_000)

    digest = build_weekly_digest(owner, today=TODAY, week="last")

    assert digest.large_transactions == []
    assert digest.large_threshold_display is None


@pytest.mark.django_db
def test_another_members_private_account_is_excluded():
    owner = make_person("owner")
    partner = make_person("partner")
    household = make_household(owner, partner)
    shared = make_account(partner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    hidden = make_account(partner, name="Synthetic Partner Private")
    AlertSettings.objects.create(person=owner, large_transaction_minor=5_000)
    make_transaction(partner, shared, transaction_date=date(2026, 10, 7), amount_minor=-6_000, description="Synthetic Shared Big")
    make_transaction(partner, hidden, transaction_date=date(2026, 10, 7), amount_minor=-7_000, description="Synthetic Hidden Big")
    # A series built on the partner's private charges belongs to the partner and never reaches the owner.
    secret = make_priced_series(partner, hidden, "Synthetic Hidden Rise", (-1000, -1000, -1000, -1500), start=date(2026, 7, 8))
    first_seen(secret, date(2026, 10, 7))

    digest = build_weekly_digest(owner, today=TODAY, week="last")

    assert titles(digest.large_transactions) == ["Synthetic Shared Big"]
    assert digest.new_series == []
    assert digest.price_changes == []
    assert titles(build_weekly_digest(partner, today=TODAY, week="last").price_changes) == ["Synthetic Hidden Rise"]


@pytest.mark.django_db
def test_own_series_with_a_charge_on_an_account_no_longer_visible_is_excluded():
    owner = make_person("owner")
    partner = make_person("partner")
    make_household(owner, partner)
    mine = make_account(owner)
    partner_private = make_account(partner, name="Synthetic Partner Private")
    series = suggested(owner, "Synthetic Mixed")
    add_member(series, make_transaction(owner, mine, transaction_date=date(2026, 10, 6), description="Synthetic Mixed"))
    add_member(
        series,
        make_transaction(partner, partner_private, transaction_date=date(2026, 10, 7), description="Synthetic Mixed"),
    )
    first_seen(series, date(2026, 10, 7))

    assert build_weekly_digest(owner, today=TODAY, week="last").new_series == []


def home_client(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_card_shows_groups_links_and_the_current_week_by_default(_localdate):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, large_transaction_minor=10_000)
    big = make_transaction(owner, account, transaction_date=date(2026, 10, 13), amount_minor=-20_000, description="Synthetic Laptop")
    series = suggested(owner, "Synthetic Weekday Stream")
    first_seen(series, date(2026, 10, 13))

    response = home_client(owner).get(reverse("home"))
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["digest"].digest.week == "current"
    assert 'id="weekly-digest-heading" class="card-title">This week</h2>' in content
    assert "Oct 12 – Oct 14, 2026 · 2 items" in content
    assert "Synthetic Laptop" in content
    assert reverse("transaction-edit", args=[big.pk]) in content
    assert f"{reverse('recurring-review')}#series-{series.pk}" in content
    assert "Nothing new this" not in content
    assert 'href="?digest_week=last#weekly-digest"' in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_card_empty_state_and_threshold_hint(_localdate):
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)

    content = home_client(owner).get(reverse("home")).content.decode()

    assert "Nothing new this week so far" in content
    assert reverse("settings-alerts") in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_switch_selects_last_week_and_keeps_other_filters(_localdate):
    owner = make_person("owner")
    make_household(owner)
    series = suggested(owner, "Synthetic Last Week Stream")
    first_seen(series, date(2026, 10, 8))
    client = home_client(owner)

    current = client.get(reverse("home"))
    last = client.get(reverse("home"), {"digest_week": "last", "grouping": "week"})
    content = last.content.decode()

    assert current.context["digest"].digest.new_series == []
    assert last.context["digest"].digest.week == "last"
    assert titles(last.context["digest"].digest.new_series) == ["Synthetic Last Week Stream"]
    assert 'id="weekly-digest-heading" class="card-title">Last week</h2>' in content
    assert 'href="?grouping=week#weekly-digest"' in content
    assert last.context["filter_form"]["grouping"].value() == "week"
    # An unknown week falls back to this week instead of failing.
    assert client.get(reverse("home"), {"digest_week": "next"}).context["digest"].digest.week == "current"


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_shows_five_items_per_group_with_see_all(_localdate):
    owner = make_person("owner")
    make_household(owner)
    for index in range(7):
        first_seen(suggested(owner, f"Synthetic Stream {index}"), date(2026, 10, 13))

    content = home_client(owner).get(reverse("home")).content.decode()

    assert content.count("Synthetic Stream") == 5
    assert f"{reverse('recurring-review')}#recurring-suggestions" in content
    assert "See all<span" in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_expands_extra_large_transactions_in_place(_localdate):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, large_transaction_minor=10_000)
    for index in range(7):
        make_transaction(
            owner, account, transaction_date=date(2026, 10, 13), amount_minor=-20_000 - index, description=f"Synthetic Big {index}"
        )

    content = home_client(owner).get(reverse("home")).content.decode()
    shown, extra = content.split("See all 7", 1)

    assert shown.count("Synthetic Big") == 5
    assert extra.count("Synthetic Big") == 2
    card = content.split('id="weekly-digest"', 1)[1].split("</article>", 1)[0]
    assert f'href="{reverse("transaction-list")}?' not in card


def _home_query_count(client):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("home"))
    assert response.status_code == 200
    return len(queries)


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
def test_home_query_count_does_not_grow_with_series(_localdate):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, large_transaction_minor=1_000)
    client = home_client(owner)

    def add_series(index):
        series = make_priced_series(
            owner, account, f"Synthetic Drift {index}", (-1000, -1000, -1000, -2000), start=date(2026, 7, 12 + index % 3)
        )
        first_seen(series, date(2026, 10, 13))

    add_series(0)
    _home_query_count(client)
    baseline = _home_query_count(client)
    for index in range(1, 5):
        add_series(index)
    _home_query_count(client)
    response = client.get(reverse("home"))
    digest = response.context["digest"].digest
    assert (len(digest.new_series), len(digest.price_changes), len(digest.large_transactions)) == (5, 5, 5)
    assert _home_query_count(client) == baseline
