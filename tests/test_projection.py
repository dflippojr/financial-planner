from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.cash_flow import cash_flow_chart_data, cash_flow_report
from finance.models import (
    Account,
    Household,
    ImportBatch,
    Membership,
    Person,
    PlannedItem,
    RecurringSeries,
    RecurringSeriesMember,
    Transaction,
)
from finance.planning_services import cash_flow_with_projection, projected_months_for
from finance.projection import KIND_EXPENSE, KIND_INCOME, SOURCE_PLANNED, project_cash_flow
from tests.page_payload import json_script_payload


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    from finance.category_services import ensure_household_categories

    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None, share_mode=None):
    if share_mode is None:
        share_mode = Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else ""
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
    )


def make_transaction(owner, account, *, transaction_date=date(2026, 9, 10), amount_minor=-2500, fingerprint=None):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(transaction_date.year, transaction_date.month, 1),
        date_range_end=date(transaction_date.year, transaction_date.month, 28),
        source_file_sha256="a" * 64,
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency="USD",
        description="Synthetic recurring charge",
        source_row_number=1,
        fingerprint=fingerprint or "b" * 64,
        original_fields={"Date": transaction_date.isoformat()},
    )


def planned_row(**overrides):
    row = dict(
        name="Synthetic rent",
        kind=KIND_EXPENSE,
        amount_minor=10000,
        currency="USD",
        start=date(2026, 11, 15),
        end=None,
        cadence="monthly",
        source=SOURCE_PLANNED,
        source_id=1,
    )
    row.update(overrides)
    return SimpleNamespace(**row)


def test_monthly_one_time_weekly_and_end_dates_are_hand_checked():
    today = date(2026, 10, 1)
    monthly = planned_row()
    one_time = planned_row(
        name="Synthetic bonus",
        kind=KIND_INCOME,
        amount_minor=50000,
        start=date(2026, 11, 20),
        cadence="one_time",
        source_id=2,
    )
    weekly = planned_row(
        name="Synthetic groceries",
        amount_minor=1000,
        start=date(2026, 11, 1),
        cadence="weekly",
        source_id=3,
    )
    ending = planned_row(
        name="Synthetic lease",
        amount_minor=20000,
        start=date(2026, 11, 1),
        end=date(2026, 12, 15),
        cadence="monthly",
        source_id=4,
    )
    rows = project_cash_flow((monthly, one_time, weekly, ending), today=today, horizon=3)

    # Nov 2026: rent 100.00 on the 15th; bonus 500.00 on the 20th; weekly 10.00 on
    # 1, 8, 15, 22, 29 (five Sundays); lease 200.00 on the 1st.
    assert rows[0].label == "November 2026"
    assert rows[0].income_minor == 50000
    assert rows[0].spending_minor == 10000 + 5000 + 20000
    assert rows[0].net_minor == 50000 - 35000
    # Dec 2026: rent on the 15th, weekly 10.00 on 6, 13, 20, 27 (four), lease on the 1st.
    assert rows[1].label == "December 2026"
    assert rows[1].income_minor == 0
    assert rows[1].spending_minor == 10000 + 4000 + 20000
    # Jan 2027: rent only; weekly continues (3, 10, 17, 24, 31 = five); lease ended.
    assert rows[2].label == "January 2027"
    assert rows[2].spending_minor == 10000 + 5000
    assert all(item.projected for item in rows)


def test_month_boundary_clamps_january_31_and_skips_ended_days():
    rows = project_cash_flow(
        (planned_row(start=date(2026, 1, 31), amount_minor=100, cadence="monthly"),),
        today=date(2025, 12, 15),
        horizon=3,
    )
    assert [item.label for item in rows] == ["January 2026", "February 2026", "March 2026"]
    assert [item.spending_minor for item in rows] == [100, 100, 100]
    assert [item.contributions[0].occurrence_count for item in rows] == [1, 1, 1]


def test_annual_item_hits_two_novembers_across_24_months():
    rows = project_cash_flow(
        (planned_row(start=date(2026, 11, 1), amount_minor=120000, cadence="annual"),),
        today=date(2026, 10, 1),
        horizon=24,
    )
    spending = [item.spending_minor for item in rows]
    assert len(spending) == 24
    assert rows[0].label == "November 2026"
    assert rows[12].label == "November 2027"
    assert rows[23].label == "October 2028"
    assert spending[0] == 120000
    assert spending[12] == 120000
    assert spending[23] == 0
    assert sum(spending) == 240000


def test_quarterly_and_biweekly_counts_are_hand_checked():
    rows = project_cash_flow(
        (
            planned_row(start=date(2026, 11, 1), amount_minor=30000, cadence="quarterly"),
            planned_row(
                name="Synthetic payday",
                kind=KIND_INCOME,
                amount_minor=10000,
                start=date(2026, 11, 6),
                cadence="biweekly",
                source_id=2,
            ),
        ),
        today=date(2026, 10, 1),
        horizon=6,
    )
    # Quarterly on Nov 1, Feb 1. Biweekly income from Nov 6: Nov 6 and 20 (2);
    # Dec 4 and 18 (2); Jan 1, 15, 29 (3); Feb 12, 26 (2); Mar 12, 26 (2); Apr 9, 23 (2).
    assert [item.spending_minor for item in rows] == [30000, 0, 0, 30000, 0, 0]
    assert [item.income_minor for item in rows] == [20000, 20000, 30000, 20000, 20000, 20000]


@pytest.mark.django_db
def test_actual_cash_flow_totals_do_not_change_when_planned_items_are_added():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, transaction_date=date(2026, 9, 10), amount_minor=-1200, fingerprint="c" * 64)
    before = cash_flow_report(
        owner,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        today=date(2026, 10, 1),
    )
    PlannedItem.objects.create(
        owner=owner,
        name="Synthetic future rent",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=99999,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    after = cash_flow_report(
        owner,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        today=date(2026, 10, 1),
    )
    combined = cash_flow_with_projection(
        owner,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        grouping="month",
        today=date(2026, 10, 1),
        horizon=3,
    )

    assert before.summary.spending_minor == after.summary.spending_minor == 1200
    assert [item.spending_minor for item in before.periods] == [item.spending_minor for item in after.periods]
    assert combined.summary.spending_minor == 1200
    assert combined.projected_periods[0].spending_minor == 99999
    assert combined.projected_periods[0].label == "November 2026"


@pytest.mark.django_db
def test_confirmed_series_project_after_last_occurrence_unless_replaced():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, transaction_date=date(2026, 9, 10), amount_minor=-2500)
    series = RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic streamer",
        display_name="Synthetic streamer",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-2500,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="d" * 64,
    )
    RecurringSeriesMember.objects.create(series=series, transaction=txn)
    months = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert [item.spending_minor for item in months] == [2500, 2500, 2500]
    PlannedItem.objects.create(
        owner=owner,
        name="Synthetic replacement",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=4000,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
        replaces_series=series,
    )
    replaced = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert [item.spending_minor for item in replaced] == [4000, 4000, 4000]


@pytest.mark.django_db
def test_private_items_and_private_series_do_not_leak_to_household_member():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    owner_private = make_account(owner, name="Owner Private")
    member_private = make_account(member, name="Member Private")
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    PlannedItem.objects.create(
        owner=owner,
        name="Secret planned",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=7777,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    PlannedItem.objects.create(
        owner=owner,
        household=household,
        scope=PlannedItem.Scope.HOUSEHOLD,
        name="Shared planned",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=3000,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    owner_series = RecurringSeries.objects.create(
        person=owner,
        merchant_key="owner private bill",
        display_name="Owner private bill",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1111,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="e" * 64,
    )
    RecurringSeriesMember.objects.create(
        series=owner_series,
        transaction=make_transaction(owner, owner_private, amount_minor=-1111, fingerprint="f" * 64),
    )
    member_series = RecurringSeries.objects.create(
        person=member,
        merchant_key="member private bill",
        display_name="Member private bill",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-2222,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="g" * 64,
    )
    RecurringSeriesMember.objects.create(
        series=member_series,
        transaction=make_transaction(member, member_private, amount_minor=-2222, fingerprint="h" * 64),
    )
    RecurringSeriesMember.objects.create(
        series=RecurringSeries.objects.create(
            person=owner,
            merchant_key="shared bill",
            display_name="Shared bill",
            cadence=RecurringSeries.Cadence.MONTHLY,
            typical_amount_minor=-500,
            status=RecurringSeries.Status.SUGGESTED,
            confidence=RecurringSeries.Confidence.HIGH,
            reasons=["synthetic"],
            fingerprint="i" * 64,
        ),
        transaction=make_transaction(
            owner,
            shared,
            transaction_date=date(2026, 9, 5),
            amount_minor=-500,
            fingerprint="j" * 64,
        ),
    )

    owner_months = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    member_months = projected_months_for(member, today=date(2026, 10, 1), horizon=3)
    owner_names = {item.name for month in owner_months for item in month.contributions}
    member_names = {item.name for month in member_months for item in month.contributions}

    assert "Secret planned" in owner_names
    assert "Shared planned" in owner_names
    assert "Owner private bill" in owner_names
    assert "Member private bill" not in owner_names
    assert "Secret planned" not in member_names
    assert "Shared planned" in member_names
    assert "Owner private bill" not in member_names
    assert "Member private bill" in member_names
    assert "Shared bill" not in owner_names
    assert member_months[0].income_minor == 3000
    assert member_months[0].spending_minor == 2222


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 10, 1))
def test_home_chart_payload_matches_projected_table(_localdate):
    owner = make_person("owner")
    make_household(owner)
    PlannedItem.objects.create(
        owner=owner,
        name="Synthetic rent",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=10000,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    client = Client()
    client.force_login(owner.user)
    page = client.get(
        reverse("home"),
        {"date_from": "2026-09-01", "date_to": "2026-10-01", "grouping": "month", "horizon": "6"},
    )
    html = page.content.decode()
    payload = json_script_payload(html, "cash-flow-chart-data")
    report = page.context["report"]

    assert payload == cash_flow_chart_data(report)
    projected = [row for row in payload["periods"] if row["projected"]]
    assert [row["label"] for row in projected] == [item.label for item in report.projected_periods]
    assert [row["spending_minor"] for row in projected] == [item.spending_minor for item in report.projected_periods]
    assert payload["summary"]["spending_minor"] == report.summary.spending_minor
    assert "Projected" in html
    assert "Projection horizon" in html
    assert "Synthetic rent" in html
    assert len(projected) == 6


@pytest.mark.django_db
def test_planned_items_page_add_edit_disable_and_hides_private_items():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    owner_client = Client()
    owner_client.force_login(owner.user)
    member_client = Client()
    member_client.force_login(member.user)

    add = owner_client.post(
        reverse("planned-items"),
        {
            "name": "Synthetic gym",
            "kind": PlannedItem.Kind.EXPENSE,
            "amount": "12.00",
            "start_date": "2026-11-01",
            "cadence": PlannedItem.Cadence.MONTHLY,
            "scope": PlannedItem.Scope.PRIVATE,
        },
    )
    assert add.status_code == 302
    item = PlannedItem.objects.get()
    assert item.amount_minor == 1200
    assert item.owner == owner

    hidden = member_client.get(reverse("planned-items"))
    assert "Synthetic gym" not in hidden.content.decode()
    assert member_client.get(reverse("planned-item-edit", args=[item.pk])).status_code == 404

    edit = owner_client.post(
        reverse("planned-item-edit", args=[item.pk]),
        {
            "name": "Synthetic gym plus",
            "kind": PlannedItem.Kind.EXPENSE,
            "amount": "15.00",
            "start_date": "2026-11-01",
            "cadence": PlannedItem.Cadence.MONTHLY,
            "scope": PlannedItem.Scope.HOUSEHOLD,
        },
    )
    assert edit.status_code == 302
    item.refresh_from_db()
    assert item.name == "Synthetic gym plus"
    assert item.scope == PlannedItem.Scope.HOUSEHOLD
    assert "Synthetic gym plus" in member_client.get(reverse("planned-items")).content.decode()

    disable = owner_client.post(reverse("planned-item-disable", args=[item.pk]))
    assert disable.status_code == 302
    item.refresh_from_db()
    assert item.enabled is False
    months = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert all(month.spending_minor == 0 for month in months)
