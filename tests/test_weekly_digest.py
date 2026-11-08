from datetime import date, datetime, time
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance.models import Account, AlertSettings, RecurringSeries, Transaction, TransferPair
from finance.recurring_services import create_manual_series
from finance.weekly_digest import build_weekly_digest, recent_span_days, week_window
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
# A Saturday: the current week is Mon Nov 2 - Sat Nov 7.
NOV_TODAY = date(2026, 11, 7)


_real_localdate = timezone.localdate


def fake_localdate(value=None, timezone=None):
    """Pin only "today"; converting a stored timestamp keeps working."""
    if value is None:
        return TODAY
    return _real_localdate(value, timezone)


def first_seen(series, day, at=time(12, 0)):
    """When the detector created the series; a history import makes this recent for old charges."""
    stamp = timezone.make_aware(datetime.combine(day, at))
    RecurringSeries.objects.filter(pk=series.pk).update(created_at=stamp)


def with_charge(series, on=date(2026, 1, 2), *, account=None, amount_minor=-1299):
    account = account or make_account(series.person, name=f"Synthetic Card {series.pk}")
    add_member(
        series,
        make_transaction(
            series.person, account, transaction_date=on, amount_minor=amount_minor, description=series.display_name
        ),
    )
    return series


def suggested(person, name, on=date(2026, 1, 2), *, created=None, **charge):
    series = make_series(person, name=name, cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1299)
    RecurringSeries.objects.filter(pk=series.pk).update(status=RecurringSeries.Status.SUGGESTED)
    with_charge(series, on, **charge)
    if created is not None:
        first_seen(series, created)
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
    # The window follows when each series was spotted; every first charge is recent for its cadence.
    sunday_late = suggested(owner, "Synthetic Sunday Stream", date(2026, 10, 1))
    first_seen(sunday_late, date(2026, 10, 11), time(23, 59))
    monday_early = with_charge(
        make_series(owner, name="Synthetic Monday Gym", cadence=RecurringSeries.Cadence.WEEKLY), date(2026, 9, 28)
    )
    first_seen(monday_early, date(2026, 10, 12), time(0, 0))
    before = suggested(owner, "Synthetic Old Box", date(2026, 10, 1))
    first_seen(before, date(2026, 10, 4), time(23, 59))
    dismissed = suggested(owner, "Synthetic Dismissed", date(2026, 10, 1))
    RecurringSeries.objects.filter(pk=dismissed.pk).update(status=RecurringSeries.Status.DISMISSED)
    first_seen(dismissed, date(2026, 10, 7))

    last = build_weekly_digest(owner, today=TODAY, week="last")
    current = build_weekly_digest(owner, today=TODAY, week="current")

    assert titles(last.new_series) == ["Synthetic Sunday Stream"]
    assert last.new_series[0].detail == "Monthly · Suggested"
    assert last.new_series[0].amount_display == "12.99 USD"
    assert last.new_series[0].url == f"{reverse('recurring-review')}#series-{sunday_late.pk}"
    assert last.new_series[0].on == date(2026, 10, 1)
    assert titles(current.new_series) == ["Synthetic Monday Gym"]
    assert current.new_series[0].detail == "Weekly · Confirmed"
    assert current.new_series[0].on == date(2026, 9, 28)


@pytest.mark.django_db
def test_history_import_is_not_new_but_a_new_monthly_subscription_is():
    # #698: linking accounts imports years of history, and the detector spots those old series this week.
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    imported = suggested(owner, "Synthetic Imported Store", date(2024, 11, 6), account=account)
    for day in (date(2025, 6, 6), date(2026, 10, 6)):
        with_charge(imported, day, account=account)
    first_seen(imported, date(2026, 11, 4))
    genuine = suggested(owner, "Synthetic New Subscription", date(2026, 9, 1), account=account)
    for day in (date(2026, 10, 1), date(2026, 11, 1)):
        with_charge(genuine, day, account=account)
    first_seen(genuine, date(2026, 11, 5))

    current = build_weekly_digest(owner, today=NOV_TODAY, week="current")

    assert titles(current.new_series) == ["Synthetic New Subscription"]
    assert current.new_series[0].on == date(2026, 9, 1)
    assert build_weekly_digest(owner, today=NOV_TODAY, week="last").new_series == []


def test_recent_span_comes_from_the_detector_cadences():
    # MIN_DETECTED_CHARGES * (period + tolerance) from CADENCE_DAYS.
    assert recent_span_days(RecurringSeries.Cadence.WEEKLY) == 30
    assert recent_span_days(RecurringSeries.Cadence.BIWEEKLY) == 51
    assert recent_span_days(RecurringSeries.Cadence.MONTHLY) == 99
    assert recent_span_days(RecurringSeries.Cadence.QUARTERLY) == 285
    assert recent_span_days(RecurringSeries.Cadence.ANNUAL) == 1110
    assert recent_span_days("irregular") == 99


@pytest.mark.django_db
def test_first_charge_at_the_edge_of_the_recent_span_and_per_cadence():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    # Spotted Thu Nov 5; the monthly span of 99 days reaches back to Jul 29.
    suggested(owner, "Synthetic On The Edge", date(2026, 7, 29), created=date(2026, 11, 5), account=account)
    suggested(owner, "Synthetic Past The Edge", date(2026, 7, 28), created=date(2026, 11, 5), account=account)
    # A weekly series reaches back only 30 days, to Oct 6.
    weekly = with_charge(
        make_series(owner, name="Synthetic Old Weekly", cadence=RecurringSeries.Cadence.WEEKLY),
        date(2026, 10, 5),
        account=account,
    )
    first_seen(weekly, date(2026, 11, 5))
    annual = with_charge(
        make_series(owner, name="Synthetic Annual Renewal", cadence=RecurringSeries.Cadence.ANNUAL),
        date(2024, 11, 20),
        account=account,
    )
    first_seen(annual, date(2026, 11, 5))

    current = build_weekly_digest(owner, today=NOV_TODAY, week="current")

    assert titles(current.new_series) == ["Synthetic On The Edge", "Synthetic Annual Renewal"]


@pytest.mark.django_db
def test_hand_made_series_after_its_second_charge_is_new():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    rows = [
        make_transaction(owner, account, transaction_date=day, amount_minor=-2500, description="Synthetic Tutor")
        for day in (date(2026, 10, 3), date(2026, 11, 3))
    ]
    series = create_manual_series(owner, "Synthetic Tutor", RecurringSeries.Cadence.MONTHLY, [row.pk for row in rows])
    first_seen(series, date(2026, 11, 4))

    current = build_weekly_digest(owner, today=NOV_TODAY, week="current")

    assert titles(current.new_series) == ["Synthetic Tutor"]
    assert current.new_series[0].detail == "Monthly · Confirmed"
    assert current.new_series[0].on == date(2026, 10, 3)


@pytest.mark.django_db
def test_first_charge_ignores_archived_members_and_rows_sort_by_first_charge_newest_first():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    restarted = suggested(owner, "Synthetic Restarted", date(2026, 6, 8), account=account)
    with_charge(restarted, date(2026, 10, 6), account=account)
    # The June charge was undone; its membership stays until the next refresh. Counted, it would be too old.
    Transaction.objects.filter(recurring_memberships__series=restarted, transaction_date=date(2026, 6, 8)).update(
        status=Transaction.Status.ARCHIVED, archived_at=timezone.now()
    )
    first_seen(restarted, date(2026, 10, 6))
    # Spotted later than Restarted, but their first charges are older; the name breaks the tie.
    suggested(owner, "Synthetic B Later", date(2026, 9, 9), created=date(2026, 10, 8), account=account)
    suggested(owner, "Synthetic A Same Day", date(2026, 9, 9), created=date(2026, 10, 7), account=account)

    last = build_weekly_digest(owner, today=TODAY, week="last")

    assert titles(last.new_series) == ["Synthetic Restarted", "Synthetic A Same Day", "Synthetic B Later"]
    assert [item.on for item in last.new_series] == [date(2026, 10, 6), date(2026, 9, 9), date(2026, 9, 9)]


@pytest.mark.django_db
def test_inactive_series_is_left_out():
    owner = make_person("owner")
    make_household(owner)
    series = with_charge(
        make_series(owner, name="Synthetic Unshared", cadence=RecurringSeries.Cadence.MONTHLY), date(2026, 10, 7)
    )
    RecurringSeries.objects.filter(pk=series.pk).update(is_active=False)
    first_seen(series, date(2026, 10, 7))

    assert build_weekly_digest(owner, today=TODAY, week="last").new_series == []


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
def test_archived_charges_do_not_count_until_refresh_catches_up():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_priced_series(owner, account, "Synthetic Undone", (-1000, -1000, -1000, -1500), start=date(2026, 7, 8))
    latest = series.members.order_by("-transaction__transaction_date").first().transaction
    fresh = suggested(owner, "Synthetic Fresh", date(2026, 10, 8), created=date(2026, 10, 8), account=account)
    # Undoing the import archives the charge; the membership stays until the next refresh.
    Transaction.objects.filter(pk=latest.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())

    last = build_weekly_digest(owner, today=TODAY, week="last")
    assert last.price_changes == []
    assert titles(last.new_series) == ["Synthetic Fresh"]
    Transaction.objects.filter(recurring_memberships__series=fresh).update(
        status=Transaction.Status.ARCHIVED, archived_at=timezone.now()
    )
    assert build_weekly_digest(owner, today=TODAY, week="last").new_series == []


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
    make_priced_series(partner, hidden, "Synthetic Hidden Rise", (-1000, -1000, -1000, -1500), start=date(2026, 7, 8))
    suggested(partner, "Synthetic Hidden New", date(2026, 10, 7), created=date(2026, 10, 7), account=hidden)

    digest = build_weekly_digest(owner, today=TODAY, week="last")
    partner_digest = build_weekly_digest(partner, today=TODAY, week="last")

    assert titles(digest.large_transactions) == ["Synthetic Shared Big"]
    assert digest.new_series == []
    assert digest.price_changes == []
    assert titles(partner_digest.price_changes) == ["Synthetic Hidden Rise"]
    assert titles(partner_digest.new_series) == ["Synthetic Hidden New"]


@pytest.mark.django_db
def test_own_series_with_a_charge_on_an_account_no_longer_visible_is_excluded():
    owner = make_person("owner")
    partner = make_person("partner")
    make_household(owner, partner)
    mine = make_account(owner)
    partner_private = make_account(partner, name="Synthetic Partner Private")
    series = suggested(owner, "Synthetic Mixed", date(2026, 10, 6), created=date(2026, 10, 7), account=mine)
    assert titles(build_weekly_digest(owner, today=TODAY, week="last").new_series) == ["Synthetic Mixed"]
    add_member(
        series,
        make_transaction(partner, partner_private, transaction_date=date(2026, 10, 7), description="Synthetic Mixed"),
    )

    assert build_weekly_digest(owner, today=TODAY, week="last").new_series == []


def home_client(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", side_effect=fake_localdate)
def test_home_card_shows_groups_links_and_the_current_week_by_default(_localdate):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, large_transaction_minor=10_000)
    big = make_transaction(owner, account, transaction_date=date(2026, 10, 13), amount_minor=-20_000, description="Synthetic Laptop")
    series = suggested(owner, "Synthetic Weekday Stream", date(2026, 10, 13))
    first_seen(series, date(2026, 10, 14))

    response = home_client(owner).get(reverse("home"))
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["digest"].digest.week == "current"
    assert 'id="weekly-digest-heading" class="card-title">This week</h2>' in content
    assert "Oct 12 – Oct 14, 2026 · 2 items" in content
    assert "Synthetic Laptop" in content
    assert "Tue Oct 13 · Monthly · Suggested" in content
    assert reverse("transaction-edit", args=[big.pk]) in content
    assert f"{reverse('recurring-review')}#series-{series.pk}" in content
    assert "Nothing new this" not in content
    assert 'href="?digest_week=last#weekly-digest"' in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", side_effect=fake_localdate)
def test_home_card_empty_state_and_threshold_hint(_localdate):
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)

    content = home_client(owner).get(reverse("home")).content.decode()

    assert "Nothing new this week so far" in content
    assert reverse("settings-alerts") in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", side_effect=fake_localdate)
def test_home_switch_selects_last_week_and_keeps_other_filters(_localdate):
    owner = make_person("owner")
    make_household(owner)
    series = suggested(owner, "Synthetic Last Week Stream", date(2026, 9, 8))
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
@patch("finance.views.timezone.localdate", side_effect=fake_localdate)
def test_home_shows_five_items_per_group_with_see_all(_localdate):
    owner = make_person("owner")
    make_household(owner)
    for index in range(7):
        suggested(owner, f"Synthetic Stream {index}", date(2026, 10, 13), created=date(2026, 10, 13))

    content = home_client(owner).get(reverse("home")).content.decode()

    assert content.count("Synthetic Stream") == 5
    assert f"{reverse('recurring-review')}#recurring-suggestions" in content
    assert "See all<span" in content


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", side_effect=fake_localdate)
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
@patch("finance.views.timezone.localdate", side_effect=fake_localdate)
def test_home_query_count_does_not_grow_with_series(_localdate):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, large_transaction_minor=1_000)
    client = home_client(owner)

    def add_series(index):
        make_priced_series(
            owner, account, f"Synthetic Drift {index}", (-1000, -1000, -1000, -2000), start=date(2026, 7, 12 + index % 3)
        )
        # Under the large-transaction threshold, so only the new-series group grows.
        suggested(
            owner,
            f"Synthetic Start {index}",
            date(2026, 10, 12 + index % 3),
            created=date(2026, 10, 13),
            account=account,
            amount_minor=-999,
        )

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
