from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.http import QueryDict

from finance.cash_flow import cash_flow_chart_data, format_minor
from finance.models import PlannedItem, SavingsGoal
from finance.planning_services import visible_projection_inputs
from finance.projection import KIND_EXPENSE, KIND_INCOME, SOURCE_SERIES, project_cash_flow
from finance.scenario import (
    CHANGE_ADD,
    CHANGE_AMOUNT,
    CHANGE_ONEOFF,
    CHANGE_PAUSE,
    apply_scenario,
    compare_projected_months,
    encode_change,
    make_this_real_url,
    parse_scenario_tokens,
    scenario_projected_months,
)
from tests.page_payload import json_script_payload
from tests.test_projection import PASSWORD, make_account, make_household, make_person, make_transaction, planned_row


def add_change(**overrides):
    row = dict(
        type=CHANGE_ADD,
        name="Synthetic raise",
        kind=KIND_INCOME,
        amount_minor=50000,
        cadence="monthly",
        start=date(2026, 11, 1),
        end=None,
    )
    row.update(overrides)
    return SimpleNamespace(**row)


def test_empty_scenario_matches_baseline_exactly():
    today = date(2026, 10, 1)
    items = (planned_row(),)
    baseline = project_cash_flow(items, today=today, horizon=3)
    scenario = scenario_projected_months(items, (), today=today, horizon=3)
    assert [row.net_minor for row in scenario] == [row.net_minor for row in baseline]
    assert [row.income_minor for row in scenario] == [row.income_minor for row in baseline]
    assert [row.spending_minor for row in scenario] == [row.spending_minor for row in baseline]
    compared = compare_projected_months(baseline, scenario)
    assert all(row.difference_minor == 0 for row in compared)


def test_added_monthly_income_is_hand_checked():
    today = date(2026, 10, 1)
    items = (planned_row(),)
    change = add_change()
    rows = scenario_projected_months(items, (change,), today=today, horizon=3)
    # Baseline spending is rent 100.00 on the 15th of each month. Raise is 500.00
    # on the 1st of Nov, Dec, and Jan.
    assert [row.income_minor for row in rows] == [50000, 50000, 50000]
    assert [row.spending_minor for row in rows] == [10000, 10000, 10000]
    assert [row.net_minor for row in rows] == [40000, 40000, 40000]


def test_amount_change_is_hand_checked():
    today = date(2026, 10, 1)
    items = (planned_row(source_id=12),)
    change = SimpleNamespace(type=CHANGE_AMOUNT, source_id=12, amount_minor=15000)
    rows = scenario_projected_months(items, (change,), today=today, horizon=3)
    assert [row.spending_minor for row in rows] == [15000, 15000, 15000]
    assert [row.income_minor for row in rows] == [0, 0, 0]


def test_pause_series_from_a_date_is_hand_checked():
    today = date(2026, 10, 1)
    series = planned_row(
        name="Synthetic streamer",
        amount_minor=2500,
        start=date(2026, 10, 10),
        cadence="monthly",
        source=SOURCE_SERIES,
        source_id=5,
    )
    change = SimpleNamespace(type=CHANGE_PAUSE, source_id=5, pause_from=date(2026, 12, 1))
    rows = scenario_projected_months((series,), (change,), today=today, horizon=3)
    # Occurrences would be Nov 10, Dec 10, Jan 10. Pause from Dec 1 keeps only Nov.
    assert [row.spending_minor for row in rows] == [2500, 0, 0]


def test_one_off_amount_is_hand_checked():
    today = date(2026, 10, 1)
    items = (planned_row(),)
    change = SimpleNamespace(
        type=CHANGE_ONEOFF,
        name="Synthetic car tax",
        kind=KIND_EXPENSE,
        amount_minor=25000,
        cadence="one_time",
        start=date(2026, 11, 20),
        end=None,
    )
    rows = scenario_projected_months(items, (change,), today=today, horizon=3)
    assert [row.spending_minor for row in rows] == [35000, 10000, 10000]
    assert [row.net_minor for row in rows] == [-35000, -10000, -10000]


def test_apply_does_not_mutate_baseline_inputs():
    items = [planned_row(source_id=12)]
    original_amount = items[0].amount_minor
    apply_scenario(items, (SimpleNamespace(type=CHANGE_AMOUNT, source_id=12, amount_minor=1),))
    assert items[0].amount_minor == original_amount


def test_hidden_ids_are_ignored_without_inventing_rows():
    items = (planned_row(source_id=1),)
    changes = (
        SimpleNamespace(type=CHANGE_AMOUNT, source_id=99, amount_minor=1),
        SimpleNamespace(type=CHANGE_PAUSE, source_id=99, pause_from=date(2026, 11, 1)),
    )
    result = apply_scenario(items, changes)
    assert len(result) == 1
    assert result[0].amount_minor == 10000
    assert result[0].end is None


def test_round_trip_tokens_and_skips_invalid():
    change = add_change(name="Raise: extra", end=date(2027, 1, 1))
    token = encode_change(change)
    query = QueryDict(mutable=True)
    query.appendlist("sc", token)
    query.appendlist("sc", "not-a-change")
    query.appendlist("sc", "m|0|100")
    parsed = parse_scenario_tokens(query)
    assert len(parsed) == 1
    assert parsed[0].name == "Raise: extra"
    assert parsed[0].end == date(2027, 1, 1)


@pytest.mark.django_db
def test_scenario_scope_follows_projection_and_ignores_private_ids():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    secret = PlannedItem.objects.create(
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
    owner_inputs = visible_projection_inputs(owner)
    member_inputs = visible_projection_inputs(member)
    change = SimpleNamespace(type=CHANGE_AMOUNT, source_id=secret.pk, amount_minor=1)
    owner_rows = scenario_projected_months(owner_inputs, (change,), today=date(2026, 10, 1), horizon=3)
    member_rows = scenario_projected_months(member_inputs, (change,), today=date(2026, 10, 1), horizon=3)
    assert owner_rows[0].spending_minor == 1
    assert member_rows[0].spending_minor == 0
    assert member_rows[0].income_minor == 3000
    member_names = {item.name for month in member_rows for item in month.contributions}
    assert "Secret planned" not in member_names


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 10, 1))
def test_home_without_changes_matches_baseline(_localdate):
    owner = make_person("owner")
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
    page = client.get(reverse("home"), {"horizon": "3"})
    compared = page.context["scenario_comparison"]
    assert compared
    assert all(row.difference_minor == 0 for row in compared)
    assert all(row.baseline.net_minor == row.scenario.net_minor for row in compared)


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 10, 1))
def test_home_shows_baseline_and_scenario_without_saving(_localdate):
    owner = make_person("owner")
    make_household(owner)
    item = PlannedItem.objects.create(
        owner=owner,
        name="Synthetic rent",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=10000,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    SavingsGoal.objects.create(
        owner=owner,
        name="Synthetic emergency",
        target_amount_minor=100000,
        target_date=date(2026, 12, 15),
        manual_amount_minor=10000,
        manual_amount_date=date(2026, 10, 1),
    )
    client = Client()
    client.force_login(owner.user)
    token = encode_change(SimpleNamespace(type=CHANGE_AMOUNT, source_id=item.pk, amount_minor=15000))
    page = client.get(
        reverse("home"),
        {
            "date_from": "2026-09-01",
            "date_to": "2026-10-01",
            "grouping": "month",
            "horizon": "3",
            "sc": token,
        },
    )
    html = page.content.decode()
    assert page.status_code == 200
    assert "This is a projection" in html
    assert "not a forecast" in html
    assert "Make this real" in html
    assert "Synthetic emergency" in html
    assert "2026-12-15" in html
    payload = json_script_payload(html, "cash-flow-chart-data")
    projected = [row for row in payload["periods"] if row["projected"]]
    assert projected[0]["spending_minor"] == 10000
    assert projected[0]["scenario_net_minor"] == -15000
    assert projected[0]["net_minor"] == -10000
    assert PlannedItem.objects.get().amount_minor == 10000
    compared = page.context["scenario_comparison"]
    assert compared[0].difference_minor == -5000
    assert compared[0].difference_display == format_minor(-5000)


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 10, 1))
def test_home_scenario_hides_other_member_private_goal_and_item(_localdate):
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    secret = PlannedItem.objects.create(
        owner=owner,
        name="Secret planned",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=7777,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    SavingsGoal.objects.create(
        owner=owner,
        name="Secret goal",
        target_amount_minor=50000,
        target_date=date(2027, 1, 1),
    )
    SavingsGoal.objects.create(
        owner=owner,
        household=household,
        scope=SavingsGoal.Scope.HOUSEHOLD,
        name="Shared goal",
        target_amount_minor=80000,
        target_date=date(2027, 6, 1),
    )
    client = Client()
    client.force_login(member.user)
    token = encode_change(SimpleNamespace(type=CHANGE_AMOUNT, source_id=secret.pk, amount_minor=1))
    page = client.get(reverse("home"), {"horizon": "3", "sc": token})
    html = page.content.decode()
    assert "Secret planned" not in html
    assert "Secret goal" not in html
    assert "Shared goal" in html
    assert token not in html


@pytest.mark.django_db
def test_adding_a_scenario_does_not_write_rows():
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)
    before = PlannedItem.objects.count()
    response = client.get(
        reverse("home"),
        {
            "scenario_action": "add",
            "change_type": CHANGE_ADD,
            "name": "Synthetic raise",
            "kind": KIND_INCOME,
            "amount": "500.00",
            "cadence": "monthly",
            "start_date": "2026-11-01",
            "horizon": "3",
        },
    )
    assert response.status_code == 302
    assert PlannedItem.objects.count() == before
    assert "sc=" in response["Location"]


@pytest.mark.django_db
def test_make_this_real_prefills_planned_item_form():
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)
    url = make_this_real_url(add_change())
    page = client.get(url)
    html = page.content.decode()
    assert page.status_code == 200
    assert 'value="Synthetic raise"' in html
    assert 'value="500.00"' in html
    assert PlannedItem.objects.count() == 0


def test_pausing_from_the_first_representable_day_pauses_the_whole_series():
    today = date(2026, 10, 1)
    series = planned_row(
        name="Synthetic streamer",
        amount_minor=2500,
        start=date(2026, 10, 10),
        cadence="monthly",
        source=SOURCE_SERIES,
        source_id=5,
    )
    change = SimpleNamespace(type=CHANGE_PAUSE, source_id=5, pause_from=date.min)

    rows = scenario_projected_months((series,), (change,), today=today, horizon=3)

    assert [row.spending_minor for row in rows] == [0, 0, 0]


@pytest.mark.django_db
def test_an_invalid_grouping_with_a_huge_range_is_not_a_server_error():
    owner = make_person("owner")
    make_household(owner)
    client = Client()
    client.force_login(owner.user)

    page = client.get(reverse("home"), {"date_from": "1900-01-01", "date_to": "2026-10-01", "grouping": "invalid"})

    assert page.status_code in (200, 400)


def _tokens(*values):
    from django.http import QueryDict

    query = QueryDict(mutable=True)
    query.setlist("sc", list(values))
    return query


def test_each_change_type_round_trips_through_the_query_string():
    from finance.scenario import encode_changes

    changes = (
        add_change(end=date(2027, 6, 1)),
        SimpleNamespace(type=CHANGE_AMOUNT, source_id=7, amount_minor=12_345),
        SimpleNamespace(type=CHANGE_PAUSE, source_id=9, pause_from=date(2026, 12, 1)),
        SimpleNamespace(
            type=CHANGE_ONEOFF,
            name="Synthetic bonus|extra",
            kind="income",
            amount_minor=50_000,
            start=date(2026, 11, 15),
        ),
    )

    parsed = parse_scenario_tokens(_tokens(*encode_changes(changes)))

    assert [change.type for change in parsed] == [CHANGE_ADD, CHANGE_AMOUNT, CHANGE_PAUSE, CHANGE_ONEOFF]
    assert parsed[0].end == date(2027, 6, 1)
    assert parsed[1].source_id == 7 and parsed[1].amount_minor == 12_345
    assert parsed[2].pause_from == date(2026, 12, 1)
    assert parsed[3].name == "Synthetic bonus extra" and parsed[3].amount_minor == 50_000


@pytest.mark.parametrize(
    "token",
    [
        "",
        "z|1|2",
        "a|income|100|monthly|not-a-date||Name",
        "a|income|-5|monthly|2026-10-01||Name",
        "a|gift|100|monthly|2026-10-01||Name",
        "a|income|100|hourly|2026-10-01||Name",
        "a|income|100|monthly|2026-10-01||",
        "a|income|100|monthly|2026-10-01|2026-09-01|Ends before it starts",
        "a|income|100|monthly|2026-10-01||" + "x" * 151,
        "m|0|100",
        "m|abc|100",
        "m|5|0",
        "p|5|someday",
        "p|-1|2026-10-01",
        "o|income|100|bad-date|Name",
        "o|gift|100|2026-10-01|Name",
        "o|income|100|2026-10-01|",
    ],
)
def test_malformed_tokens_are_ignored(token):
    assert parse_scenario_tokens(_tokens(token)) == ()


def test_only_the_first_max_changes_tokens_are_kept():
    from finance.scenario import MAX_CHANGES

    tokens = [f"m|{index}|100" for index in range(1, MAX_CHANGES + 5)]

    assert len(parse_scenario_tokens(_tokens(*tokens))) == MAX_CHANGES


def test_scenario_form_requires_the_fields_each_change_type_needs():
    from finance.forms import ScenarioChangeForm

    add = ScenarioChangeForm({"change_type": "add"})
    oneoff = ScenarioChangeForm({"change_type": "oneoff", "name": "Synthetic gift", "kind": "income", "amount": "10.00"})
    amount = ScenarioChangeForm({"change_type": "amount"})
    pause = ScenarioChangeForm({"change_type": "pause"})
    backwards = ScenarioChangeForm(
        {
            "change_type": "add",
            "name": "Synthetic job",
            "kind": "income",
            "amount": "100.00",
            "cadence": "monthly",
            "start_date": "2026-10-01",
            "end_date": "2026-09-01",
        }
    )
    negative = ScenarioChangeForm({"change_type": "amount", "amount": "-1.00"})

    assert not add.is_valid() and {"name", "kind", "amount", "start_date", "cadence"} <= set(add.errors)
    assert not oneoff.is_valid() and "start_date" in oneoff.errors
    assert not amount.is_valid() and {"planned_item", "amount"} <= set(amount.errors)
    assert not pause.is_valid() and {"series", "pause_from"} <= set(pause.errors)
    assert not backwards.is_valid() and "end_date" in backwards.errors
    assert not negative.is_valid() and "amount" in negative.errors


def test_scenario_form_builds_add_and_one_off_changes():
    from finance.forms import ScenarioChangeForm

    add = ScenarioChangeForm(
        {
            "change_type": "add",
            "name": "Synthetic job",
            "kind": "income",
            "amount": "1234.56",
            "cadence": "monthly",
            "start_date": "2026-10-01",
        }
    )
    oneoff = ScenarioChangeForm(
        {"change_type": "oneoff", "name": "Synthetic gift", "kind": "income", "amount": "10.00", "start_date": "2026-11-01"}
    )

    assert add.is_valid(), add.errors
    assert oneoff.is_valid(), oneoff.errors
    added = add.to_change()
    once = oneoff.to_change()
    assert (added.type, added.amount_minor, added.cadence, added.end) == (CHANGE_ADD, 123_456, "monthly", None)
    assert (once.type, once.amount_minor, once.start) == (CHANGE_ONEOFF, 1_000, date(2026, 11, 1))
