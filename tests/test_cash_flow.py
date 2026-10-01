from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.cash_flow import (
    GROUPING_MONTH,
    GROUPING_QUARTER,
    GROUPING_WEEK,
    GROUPING_YEAR,
    cash_flow_chart_data,
    cash_flow_report,
    default_date_range,
    format_minor,
    iter_period_windows,
    period_count,
    period_label,
    previous_equal_range,
)
from tests.page_payload import json_script_payload
from finance.category_services import (
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
)
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


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


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 15),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
    kind=Transaction.Kind.CASH_FLOW,
    range_start=None,
    range_end=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=range_start or date(2026, 1, 1),
        date_range_end=range_end or date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=kind,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


@pytest.mark.django_db
def test_default_range_is_twelve_full_months_plus_current_month_to_date():
    start, end = default_date_range(date(2026, 9, 15))

    assert start == date(2025, 9, 1)
    assert end == date(2026, 9, 15)


@pytest.mark.django_db
def test_weeks_start_on_monday():
    windows = list(iter_period_windows(date(2026, 1, 1), date(2026, 1, 7), GROUPING_WEEK))

    assert windows[0].calendar_start == date(2025, 12, 29)
    assert windows[0].calendar_start.weekday() == 0
    assert windows[0].start == date(2026, 1, 1)
    assert windows[0].end == date(2026, 1, 4)


@pytest.mark.django_db
def test_period_totals_match_income_and_spending_helper():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    make_transaction(owner, checking, amount_minor=5000, description="Synthetic paycheck")
    make_transaction(
        owner,
        checking,
        amount_minor=-2300,
        description="Synthetic groceries",
        fingerprint="c" * 64,
    )
    expected = income_and_spending_totals(owner, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    report = cash_flow_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        grouping=GROUPING_MONTH,
        today=date(2026, 2, 1),
    )

    assert len(report.periods) == 1
    assert report.periods[0].income_minor == expected.income_minor == 5000
    assert report.periods[0].spending_minor == expected.spending_minor == 2300
    assert report.periods[0].net_minor == expected.net_minor == 2700
    assert report.periods[0].income_display == format_minor(5000)
    assert report.periods[0].spending_display == "23.00 USD"
    assert report.periods[0].net_display == "27.00 USD"


@pytest.mark.django_db
def test_excluded_transfer_and_linked_refund_follow_helper_not_copied_rules():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic transfer out")
    make_transaction(
        owner,
        savings,
        amount_minor=4000,
        description="Synthetic transfer in",
        fingerprint="d" * 64,
    )
    purchase = make_transaction(
        owner,
        checking,
        amount_minor=-2000,
        description="Synthetic purchase",
        fingerprint="e" * 64,
    )
    refund = make_transaction(
        owner,
        checking,
        amount_minor=500,
        description="Synthetic refund",
        fingerprint="f" * 64,
    )
    refresh_transfer_pairs(owner)
    link_refund(owner, refund.pk, purchase.pk)
    expected = income_and_spending_totals(owner, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    report = cash_flow_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        grouping=GROUPING_MONTH,
        today=date(2026, 2, 1),
    )

    assert expected.income_minor == 0
    assert expected.spending_minor == 1500
    assert report.periods[0].income_minor == expected.income_minor
    assert report.periods[0].spending_minor == expected.spending_minor
    assert report.periods[0].net_minor == expected.net_minor


@pytest.mark.django_db
def test_investment_activity_omitted_and_account_filter_hides_other_ledgers():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    brokerage = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    make_transaction(owner, checking, amount_minor=3000, description="Synthetic pay")
    make_transaction(
        owner,
        brokerage,
        amount_minor=9000,
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
        description="Synthetic investment activity",
        fingerprint="g" * 64,
    )
    all_totals = income_and_spending_totals(owner)
    checking_totals = income_and_spending_totals(owner, accounts=[checking])
    report = cash_flow_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        grouping=GROUPING_MONTH,
        account=brokerage,
        today=date(2026, 2, 1),
    )

    assert all_totals.income_minor == checking_totals.income_minor == 3000
    assert report.periods[0].income_minor == 0
    assert report.includes_investment is True
    assert "verified" in report.investment_notice


@pytest.mark.django_db
def test_private_account_never_appears_in_another_members_periods_or_flags():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private = make_account(owner, name="SECRET PRIVATE LEDGER")
    make_transaction(
        owner,
        shared,
        amount_minor=-1000,
        range_start=date(2026, 1, 1),
        range_end=date(2026, 1, 31),
    )
    make_transaction(
        owner,
        private,
        amount_minor=-99999,
        description="Synthetic hidden",
        fingerprint="h" * 64,
        range_start=date(2026, 2, 1),
        range_end=date(2026, 2, 28),
        transaction_date=date(2026, 2, 10),
    )
    member_report = cash_flow_report(
        member,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 2, 28),
        grouping=GROUPING_MONTH,
        today=date(2026, 3, 1),
    )
    owner_private = cash_flow_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 2, 28),
        grouping=GROUPING_MONTH,
        account=private,
        today=date(2026, 3, 1),
    )

    assert [item.name for item in member_report.accounts] == ["Synthetic Shared"]
    january, february = member_report.periods
    assert january.spending_minor == 1000
    assert january.missing_import is False
    assert february.spending_minor == 0
    assert february.missing_import is True
    assert february.spending_display == "0.00 USD"
    assert owner_private.periods[1].spending_minor == 99999
    expected = income_and_spending_totals(member, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    assert january.spending_minor == expected.spending_minor

    client = Client()
    client.force_login(member.user)
    page = client.get(
        reverse("home"),
        {"date_from": "2026-01-01", "date_to": "2026-02-28", "grouping": "month"},
    )
    html = page.content.decode()
    payload = json_script_payload(html, "cash-flow-chart-data")
    report_payload = cash_flow_chart_data(member_report)

    assert payload == report_payload
    assert [row["spending_minor"] for row in payload["periods"]] == [1000, 0]
    assert payload["summary"]["spending_minor"] == 1000
    assert "SECRET PRIVATE LEDGER" not in html
    assert "99999" not in html
    assert "999.99 USD" not in html
    assert "cdn." not in html.lower()
    assert "/static/vendor/chart.umd.min.js" in html


@pytest.mark.django_db
def test_archived_import_batch_does_not_cover_a_period():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, amount_minor=-100)
    ImportBatch.objects.filter(account=account).update(status=ImportBatch.Status.ARCHIVED, archived_at=timezone.now())
    report = cash_flow_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        grouping=GROUPING_MONTH,
        today=date(2026, 2, 1),
    )

    assert report.periods[0].missing_import is True
    assert report.periods[0].spending_minor == 100


@pytest.mark.django_db
def test_quarter_and_year_windows_cover_selected_range():
    windows = list(iter_period_windows(date(2026, 2, 1), date(2026, 8, 1), GROUPING_QUARTER))
    years = list(iter_period_windows(date(2025, 6, 1), date(2026, 2, 1), GROUPING_YEAR))

    assert [item.calendar_start for item in windows] == [date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 1)]
    assert years[0].calendar_start == date(2025, 1, 1)
    assert years[1].calendar_start == date(2026, 1, 1)


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 9, 15))
def test_home_is_cash_flow_with_default_range_and_partial_current_month(_localdate):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(
        owner,
        account,
        transaction_date=date(2026, 8, 10),
        amount_minor=-1200,
        range_start=date(2026, 8, 1),
        range_end=date(2026, 8, 31),
    )
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("home"))
    content = response.content.decode()

    assert response.status_code == 200
    assert ">Cash flow</h1>" in content
    assert "August 2026" in content
    assert "August 2026 (partial)" not in content
    assert "September 2026 (partial)" in content
    assert "12.00 USD" in content
    assert "Missing import" in content
    assert 'role="img"' in content
    assert 'aria-label="' in content
    assert "/static/vendor/chart.umd.min.js" in content
    assert "/static/js/charts.js" in content
    assert "cdn." not in content.lower()
    payload = json_script_payload(content, "cash-flow-chart-data")
    report = response.context["report"]
    assert payload == cash_flow_chart_data(report)
    assert payload["summary"]["spending_minor"] == report.summary.spending_minor == 1200
    assert payload["summary"]["income_display"] == report.summary.income_display
    assert "vs previous range" in content
    periods = response.context["report"].periods
    assert periods[0].start == date(2025, 9, 1)
    assert periods[-1].end == date(2026, 9, 15)
    august = next(item for item in periods if item.label == "August 2026")
    parsed = parse_qs(urlparse(august.drilldown_url).query)
    assert parsed["date_from"] == ["2026-08-01"]
    assert parsed["date_to"] == ["2026-08-31"]


@pytest.mark.django_db
def test_home_empty_state_links_to_csv_import():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(
        reverse("home"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "grouping": "month"},
    )
    content = response.content.decode()

    assert "No visible transactions yet." in content
    assert reverse("account-list") in content
    assert "Missing import" in content


@pytest.mark.django_db
def test_home_filters_and_drilldown_keep_account_and_scope():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private = make_account(owner, name="SECRET PRIVATE LEDGER")
    make_transaction(owner, shared, amount_minor=-1100)
    make_transaction(
        owner,
        private,
        amount_minor=-8800,
        fingerprint="i" * 64,
        description="Synthetic secret spend",
    )
    client = Client()
    client.force_login(member.user)

    response = client.get(
        reverse("home"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "grouping": "month", "scope": "household"},
    )
    content = response.content.decode()
    period = response.context["report"].periods[0]
    parsed = parse_qs(urlparse(period.drilldown_url).query)

    assert "SECRET PRIVATE LEDGER" not in content
    assert period.spending_minor == 1100
    assert parsed["scope"] == ["household"]
    assert "account" not in parsed

    listed = client.get(reverse("transaction-list"), {"date_from": "2026-01-01", "date_to": "2026-01-31", "scope": "household"})
    assert list(listed.context["transactions"]) == list(
        Transaction.objects.filter(account=shared, status=Transaction.Status.ACTIVE)
    )


@pytest.mark.django_db
def test_home_does_not_treat_missing_import_as_a_zero_amount():
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = Client()
    client.force_login(owner.user)
    response = client.get(
        reverse("home"),
        {"date_from": "2026-03-01", "date_to": "2026-03-31", "grouping": "month"},
    )
    content = response.content.decode()
    assert "Missing import" in content
    assert "0.00 USD" in content
    assert "Missing import</td>" not in content


def test_period_clipped_at_the_range_start_is_labeled_partial():
    windows = list(iter_period_windows(date(2025, 9, 1), date(2026, 9, 30), GROUPING_YEAR))
    today = date(2026, 12, 31)

    assert period_label(windows[0], today=today) == "2025 (partial)"
    assert period_label(windows[1], today=today) == "2026 (partial)"
    full_year = list(iter_period_windows(date(2024, 1, 1), date(2024, 12, 31), GROUPING_YEAR))
    assert period_label(full_year[0], today=today) == "2024"


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 9, 30))
@patch("finance.cash_flow.timezone.localdate", return_value=date(2026, 9, 30))
def test_home_applies_default_start_before_range_validation(_cash_today, _view_today):
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(
        reverse("home"),
        {"date_to": "2025-08-31", "grouping": "month"},
    )

    assert response.status_code == 200
    assert response.context["report"] is None
    assert "End date must be on or after start date." in response.content.decode()


@pytest.mark.django_db
def test_home_rejects_dates_past_the_last_safe_period_boundary():
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(
        reverse("home"),
        {"date_from": "9999-12-01", "date_to": "9999-12-31", "grouping": "month"},
    )

    assert response.status_code == 200
    assert response.context["report"] is None
    assert "9998-12-31" in response.content.decode()


@pytest.mark.parametrize("grouping", [GROUPING_WEEK, GROUPING_MONTH, GROUPING_QUARTER, GROUPING_YEAR])
def test_period_count_agrees_with_the_period_windows(grouping):
    for date_from, date_to in (
        (date(2025, 9, 1), date(2026, 9, 30)),
        (date(2024, 2, 29), date(2024, 3, 1)),
        (date(2023, 12, 31), date(2025, 1, 1)),
    ):
        assert period_count(date_from, date_to, grouping) == len(list(iter_period_windows(date_from, date_to, grouping)))


@pytest.mark.django_db
def test_home_rejects_a_range_with_too_many_periods_without_building_it():
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = Client()
    client.force_login(owner.user)

    with patch("finance.cash_flow.iter_period_windows") as iterate:
        response = client.get(
            reverse("home"),
            {"date_from": "0001-01-01", "date_to": "9998-12-31", "grouping": "week"},
        )

    assert response.status_code == 200
    assert response.context["report"] is None
    assert "more than 500 periods" in response.content.decode()
    iterate.assert_not_called()


def test_report_refuses_too_many_periods_when_called_directly():
    date_from, date_to = date(2000, 1, 1), date(2020, 1, 1)
    with pytest.raises(ValueError):
        cash_flow_report(None, date_from=date_from, date_to=date_to, grouping=GROUPING_WEEK)


def test_previous_equal_range_is_the_same_length_immediately_before():
    assert previous_equal_range(date(2026, 2, 1), date(2026, 2, 28)) == (date(2026, 1, 4), date(2026, 1, 31))
    assert previous_equal_range(date(2026, 1, 1), date(2026, 1, 31)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_previous_equal_range_is_none_before_the_earliest_date():
    assert previous_equal_range(date(1, 1, 1), date(1, 1, 31)) == (None, None)
    assert previous_equal_range(date(1, 2, 1), date(1, 2, 28)) == (date(1, 1, 4), date(1, 1, 31))


@pytest.mark.django_db
def test_home_with_a_range_at_year_one_has_no_comparison_instead_of_failing():
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("home"), {"date_from": "0001-01-01", "date_to": "0001-01-31", "grouping": "month"})

    assert response.status_code == 200
    assert response.context["report"].summary.income_change.label == "No earlier range to compare"
    assert b"No earlier range to compare" in response.content
