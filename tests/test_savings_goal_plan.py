from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from finance.models import (
    Account,
    AuditEvent,
    BalanceSnapshot,
    Household,
    ImportBatch,
    Membership,
    Person,
    PlannedItem,
    SavingsGoal,
    Transaction,
)
from finance.planning_services import projected_months_for
from finance.savings_goal_plan import (
    ALREADY_FUNDED,
    BEYOND_HORIZON,
    LATE,
    ON_TIME,
    build_funding_plan,
    fill_goals,
    order_goals,
    set_savings_buffer,
    target_check,
)
from finance.savings_goal_services import save_savings_goal

PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 10, 1)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def add_spending(owner, account, months, amount_minor):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="c" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 12, 28),
    )
    for index, month in enumerate(months):
        Transaction.objects.create(
            account=account,
            import_batch=batch,
            transaction_date=date(2026, month, 10),
            amount_minor=-amount_minor,
            description=f"Synthetic spend {month}",
            kind=Transaction.Kind.CASH_FLOW,
            source_row_number=index + 2,
            fingerprint=f"{account.pk}-{month}".encode().hex().ljust(64, "a")[:64],
            original_fields={"Synthetic Amount": str(-amount_minor)},
        )


def plan_item(owner, name, kind, amount_minor, *, household=None):
    return PlannedItem.objects.create(
        owner=owner,
        household=household,
        scope=PlannedItem.Scope.HOUSEHOLD if household else PlannedItem.Scope.PRIVATE,
        name=name,
        kind=kind,
        amount_minor=amount_minor,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )


def monthly_surplus(owner, income_minor, expense_minor, *, household=None):
    plan_item(owner, "Synthetic pay", PlannedItem.Kind.INCOME, income_minor, household=household)
    plan_item(owner, "Synthetic rent", PlannedItem.Kind.EXPENSE, expense_minor, household=household)


def make_goal(owner, name, target_amount_minor, **kwargs):
    return SavingsGoal.objects.create(owner=owner, name=name, target_amount_minor=target_amount_minor, **kwargs)


def rows_by_name(plan):
    return {row.goal.name: row for row in plan.rows}


def month_label(row):
    return row.funded_month.strftime("%Y-%m") if row.funded_month else None


def summary(plan):
    return (
        [(row.goal.name, month_label(row), row.remaining_minor, row.check) for row in plan.rows],
        plan.buffer_minor,
        [(month.surplus_display, month.held_display, month.available_display) for month in plan.months],
    )


# -- pure fill and ordering ---------------------------------------------------


def goal_stub(pk, priority=None, depends_on_id=None):
    return SimpleNamespace(pk=pk, priority=priority, depends_on_id=depends_on_id)


def test_order_uses_priority_then_id_with_unranked_last():
    goals = [goal_stub(5), goal_stub(4, priority=2), goal_stub(3, priority=1), goal_stub(2, priority=2), goal_stub(1)]
    assert [goal.pk for goal in order_goals(goals)] == [3, 2, 4, 1, 5]


def test_order_places_a_goal_after_its_dependency():
    first_by_rank = goal_stub(1, priority=1, depends_on_id=2)
    dependency = goal_stub(2, priority=5)
    unrelated = goal_stub(3, priority=3)
    assert [goal.pk for goal in order_goals([first_by_rank, dependency, unrelated])] == [3, 2, 1]


def test_order_ignores_a_dependency_outside_the_plan():
    assert [goal.pk for goal in order_goals([goal_stub(1, priority=1, depends_on_id=99), goal_stub(2)])] == [1, 2]


def test_order_survives_a_corrupt_cycle():
    ordered = order_goals([goal_stub(1, depends_on_id=2), goal_stub(2, depends_on_id=1)])
    assert sorted(goal.pk for goal in ordered) == [1, 2]


def test_fill_is_sequential_and_spills_leftover_into_the_next_goal():
    funded, months = fill_goals([(1, 1_500), (2, 1_000), (3, 5_000)], [1_000, 1_000, 1_000], 0)
    # Month 0: 1,000 to goal 1. Month 1: 500 finishes goal 1, 500 starts goal 2.
    # Month 2: 500 finishes goal 2, 500 starts goal 3.
    assert funded == {1: 1, 2: 2, 3: None}
    assert [month.available_minor for month in months] == [1_000, 1_000, 1_000]


def test_fill_sets_the_buffer_aside_once_before_funding_goals():
    funded, months = fill_goals([(1, 1_500)], [1_000, 1_000, 1_000], 1_200)
    assert [(month.held_minor, month.available_minor) for month in months] == [(1_000, 0), (200, 800), (0, 1_000)]
    assert funded == {1: 2}


def test_fill_negative_month_adds_nothing_and_never_unfunds():
    funded, months = fill_goals([(1, 1_000)], [600, -5_000, 600], 0)
    assert funded == {1: 2}
    assert [month.available_minor for month in months] == [600, 0, 600]


def test_fill_negative_month_does_not_count_toward_the_buffer():
    funded, months = fill_goals([(1, 100)], [-300, 250], 200)
    assert [(month.held_minor, month.available_minor) for month in months] == [(0, 0), (200, 50)]
    assert funded == {1: None}


def test_fill_marks_an_already_funded_goal_and_leaves_surplus_to_the_next():
    funded, _months = fill_goals([(1, 0), (2, 400)], [500], 0)
    assert funded == {1: ALREADY_FUNDED, 2: 0}


def test_target_check_treats_the_target_month_as_on_time():
    months = [SimpleNamespace(start=date(2026, 11, 1)), SimpleNamespace(start=date(2026, 12, 1))]
    assert target_check(None, 0, months) is None
    assert target_check(date(2026, 11, 3), 0, months) == ON_TIME
    assert target_check(date(2026, 11, 30), 1, months) == LATE
    assert target_check(date(2026, 12, 1), ALREADY_FUNDED, months) == ON_TIME
    assert target_check(date(2026, 12, 20), None, months) == LATE
    assert target_check(date(2027, 3, 1), None, months) == BEYOND_HORIZON


# -- plan on real data ----------------------------------------------------------


@pytest.mark.django_db
def test_plan_orders_by_priority_and_dates_each_goal():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)
    later = make_goal(owner, "Bicycle", 150_000, priority=2)
    first = make_goal(owner, "Laptop", 200_000, priority=1)
    unranked = make_goal(owner, "Sofa", 100_000)

    plan = build_funding_plan(owner.user, today=TODAY)

    assert [row.goal.pk for row in plan.rows] == [first.pk, later.pk, unranked.pk]
    # 1,000.00 a month from November: Laptop done Dec; Bicycle takes Jan and half of Feb;
    # Sofa takes the other half of Feb and half of Mar.
    assert [month_label(row) for row in plan.rows] == ["2026-12", "2027-02", "2027-03"]


@pytest.mark.django_db
def test_plan_funds_a_dependency_before_the_goal_that_waits_on_it():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)
    dependency = make_goal(owner, "Desk", 100_000, priority=5)
    make_goal(owner, "Monitor", 100_000, priority=1, depends_on=dependency)

    plan = build_funding_plan(owner.user, today=TODAY)

    assert [row.goal.name for row in plan.rows] == ["Desk", "Monitor"]
    assert [month_label(row) for row in plan.rows] == ["2026-11", "2026-12"]
    assert rows_by_name(plan)["Monitor"].depends_on_name == "Desk"


@pytest.mark.django_db
def test_default_buffer_is_one_month_of_average_spending_and_zero_removes_it():
    owner = make_person("owner")
    household = make_household(owner)
    monthly_surplus(owner, 300_000, 200_000)
    add_spending(owner, make_account(owner), [7, 8, 9], 90_000)
    make_goal(owner, "Laptop", 300_000, priority=1)

    default_plan = build_funding_plan(owner.user, today=TODAY)
    set_savings_buffer(owner.user, 0)
    no_buffer_plan = build_funding_plan(owner.user, today=TODAY)

    assert default_plan.buffer_minor == 90_000 and default_plan.buffer_source == "default"
    # 900.00 is set aside first, so 3,000.00 takes four months instead of three.
    assert month_label(default_plan.rows[0]) == "2027-02"
    assert no_buffer_plan.buffer_minor == 0 and no_buffer_plan.buffer_source == "household"
    assert month_label(no_buffer_plan.rows[0]) == "2027-01"
    household.refresh_from_db()
    assert household.savings_buffer_minor == 0


@pytest.mark.django_db
def test_default_buffer_averages_only_the_last_three_complete_months():
    owner = make_person("owner")
    account = make_account(owner)
    add_spending(owner, account, [6], 999_999)
    add_spending(owner, account, [10], 999_999)
    add_spending(owner, make_account(owner, name="Synthetic Card"), [7, 8, 9], 30_001)

    plan = build_funding_plan(owner.user, today=TODAY)

    assert plan.buffer_minor == 30_001


@pytest.mark.django_db
def test_negative_surplus_month_draws_nothing_from_goals():
    owner = make_person("owner")
    plan_item(owner, "Synthetic pay", PlannedItem.Kind.INCOME, 100_000)
    plan_item(owner, "Synthetic rent", PlannedItem.Kind.EXPENSE, 60_000)
    PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.PRIVATE,
        name="Synthetic repair",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=500_000,
        start_date=date(2026, 12, 15),
        cadence=PlannedItem.Cadence.ONE_TIME,
    )
    make_goal(owner, "Laptop", 80_000, priority=1)

    plan = build_funding_plan(owner.user, today=TODAY, horizon=6)

    assert [month.available_display for month in plan.months][:3] == ["400.00 USD", "0.00 USD", "400.00 USD"]
    assert month_label(plan.rows[0]) == "2027-01"


@pytest.mark.django_db
def test_goal_already_funded_by_its_linked_balance_takes_no_surplus():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)
    savings = make_account(owner, name="Synthetic Savings")
    BalanceSnapshot.objects.create(
        account=savings, snapshot_date=date(2026, 9, 30), amount_minor=120_000, currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    make_goal(owner, "Camera", 100_000, priority=1, linked_account=savings, target_date=date(2026, 12, 31))
    make_goal(owner, "Laptop", 100_000, priority=2)

    plan = build_funding_plan(owner.user, today=TODAY)
    rows = rows_by_name(plan)

    assert rows["Camera"].already_funded and rows["Camera"].check == ON_TIME
    assert rows["Camera"].remaining_minor == 0
    assert month_label(rows["Laptop"]) == "2026-11"


@pytest.mark.django_db
def test_time_sensitive_goal_that_misses_its_target_is_flagged():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)
    make_goal(owner, "Laptop", 300_000, priority=1)
    make_goal(owner, "Concert", 100_000, priority=2, time_sensitive=True, target_date=date(2026, 12, 20))
    make_goal(owner, "Trip", 100_000, priority=3, target_date=date(2026, 12, 20))

    plan = build_funding_plan(owner.user, today=TODAY)
    rows = rows_by_name(plan)

    assert month_label(rows["Concert"]) == "2027-02"
    assert rows["Concert"].check == LATE and rows["Concert"].flagged
    assert rows["Trip"].check == LATE and not rows["Trip"].flagged
    assert [row.goal.name for row in plan.flagged] == ["Concert"]


@pytest.mark.django_db
def test_goal_not_funded_inside_the_horizon_is_reported_as_such():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)
    make_goal(owner, "Car", 5_000_000, priority=1, target_date=date(2026, 12, 31), time_sensitive=True)
    make_goal(owner, "Boat", 900_000, priority=2, target_date=date(2030, 1, 1))

    plan = build_funding_plan(owner.user, today=TODAY, horizon=3)
    rows = rows_by_name(plan)

    assert not rows["Car"].within_horizon and rows["Car"].funded_month is None
    assert rows["Car"].check == LATE and rows["Car"].flagged
    assert rows["Boat"].check == BEYOND_HORIZON


@pytest.mark.django_db
def test_completed_and_archived_goals_are_left_out_and_do_not_hold_back_a_dependent():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)
    done = make_goal(owner, "Done", 100_000, priority=1)
    SavingsGoal.objects.filter(pk=done.pk).update(completed_at="2026-09-01T00:00:00Z")
    make_goal(owner, "Next", 100_000, priority=2, depends_on=done)

    plan = build_funding_plan(owner.user, today=TODAY)

    assert [row.goal.name for row in plan.rows] == ["Next"]
    assert month_label(plan.rows[0]) == "2026-11"


@pytest.mark.django_db
def test_goals_do_not_change_the_projection():
    owner = make_person("owner")
    monthly_surplus(owner, 300_000, 200_000)

    def numbers():
        return [
            (month.income_minor, month.spending_minor, month.net_minor)
            for month in projected_months_for(owner.user, today=TODAY, horizon=6)
        ]

    before = numbers()
    make_goal(owner, "Laptop", 100_000, priority=1)
    make_goal(owner, "Sofa", 100_000, priority=2, time_sensitive=True, target_date=date(2026, 12, 1))
    build_funding_plan(owner.user, today=TODAY)

    assert numbers() == before


# -- visibility --------------------------------------------------------------------


def household_fixture():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    monthly_surplus(owner, 400_000, 100_000, household=household)
    shared = make_account(owner, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    add_spending(owner, shared, [7, 8, 9], 60_000)
    make_goal(owner, "Shared sofa", 450_000, priority=1, scope="household", household=household)
    return owner, member, household


@pytest.mark.django_db
def test_household_plan_is_identical_for_every_member_whatever_private_data_exists():
    owner, member, household = household_fixture()
    before_owner = summary(build_funding_plan(owner.user, today=TODAY, scope="household"))
    before_member = summary(build_funding_plan(member.user, today=TODAY, scope="household"))
    assert before_owner == before_member

    private_account = make_account(owner, name=SECRET_ACCOUNT)
    add_spending(owner, private_account, [7, 8, 9], 700_000)
    BalanceSnapshot.objects.create(
        account=private_account, snapshot_date=date(2026, 9, 30), amount_minor=9_000_000, currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    monthly_surplus(owner, 900_000, 100_000)
    make_goal(owner, "Secret vault", 100_000, priority=1)
    SavingsGoal.objects.filter(name="Shared sofa").update(linked_account=private_account)

    after_owner = summary(build_funding_plan(owner.user, today=TODAY, scope="household"))
    after_member = summary(build_funding_plan(member.user, today=TODAY, scope="household"))

    assert after_owner == after_member == before_owner
    assert [name for name, *_ in after_member[0]] == ["Shared sofa"]


SECRET_ACCOUNT = "Owner Secret Vault"


@pytest.mark.django_db
def test_member_never_sees_another_members_private_goals_in_any_plan_scope():
    owner, member, _household = household_fixture()
    make_goal(owner, "Owner secret goal", 100_000, priority=1)

    for scope in ("", "private", "household"):
        plan = build_funding_plan(member.user, today=TODAY, scope=scope)
        assert "Owner secret goal" not in [row.goal.name for row in plan.rows]
    client = Client()
    client.force_login(member.user)
    page = client.get(reverse("savings-goal-plan"))
    assert b"Owner secret goal" not in page.content


@pytest.mark.django_db
def test_private_plan_excludes_household_goals_and_household_items():
    owner, _member, _household = household_fixture()
    plan_item(owner, "Synthetic stipend", PlannedItem.Kind.INCOME, 50_000)
    make_goal(owner, "Private pen", 25_000, priority=1)

    plan = build_funding_plan(owner.user, today=TODAY, scope="private")

    assert [row.goal.name for row in plan.rows] == ["Private pen"]
    assert plan.months[0].surplus_display == "500.00 USD"


@pytest.mark.django_db
def test_household_scope_without_a_household_is_empty():
    loner = make_person("loner")
    make_goal(loner, "Private pen", 25_000)

    plan = build_funding_plan(loner.user, today=TODAY, scope="household")

    assert plan.rows == [] and plan.needs_household and not plan.can_set_buffer


def test_unknown_scope_and_horizon_are_rejected():
    with pytest.raises(ValueError):
        build_funding_plan(object(), today=TODAY, scope="everyone")


# -- buffer setting ------------------------------------------------------------------


@pytest.mark.django_db
def test_buffer_change_is_audited_and_unchanged_value_is_not():
    owner = make_person("owner")
    household = make_household(owner)

    set_savings_buffer(owner.user, 12_345)
    set_savings_buffer(owner.user, 12_345)
    set_savings_buffer(owner.user, None)

    events = AuditEvent.objects.filter(household=household, target_type=AuditEvent.TargetType.SETTING)
    assert [event.changed_fields for event in events] == [["buffer"], ["buffer"]]
    household.refresh_from_db()
    assert household.savings_buffer_minor is None


@pytest.mark.django_db
def test_buffer_rejects_negative_and_requires_a_household():
    owner = make_person("owner")
    with pytest.raises(PermissionDenied):
        set_savings_buffer(owner.user, 100)
    make_household(owner)
    with pytest.raises(ValidationError):
        set_savings_buffer(owner.user, -1)


# -- pages ------------------------------------------------------------------------------


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_plan_page_shows_assumptions_and_ignores_bad_query_values():
    owner = make_person("owner")
    make_household(owner)
    monthly_surplus(owner, 300_000, 200_000)
    make_goal(owner, "Laptop", 200_000, priority=1)

    with patch("finance.savings_goal_plan_views.timezone.localdate", return_value=TODAY):
        page = signed_in(owner).get(reverse("savings-goal-plan"), {"scope": "bogus", "horizon": "7"})

    body = page.content.decode()
    assert page.status_code == 200
    assert page.context["plan"].horizon == 12 and page.context["plan"].scope == ""
    for needle in ("Assumptions", "Horizon: the next 12 months", "Safety buffer", "Laptop", "December 2026"):
        assert needle in body


@pytest.mark.django_db
def test_buffer_form_saves_and_rejects_bad_input():
    owner = make_person("owner")
    household = make_household(owner)
    client = signed_in(owner)

    assert client.post(reverse("savings-goal-buffer"), {"buffer": "250.50"}).status_code == 302
    household.refresh_from_db()
    assert household.savings_buffer_minor == 25_050
    client.post(reverse("savings-goal-buffer"), {"buffer": "-3"})
    client.post(reverse("savings-goal-buffer"), {"buffer": ""})
    household.refresh_from_db()
    assert household.savings_buffer_minor is None


@pytest.mark.django_db
def test_buffer_form_needs_a_household():
    owner = make_person("owner")
    assert signed_in(owner).post(reverse("savings-goal-buffer"), {"buffer": "5"}).status_code == 404


@pytest.mark.django_db
def test_plan_pages_require_sign_in():
    for name in ("savings-goal-plan", "savings-goal-import"):
        assert Client().get(reverse(name)).status_code == 302
    assert Client().post(reverse("savings-goal-buffer"), {"buffer": "5"}).status_code == 302


# -- dependency rules on save ------------------------------------------------------------


def payload(**overrides):
    values = {"scope": "private", "name": "Goal", "target_amount_minor": 10_000, "target_date": None}
    values.update(overrides)
    return values


@pytest.mark.django_db
def test_dependency_cycles_self_and_cross_scope_links_are_rejected():
    owner = make_person("owner")
    household = make_household(owner)
    first = save_savings_goal(owner.user, payload(name="First"))
    second = save_savings_goal(owner.user, payload(name="Second", depends_on=first))
    shared = save_savings_goal(owner.user, payload(name="Shared", scope="household"))

    with pytest.raises(ValidationError):
        save_savings_goal(owner.user, payload(name="First", depends_on=second), goal=first)
    with pytest.raises(ValidationError):
        save_savings_goal(owner.user, payload(name="First", depends_on=first), goal=first)
    with pytest.raises(ValidationError):
        save_savings_goal(owner.user, payload(name="Third", depends_on=shared))
    with pytest.raises(ValidationError):
        save_savings_goal(owner.user, payload(name="Shared two", scope="household", depends_on=first))
    with pytest.raises(ValidationError):
        save_savings_goal(owner.user, payload(name="Second", scope="household"), goal=second)
    assert household.pk == shared.household_id


@pytest.mark.django_db
def test_dependency_on_a_goal_the_editor_cannot_see_is_denied():
    owner = make_person("owner")
    other = make_person("other")
    hidden = make_goal(owner, "Hidden", 10_000)

    with pytest.raises(PermissionDenied):
        save_savings_goal(other.user, payload(name="Mine", depends_on=hidden))


@pytest.mark.django_db
def test_omitted_payload_keys_leave_the_goal_as_it_is():
    owner = make_person("owner")
    savings = make_account(owner, name="Synthetic Savings")
    dependency = make_goal(owner, "Dependency", 10_000)
    goal = save_savings_goal(
        owner.user,
        payload(
            name="Goal",
            priority=3,
            time_sensitive=True,
            depends_on=dependency,
            linked_account=savings,
            target_date=date(2027, 1, 1),
        ),
    )

    save_savings_goal(owner.user, {"scope": "private", "name": "Goal", "target_amount_minor": 20_000}, goal=goal)

    goal.refresh_from_db()
    assert (goal.priority, goal.time_sensitive, goal.depends_on_id, goal.linked_account_id) == (
        3,
        True,
        dependency.pk,
        savings.pk,
    )
    assert goal.target_date == date(2027, 1, 1) and goal.target_amount_minor == 20_000


@pytest.mark.django_db
def test_goal_form_shows_a_dependency_error_and_saves_the_new_fields():
    owner = make_person("owner")
    make_household(owner)
    first = make_goal(owner, "First", 10_000)
    second = make_goal(owner, "Second", 10_000, depends_on=first)
    client = signed_in(owner)
    base = {"name": "First", "target_amount": "100.00", "scope": "private", "priority": "2", "depends_on": second.pk}

    rejected = client.post(reverse("savings-goal-edit", args=[first.pk]), base)
    assert rejected.status_code == 200
    assert "wait on each other" in rejected.content.decode()

    accepted = client.post(
        reverse("savings-goal-edit", args=[second.pk]),
        {**base, "name": "Second", "depends_on": first.pk, "time_sensitive": "on"},
    )
    assert accepted.status_code == 302
    second.refresh_from_db()
    assert (second.priority, second.time_sensitive, second.target_date) == (2, True, None)


@pytest.mark.django_db
def test_goal_list_shows_goals_without_a_date_and_new_badges():
    owner = make_person("owner")
    first = make_goal(owner, "First", 10_000, priority=1, time_sensitive=True)
    make_goal(owner, "Second", 10_000, depends_on=first)

    page = signed_in(owner).get(reverse("savings-goals"))
    body = page.content.decode()

    assert page.status_code == 200
    assert "No target date" in body and "Priority 1" in body and "Time-sensitive" in body
    assert "Buy after First" in body and "None/month" not in body
