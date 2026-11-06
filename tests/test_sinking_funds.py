"""Sinking funds (#670): one account split across savings goals.

Covers the assignment math, over-assignment and balance drops, the planned-item
link, the monthly suggestion and its confirmation, automatic draw-down and
undo, the projection outflow, visibility, and goals that keep their old meaning.
All figures are synthetic.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.fund_draw_downs import apply_fund_draw_downs
from finance.fund_projection import SOURCE_SET_ASIDE, fund_schedule
from finance.fund_services import (
    account_position,
    assign_to_fund,
    confirm_set_asides,
    evaluate_fund_alerts,
    monthly_set_asides,
    release_from_fund,
    resolve_shortfall,
    suggested_set_aside,
    undo_draw_down,
)
from finance.funds import assigned_minor, fund_due_date
from finance.models import (
    Account,
    Alert,
    AuditEvent,
    BalanceSnapshot,
    Category,
    FundEntry,
    Household,
    ImportBatch,
    Membership,
    Person,
    PlannedItem,
    SavingsGoal,
    Transaction,
    TransferPair,
)
from finance.planning_services import cash_flow_with_projection, projected_months_for
from finance.savings_goal_services import (
    FUND_ACCOUNT_TYPE_ERROR,
    FUND_MOVE_ERROR,
    FUND_OFF_ERROR,
    FUND_SCOPE_ERROR,
    PLANNED_ITEM_SCOPE_ERROR,
    PLANNED_ITEM_TAKEN_ERROR,
    SOURCE_FUND,
    SOURCE_MANUAL,
    SOURCE_SNAPSHOT,
    goal_progress,
    save_savings_goal,
)

PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 11, 5)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, *, name="Synthetic Cash Plus", household=None, account_type=Account.Type.SAVINGS):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD if household else Account.Scope.PRIVATE,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if household else "",
    )


def add_balance(account, amount_minor, *, on=TODAY, source=BalanceSnapshot.Source.MANUAL):
    return BalanceSnapshot.objects.create(
        account=account, snapshot_date=on, amount_minor=amount_minor, currency="USD", source=source
    )


def make_fund(owner, account, *, name="Synthetic Insurance", target=120_000, target_date=date(2027, 4, 30),
              household=None, priority=None):
    return SavingsGoal.objects.create(
        owner=owner,
        name=name,
        target_amount_minor=target,
        target_date=target_date,
        scope=SavingsGoal.Scope.HOUSEHOLD if household else SavingsGoal.Scope.PRIVATE,
        household=household,
        linked_account=account,
        share_of_account=True,
        priority=priority,
    )


def make_item(owner, *, name="Synthetic Car Insurance", amount=120_000, start=date(2027, 3, 15),
              cadence=PlannedItem.Cadence.ANNUAL, household=None, category=None, kind=PlannedItem.Kind.EXPENSE):
    return PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.HOUSEHOLD if household else PlannedItem.Scope.PRIVATE,
        household=household,
        name=name,
        kind=kind,
        amount_minor=amount,
        start_date=start,
        cadence=cadence,
        category=category,
    )


def link_item(goal, item, *, linked_on=TODAY):
    goal.funds_planned_item = item
    goal.planned_item_linked_on = linked_on
    goal.save(update_fields=("funds_planned_item", "planned_item_linked_on"))
    return goal


def make_transaction(owner, account, *, on, amount_minor, description="SYNTHETIC INSURER PAYMENT", category=None):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="e" * 64,
        date_range_start=on - timedelta(days=30),
        date_range_end=on,
    )
    digest = f"{account.pk}-{amount_minor}-{on}-{description}".encode().hex().ljust(64, "a")[:64]
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=on,
        amount_minor=amount_minor,
        description=description,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic": "row"},
        category=category,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


# -- assignment math --


@pytest.mark.django_db
def test_several_funds_split_one_account_and_unassigned_is_the_rest():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 500_000)
    insurance = make_fund(owner, account)
    trip = make_fund(owner, account, name="Synthetic Trip", target=300_000)

    assign_to_fund(owner, insurance, 40_000, today=TODAY)
    assign_to_fund(owner, trip, 100_000, today=TODAY)
    release_from_fund(owner, trip, 25_000, today=TODAY)

    position = account_position(account, today=TODAY)
    assert {row.goal.pk: row.assigned_minor for row in position.funds} == {insurance.pk: 40_000, trip.pk: 75_000}
    assert position.assigned_minor == 115_000
    assert position.unassigned_minor == 500_000 - 115_000
    assert position.shortfall_minor == 0
    card = goal_progress(owner, trip, today=TODAY)
    assert (card.current_amount_minor, card.source) == (75_000, SOURCE_FUND)
    assert card.linked_account == account
    kinds = list(FundEntry.objects.filter(goal=trip).order_by("pk").values_list("kind", "reason", "amount_minor"))
    assert kinds == [("assign", "manual", 100_000), ("release", "manual", -25_000)]
    assert AuditEvent.objects.filter(target_type="goal", target_id=trip.pk, changed_fields=["assigned"]).count() == 2


@pytest.mark.django_db
def test_assignments_cannot_exceed_the_balance_or_need_a_balance_first():
    owner = make_person("owner")
    account = make_account(owner)
    fund = make_fund(owner, account)
    with pytest.raises(ValidationError, match="Record a balance"):
        assign_to_fund(owner, fund, 1_000, today=TODAY)
    add_balance(account, 50_000)
    other = make_fund(owner, account, name="Synthetic Gifts")
    assign_to_fund(owner, other, 30_000, today=TODAY)
    with pytest.raises(ValidationError, match="unassigned balance"):
        assign_to_fund(owner, fund, 20_001, today=TODAY)
    assign_to_fund(owner, fund, 20_000, today=TODAY)
    assert account_position(account, today=TODAY).unassigned_minor == 0
    with pytest.raises(ValidationError, match="more than"):
        release_from_fund(owner, fund, 20_001, today=TODAY)


@pytest.mark.django_db
def test_over_assignment_is_a_form_error_on_the_fund_page():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 10_000, on=timezone.localdate())
    fund = make_fund(owner, account)
    response = signed_in(owner).post(
        reverse("savings-goal-fund", args=[fund.pk]), {"action": "assign", "amount": "$100.01"}
    )
    assert response.status_code == 200
    assert "unassigned balance" in response.content.decode()
    assert not FundEntry.objects.exists()
    response = signed_in(owner).post(
        reverse("savings-goal-fund", args=[fund.pk]), {"action": "assign", "amount": "$100"}
    )
    assert response.status_code == 302
    assert assigned_minor(fund) == 10_000


@pytest.mark.django_db
def test_balance_drop_below_assigned_is_flagged_and_member_chooses_which_fund_to_reduce():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 100_000, on=TODAY - timedelta(days=3))
    first = make_fund(owner, account, name="Synthetic First")
    second = make_fund(owner, account, name="Synthetic Second")
    assign_to_fund(owner, first, 60_000, today=TODAY)
    assign_to_fund(owner, second, 40_000, today=TODAY)
    # A sync brings a lower balance: nothing is reduced on its own.
    add_balance(account, 70_000, source=BalanceSnapshot.Source.SIMPLEFIN)

    position = account_position(account, today=TODAY)
    assert position.shortfall_minor == 30_000
    assert position.unassigned_minor == -30_000
    assert (assigned_minor(first), assigned_minor(second)) == (60_000, 40_000)
    alerts = evaluate_fund_alerts(today=TODAY)
    shortfall = [alert for alert in alerts if alert.dedupe_key.startswith("fund-shortfall:")]
    assert [alert.recipient_id for alert in shortfall] == [owner.pk]
    assert shortfall[0].link == reverse("fund-shortfall", args=[account.pk])
    assert "$" not in shortfall[0].title
    assert evaluate_fund_alerts(today=TODAY) == []

    resolve_shortfall(owner, account, [(first, 10_000), (second, 20_000)], today=TODAY)
    assert account_position(account, today=TODAY).shortfall_minor == 0
    reasons = set(FundEntry.objects.filter(kind="release").values_list("reason", flat=True))
    assert reasons == {"balance_drop"}
    with pytest.raises(ValidationError):
        resolve_shortfall(owner, account, [(first, 60_000)], today=TODAY)


@pytest.mark.django_db
def test_shortfall_flag_shows_on_goals_and_account_pages_and_the_page_reduces_funds():
    owner = make_person("owner")
    account = make_account(owner)
    today = timezone.localdate()
    add_balance(account, 100_000, on=today - timedelta(days=1))
    fund = make_fund(owner, account, name="Synthetic Roof")
    assign_to_fund(owner, fund, 80_000, today=today)
    add_balance(account, 50_000, on=today)
    client = signed_in(owner)
    for url in (reverse("savings-goals"), reverse("account-balances", args=[account.pk])):
        page = client.get(url).content.decode()
        assert "Below assigned" in page
        assert reverse("fund-shortfall", args=[account.pk]) in page
        assert "Synthetic Roof" in page
    response = client.post(reverse("fund-shortfall", args=[account.pk]), {f"reduce_{fund.pk}": "300"})
    assert response.status_code == 302
    assert assigned_minor(fund) == 50_000
    assert "Below assigned" not in client.get(reverse("savings-goals")).content.decode()


# -- fund setup and scope --


@pytest.mark.django_db
def test_fund_scope_must_match_its_account_and_needs_a_cash_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared Cash", household=household)
    private = make_account(owner, name="Synthetic Own Cash")
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    base = {"name": "Synthetic Fund", "target_amount_minor": 50_000, "share_of_account": True}
    with pytest.raises(ValidationError, match=re.escape(FUND_SCOPE_ERROR)):
        save_savings_goal(owner, {**base, "scope": "private", "linked_account": shared})
    with pytest.raises(ValidationError, match=re.escape(FUND_SCOPE_ERROR)):
        save_savings_goal(owner, {**base, "scope": "household", "linked_account": private})
    with pytest.raises(ValidationError, match=re.escape(FUND_ACCOUNT_TYPE_ERROR)):
        save_savings_goal(owner, {**base, "scope": "private", "linked_account": card})
    goal = save_savings_goal(owner, {**base, "scope": "household", "linked_account": shared})
    assert goal.share_of_account and goal.manual_amount_minor is None
    add_balance(shared, 10_000)
    assign_to_fund(member, goal, 5_000, today=TODAY)
    with pytest.raises(ValidationError, match=re.escape(FUND_MOVE_ERROR)):
        save_savings_goal(owner, {**base, "scope": "private", "linked_account": private}, goal=goal)
    with pytest.raises(ValidationError, match=re.escape(FUND_OFF_ERROR)):
        save_savings_goal(owner, {**base, "scope": "household", "share_of_account": False}, goal=goal)


@pytest.mark.django_db
def test_fund_form_reports_a_scope_mismatch_instead_of_saving():
    owner = make_person("owner")
    household = make_household(owner)
    shared = make_account(owner, name="Synthetic Shared Cash", household=household)
    response = signed_in(owner).post(
        reverse("savings-goals"),
        {"name": "Synthetic Fund", "target_amount": "500", "scope": "private", "linked_account": shared.pk,
         "share_of_account": "on"},
    )
    assert response.status_code == 200
    assert "household fund needs a household account" in response.content.decode()
    assert not SavingsGoal.objects.exists()


@pytest.mark.django_db
def test_linking_a_planned_item_sets_target_and_date_and_shows_the_monthly_set_aside():
    owner = make_person("owner")
    account = make_account(owner)
    item = make_item(owner, amount=120_000, start=date(2026, 3, 15))
    today = timezone.localdate()
    response = signed_in(owner).get(reverse("savings-goals") + f"?fund_item={item.pk}")
    initial = response.context["add_form"].initial
    assert (initial["share_of_account"], initial["funds_planned_item"], initial["name"]) == (True, item.pk, item.name)
    response = signed_in(owner).post(
        reverse("savings-goals"),
        {"name": "Synthetic Insurance Fund", "target_amount": "", "scope": "private",
         "linked_account": account.pk, "share_of_account": "on", "funds_planned_item": item.pk},
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    goal = SavingsGoal.objects.get()
    assert goal.funds_planned_item_id == item.pk
    assert goal.target_amount_minor == 120_000
    next_date = fund_due_date(goal, today)
    assert goal.target_date == next_date
    assert next_date >= today and next_date.month == 3 and next_date.day == 15
    assert goal.planned_item_linked_on == today
    card = goal_progress(owner, goal, today=today)
    assert card.monthly_needed_minor == suggested_set_aside(120_000, 0, next_date, on=today)
    goals_page = signed_in(owner).get(reverse("savings-goals")).content.decode()
    assert "/month set-aside" in goals_page
    items_page = signed_in(owner).get(reverse("planned-items")).content.decode()
    assert "Funded by Synthetic Insurance Fund" in items_page
    assert "/month" in items_page


@pytest.mark.django_db
def test_planned_item_link_stays_in_scope_and_one_goal_per_item():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared Cash", household=household)
    private_item = make_item(owner, name="Synthetic Private Bill")
    shared_item = make_item(owner, name="Synthetic Shared Bill", household=household)
    base = {"name": "Synthetic Fund", "target_amount_minor": 50_000, "share_of_account": True,
            "scope": "household", "linked_account": shared}
    with pytest.raises(ValidationError, match=re.escape(PLANNED_ITEM_SCOPE_ERROR)):
        save_savings_goal(owner, {**base, "funds_planned_item": private_item})
    with pytest.raises(PermissionDenied):
        save_savings_goal(member, {**base, "funds_planned_item": private_item})
    save_savings_goal(owner, {**base, "funds_planned_item": shared_item})
    with pytest.raises(ValidationError, match=re.escape(PLANNED_ITEM_TAKEN_ERROR)):
        save_savings_goal(member, {**base, "name": "Synthetic Second", "funds_planned_item": shared_item})


@pytest.mark.django_db
def test_an_item_taken_private_no_longer_drives_a_household_fund():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, household=household)
    goal = make_fund(owner, shared, household=household, target_date=date(2027, 6, 1))
    item = make_item(owner, household=household, start=date(2027, 1, 10))
    link_item(goal, item)
    assert fund_due_date(goal, TODAY) == date(2027, 1, 10)
    item.scope = PlannedItem.Scope.PRIVATE
    item.household = None
    item.save()
    goal.refresh_from_db()
    assert fund_due_date(goal, TODAY) == date(2027, 6, 1)
    assert goal_progress(member, goal, today=TODAY).funded_item is None


@pytest.mark.django_db
def test_an_edit_that_leaves_the_target_out_keeps_a_linked_funds_target():
    owner = make_person("owner")
    account = make_account(owner)
    item = make_item(owner, amount=120_000)
    goal = save_savings_goal(owner, {"scope": "private", "name": "Synthetic Fund", "share_of_account": True,
                                     "linked_account": account, "funds_planned_item": item,
                                     "target_amount_minor": 150_000})
    save_savings_goal(owner, {"scope": "private", "name": "Synthetic Renamed", "funds_planned_item": item}, goal=goal)
    goal.refresh_from_db()
    assert (goal.name, goal.target_amount_minor, goal.funds_planned_item_id) == ("Synthetic Renamed", 150_000, item.pk)


# -- monthly suggestion and confirmation --


def test_suggested_set_aside_formula():
    # (target − assigned) ÷ months until due, a partial month counting, rounded up.
    assert suggested_set_aside(120_000, 0, date(2027, 4, 30), on=date(2026, 11, 5)) == 20_000
    assert suggested_set_aside(120_000, 20_000, date(2027, 4, 30), on=date(2026, 12, 1)) == 20_000
    assert suggested_set_aside(100_000, 0, date(2027, 1, 4), on=date(2026, 11, 5)) == 50_000
    assert suggested_set_aside(10_000, 0, date(2026, 11, 20), on=date(2026, 11, 5)) == 10_000
    assert suggested_set_aside(10_000, 3_333, date(2027, 1, 31), on=date(2026, 11, 5)) == 2_223
    assert suggested_set_aside(10_000, 10_000, date(2027, 1, 31), on=date(2026, 11, 5)) is None
    assert suggested_set_aside(10_000, 0, None, on=date(2026, 11, 5)) is None
    assert suggested_set_aside(10_000, 0, date(2026, 11, 4), on=date(2026, 11, 5)) is None


@pytest.mark.django_db
def test_monthly_suggestion_caps_at_unassigned_in_fund_order_and_confirm_records_history():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 30_000)
    first = make_fund(owner, account, name="Synthetic First", target=120_000, priority=1)
    second = make_fund(owner, account, name="Synthetic Second", target=60_000, priority=2)
    undated = make_fund(owner, account, name="Synthetic Someday", target=60_000, target_date=None)

    plan = monthly_set_asides(owner, today=TODAY)
    by_goal = {row.goal.pk: row for row in plan.rows}
    assert by_goal[first.pk].suggested_minor == 20_000
    assert by_goal[second.pk].suggested_minor == 10_000
    assert by_goal[undated.pk].suggested_minor is None
    assert [row.goal.pk for row in plan.pending] == [first.pk, second.pk]
    assert (by_goal[first.pk].default_minor, by_goal[second.pk].default_minor) == (20_000, 10_000)

    # Edited amount: the member sets aside less for the second fund.
    confirm_set_asides(owner, [(first, 20_000), (second, 5_000)], today=TODAY)
    entries = FundEntry.objects.filter(reason="monthly").order_by("goal_id")
    assert [(entry.goal_id, entry.amount_minor, entry.month) for entry in entries] == [
        (first.pk, 20_000, date(2026, 11, 1)),
        (second.pk, 5_000, date(2026, 11, 1)),
    ]
    assert all(entry.actor_id == owner.pk for entry in entries)
    with pytest.raises(ValidationError, match="already confirmed"):
        confirm_set_asides(owner, [(second, 1_000)], today=TODAY)
    plan = monthly_set_asides(owner, today=TODAY)
    assert plan.pending == []
    assert {row.goal.pk for row in plan.rows if row.confirmed} == {first.pk, second.pk}
    # December asks again, from the new assigned amounts.
    december = monthly_set_asides(owner, today=date(2026, 12, 2))
    assert {row.goal.pk: row.suggested_minor for row in december.pending} == {
        first.pk: 20_000, second.pk: 11_000,
    }


@pytest.mark.django_db
def test_confirm_refuses_more_than_the_unassigned_balance_together():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 25_000)
    first = make_fund(owner, account, name="Synthetic First")
    second = make_fund(owner, account, name="Synthetic Second")
    with pytest.raises(ValidationError, match="unassigned balance"):
        confirm_set_asides(owner, [(first, 20_000), (second, 10_000)], today=TODAY)
    assert not FundEntry.objects.exists()


@pytest.mark.django_db
def test_set_aside_page_lists_checkboxes_and_confirms_checked_rows_with_edits():
    owner = make_person("owner")
    account = make_account(owner)
    today = timezone.localdate()
    add_balance(account, 500_000, on=today)
    first = make_fund(owner, account, name="Synthetic First", target_date=today + timedelta(days=200))
    second = make_fund(owner, account, name="Synthetic Second", target_date=today + timedelta(days=200))
    client = signed_in(owner)
    page = client.get(reverse("fund-set-asides")).content.decode()
    assert f'name="pick_{first.pk}"' in page and f'name="amount_{second.pk}"' in page
    response = client.post(
        reverse("fund-set-asides"),
        {f"pick_{first.pk}": "on", f"amount_{first.pk}": "$1,234.50", f"amount_{second.pk}": "99"},
    )
    assert response.status_code == 302
    assert list(FundEntry.objects.values_list("goal_id", "amount_minor")) == [(first.pk, 123_450)]
    page = client.get(reverse("fund-set-asides")).content.decode()
    assert "Confirmed this month" in page
    assert f'name="pick_{first.pk}"' not in page and f'name="pick_{second.pk}"' in page


@pytest.mark.django_db
def test_monthly_notice_is_one_alert_per_member_and_month_and_household_members_share_funds():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    shared = make_account(owner, household=household)
    add_balance(shared, 100_000)
    fund = make_fund(owner, shared, household=household)

    created = evaluate_fund_alerts(today=TODAY)
    keys = sorted((alert.recipient_id, alert.dedupe_key) for alert in created)
    assert keys == sorted([(owner.pk, "fund-set-aside:2026-11"), (member.pk, "fund-set-aside:2026-11")])
    assert all(alert.kind == Alert.Kind.FUND and "$" not in alert.title for alert in created)
    assert not Alert.objects.filter(recipient=outsider).exists()
    assert evaluate_fund_alerts(today=date(2026, 11, 20)) == []
    # The member confirms; the owner then sees it as confirmed.
    confirm_set_asides(member, [(fund, 20_000)], today=TODAY)
    assert monthly_set_asides(owner, today=TODAY).pending == []
    december = evaluate_fund_alerts(today=date(2026, 12, 1))
    assert {alert.dedupe_key for alert in december} == {"fund-set-aside:2026-12"}


@pytest.mark.django_db
def test_turning_the_fund_notice_off_stops_it():
    from finance.alert_services import settings_for

    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 100_000)
    make_fund(owner, account)
    prefs = settings_for(owner)
    prefs.fund_enabled = False
    prefs.save()
    assert evaluate_fund_alerts(today=TODAY) == []


# -- draw-down --


def _drawn_fund(*, category=None, item_category=None):
    owner = make_person("owner")
    savings = make_account(owner)
    checking = make_account(owner, name="Synthetic Checking", account_type=Account.Type.CHECKING)
    add_balance(savings, 200_000)
    fund = make_fund(owner, savings, target=120_000)
    item = make_item(owner, amount=120_000, start=date(2026, 12, 10), category=item_category)
    link_item(fund, item, linked_on=date(2026, 11, 1))
    assign_to_fund(owner, fund, 100_000, today=TODAY)
    return owner, savings, checking, fund, item


@pytest.mark.django_db
def test_matching_payment_draws_the_fund_down_once_and_can_be_undone():
    owner, _savings, checking, fund, _item = _drawn_fund()
    txn = make_transaction(owner, checking, on=date(2026, 12, 11), amount_minor=-95_000,
                           description="SYNTHETIC CAR INSURANCE 1234")

    created = apply_fund_draw_downs(today=date(2026, 12, 12))
    assert [(entry.transaction_id, entry.amount_minor, entry.occurrence_date) for entry in created] == [
        (txn.pk, -95_000, date(2026, 12, 10))
    ]
    assert assigned_minor(fund) == 5_000
    assert apply_fund_draw_downs(today=date(2026, 12, 13)) == []
    page = signed_in(owner).get(reverse("savings-goal-fund", args=[fund.pk])).content.decode()
    assert "Drawn down" in page and "SYNTHETIC CAR INSURANCE 1234" in page and "Undo" in page

    undo_draw_down(owner, created[0])
    assert assigned_minor(fund) == 100_000
    # Undone means "not this payment": the same transaction never draws again.
    assert apply_fund_draw_downs(today=date(2026, 12, 14)) == []
    with pytest.raises(ValidationError):
        undo_draw_down(owner, created[0])


@pytest.mark.django_db
def test_draw_down_is_capped_at_what_the_fund_holds():
    owner, _savings, checking, fund, _item = _drawn_fund()
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-125_000,
                     description="SYNTHETIC CAR INSURANCE")
    created = apply_fund_draw_downs(today=date(2026, 12, 12))
    assert [entry.amount_minor for entry in created] == [-100_000]
    assert assigned_minor(fund) == 0


@pytest.mark.django_db
def test_draw_down_ignores_rows_outside_the_amount_date_or_identity_window():
    owner, _savings, checking, fund, _item = _drawn_fund()
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-50_000,
                     description="SYNTHETIC CAR INSURANCE")
    make_transaction(owner, checking, on=date(2026, 12, 25), amount_minor=-120_000,
                     description="SYNTHETIC CAR INSURANCE LATE")
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-120_000,
                     description="SYNTHETIC GROCER")
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=120_000,
                     description="SYNTHETIC CAR INSURANCE REFUND")
    assert apply_fund_draw_downs(today=date(2026, 12, 31)) == []
    assert assigned_minor(fund) == 100_000


@pytest.mark.django_db
def test_draw_down_matches_by_category_and_skips_transfers_and_rows_before_the_link():
    owner = make_person("owner")
    household = make_household(owner)
    category = Category.objects.create(household=household, name="Synthetic Insurance")
    savings = make_account(owner)
    checking = make_account(owner, name="Synthetic Checking", account_type=Account.Type.CHECKING)
    add_balance(savings, 200_000)
    fund = make_fund(owner, savings)
    item = make_item(owner, amount=120_000, start=date(2026, 6, 10), cadence=PlannedItem.Cadence.MONTHLY,
                     category=category)
    link_item(fund, item, linked_on=date(2026, 11, 1))
    assign_to_fund(owner, fund, 150_000, today=TODAY)
    before_link = make_transaction(owner, checking, on=date(2026, 10, 10), amount_minor=-120_000,
                                   description="SYNTHETIC POLICY CO", category=category)
    transfer = make_transaction(owner, checking, on=date(2026, 11, 10), amount_minor=-120_000,
                                description="SYNTHETIC POLICY CO TRANSFER", category=category)
    other_leg = make_transaction(owner, savings, on=date(2026, 11, 10), amount_minor=120_000,
                                 description="SYNTHETIC TRANSFER IN")
    low, high = sorted((transfer, other_leg), key=lambda txn: txn.pk)
    TransferPair.objects.create(
        leg_a=low, leg_b=high, status=TransferPair.Status.CONFIRMED, kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.HIGH, reasons=["synthetic"],
    )
    paid = make_transaction(owner, checking, on=date(2026, 12, 9), amount_minor=-118_000,
                            description="SYNTHETIC POLICY CO", category=category)

    created = apply_fund_draw_downs(today=date(2026, 12, 12))
    assert [entry.transaction_id for entry in created] == [paid.pk]
    assert before_link.pk not in FundEntry.objects.values_list("transaction_id", flat=True)


@pytest.mark.django_db
def test_an_undone_import_stops_its_draw_down_counting_and_a_reimport_draws_again():
    owner, _savings, checking, fund, _item = _drawn_fund()
    txn = make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-90_000,
                           description="SYNTHETIC CAR INSURANCE")
    apply_fund_draw_downs(today=date(2026, 12, 12))
    assert assigned_minor(fund) == 10_000
    Transaction.objects.filter(pk=txn.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())
    assert assigned_minor(fund) == 100_000
    again = make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-90_000,
                             description="SYNTHETIC CAR INSURANCE COPY")
    created = apply_fund_draw_downs(today=date(2026, 12, 12))
    assert [entry.transaction_id for entry in created] == [again.pk]
    assert assigned_minor(fund) == 10_000


@pytest.mark.django_db
def test_a_fund_relinked_to_another_item_draws_for_its_occurrences():
    owner, _savings, checking, fund, item = _drawn_fund()
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-50_000,
                     description="SYNTHETIC CAR INSURANCE")
    # A smaller first payment draws the old item's December occurrence.
    item.amount_minor = 50_000
    item.save()
    assert len(apply_fund_draw_downs(today=date(2026, 12, 12))) == 1
    other = make_item(owner, name="Synthetic Home Insurance", amount=40_000, start=date(2026, 12, 10))
    link_item(fund, other, linked_on=date(2026, 12, 1))
    paid = make_transaction(owner, checking, on=date(2026, 12, 11), amount_minor=-40_000,
                            description="SYNTHETIC HOME INSURANCE")
    created = apply_fund_draw_downs(today=date(2026, 12, 12))
    assert [(entry.transaction_id, entry.planned_item_id) for entry in created] == [(paid.pk, other.pk)]


@pytest.mark.django_db
def test_household_fund_never_draws_on_a_private_transaction():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, household=household)
    member_private = make_account(member, name="Synthetic Member Card", account_type=Account.Type.CHECKING)
    add_balance(shared, 200_000)
    fund = make_fund(owner, shared, household=household)
    item = make_item(owner, household=household, start=date(2026, 12, 10))
    link_item(fund, item, linked_on=date(2026, 11, 1))
    assign_to_fund(owner, fund, 120_000, today=TODAY)
    make_transaction(member, member_private, on=date(2026, 12, 10), amount_minor=-120_000,
                     description="SYNTHETIC CAR INSURANCE")
    assert apply_fund_draw_downs(today=date(2026, 12, 12)) == []
    shared_checking = make_account(owner, name="Synthetic Joint Checking", household=household,
                                   account_type=Account.Type.CHECKING)
    paid = make_transaction(owner, shared_checking, on=date(2026, 12, 11), amount_minor=-120_000,
                            description="SYNTHETIC CAR INSURANCE")
    created = apply_fund_draw_downs(today=date(2026, 12, 12))
    assert [entry.transaction_id for entry in created] == [paid.pk]


@pytest.mark.django_db
def test_daily_pass_runs_draw_downs():
    owner, _savings, checking, fund, _item = _drawn_fund()
    txn = make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-120_000,
                           description="SYNTHETIC CAR INSURANCE")
    run_daily_alert_pass(today=date(2026, 12, 12))
    assert FundEntry.objects.filter(transaction=txn, kind="draw_down").count() == 1


# -- projection --


@pytest.mark.django_db
def test_projection_counts_set_asides_as_outflows_and_the_fund_pays_its_item():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 500_000)
    fund = make_fund(owner, account, target=120_000)
    item = make_item(owner, amount=120_000, start=date(2027, 3, 15))
    link_item(fund, item)
    assign_to_fund(owner, fund, 20_000, today=TODAY)
    report = cash_flow_with_projection(
        owner, date_from=date(2026, 6, 1), date_to=date(2026, 11, 30), grouping="month", today=TODAY, horizon=6,
    )
    months = {month.start: month for month in report.projected_periods}
    # Dec through Mar: (120,000 − 20,000) over 4 months, 25,000 each.
    for start in (date(2026, 12, 1), date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1)):
        set_asides = [c for c in months[start].contributions if c.source == SOURCE_SET_ASIDE]
        assert [(c.source_id, c.amount_minor) for c in set_asides] == [(fund.pk, 25_000)]
    march = months[date(2027, 3, 1)]
    planned = [c for c in march.contributions if c.source == "planned"]
    assert [(c.amount_minor, c.paid_from_fund_minor) for c in planned] == [(0, 120_000)]
    assert march.spending_minor == 25_000
    # After the payment the fund starts again for next year's renewal.
    april = [c for c in months[date(2027, 4, 1)].contributions if c.source == SOURCE_SET_ASIDE]
    assert [c.amount_minor for c in april] == [10_000]
    # The plain projection (funding plan, bills calendar, chat) is unchanged.
    plain = {month.start: month for month in projected_months_for(owner, today=TODAY, horizon=6)}
    assert plain[date(2027, 3, 1)].spending_minor == 120_000
    assert plain[date(2026, 12, 1)].spending_minor == 0
    assert report.projection_has_set_asides
    # The scenario baseline matches, so the comparison shows no phantom change.
    assert [m.net_minor for m in report.scenario_projected_periods] == [m.net_minor for m in report.projected_periods]


@pytest.mark.django_db
def test_projection_leaves_set_asides_out_of_one_account_and_follows_scope():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Synthetic Own Cash")
    shared = make_account(owner, name="Synthetic Shared Cash", household=household)
    make_fund(owner, private, name="Synthetic Private Fund", target_date=date(2027, 1, 31))
    make_fund(owner, shared, name="Synthetic Shared Fund", household=household, target_date=date(2027, 1, 31))

    def set_aside_names(principal, **kwargs):
        report = cash_flow_with_projection(
            principal, date_from=date(2026, 10, 1), date_to=date(2026, 11, 30), grouping="month",
            today=TODAY, horizon=3, **kwargs,
        )
        return {c.name for m in report.projected_periods for c in m.contributions if c.source == SOURCE_SET_ASIDE}

    assert set_aside_names(owner) == {"Synthetic Private Fund", "Synthetic Shared Fund"}
    assert set_aside_names(owner, scope="household") == {"Synthetic Shared Fund"}
    assert set_aside_names(member) == {"Synthetic Shared Fund"}
    assert set_aside_names(owner, account=shared) == set()


@pytest.mark.django_db
def test_cash_flow_page_labels_set_asides():
    owner = make_person("owner")
    account = make_account(owner)
    today = timezone.localdate()
    make_fund(owner, account, name="Synthetic Labelled Fund", target_date=today + timedelta(days=150))
    page = signed_in(owner).get(reverse("home")).content.decode()
    assert "Set aside: Synthetic Labelled Fund" in page
    assert "monthly set-aside" in page


def _month(start, contributions=()):
    from types import SimpleNamespace

    return SimpleNamespace(start=start, contributions=tuple(contributions))


def test_fund_schedule_is_pure_and_stops_once_reached():
    goal = SavingsGoal(pk=1, target_amount_minor=30_000, target_date=date(2027, 1, 31), share_of_account=True)
    months = [_month(date(2026, 12, 1)), _month(date(2027, 1, 1)), _month(date(2027, 2, 1))]
    schedule = fund_schedule([goal], {1: 0}, months)
    assert [[amount for _goal, amount in schedule[m.start].set_asides] for m in months] == [[15_000], [15_000], []]


def test_fund_schedule_pays_what_the_scenario_spends_on_the_item():
    from types import SimpleNamespace

    item = PlannedItem(pk=7, owner_id=1, scope="private", kind="expense", amount_minor=120_000,
                       start_date=date(2026, 12, 10), cadence="annual", enabled=True)
    goal = SavingsGoal(pk=1, owner_id=1, scope="private", target_amount_minor=120_000, share_of_account=True)
    goal.funds_planned_item = item
    # The scenario halves December's payment: the fund pays $600 and keeps $600.
    payment = SimpleNamespace(source="planned", source_id=7, kind="expense", amount_minor=60_000)
    months = [_month(date(2026, 12, 1), [payment]), _month(date(2027, 1, 1))]
    schedule = fund_schedule([goal], {1: 120_000}, months)
    assert schedule[date(2026, 12, 1)].covered == {7: 60_000}
    # January asks for the rest of next December's renewal: (120,000 - 60,000) over 12 months.
    assert [amount for _goal, amount in schedule[date(2027, 1, 1)].set_asides] == [5_000]


@pytest.mark.django_db
def test_projection_expects_this_months_payment_before_carrying_the_fund_forward():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 500_000)
    fund = make_fund(owner, account, target=120_000)
    item = make_item(owner, amount=120_000, start=date(2026, 11, 20))
    link_item(fund, item)
    assign_to_fund(owner, fund, 120_000, today=TODAY)
    report = cash_flow_with_projection(
        owner, date_from=date(2026, 6, 1), date_to=date(2026, 11, 30), grouping="month", today=TODAY, horizon=3,
    )
    december = report.projected_periods[0]
    # November 20's payment will empty the fund, so December starts saving for next November.
    set_asides = [c.amount_minor for c in december.contributions if c.source == SOURCE_SET_ASIDE]
    assert set_asides == [10_000]
    # Once that payment has drawn the fund down, it is not expected twice.
    paid = make_transaction(owner, make_account(owner, name="Synthetic Checking", account_type=Account.Type.CHECKING),
                            on=date(2026, 11, 20), amount_minor=-120_000, description="SYNTHETIC CAR INSURANCE")
    apply_fund_draw_downs(today=date(2026, 11, 21))
    assert FundEntry.objects.filter(transaction=paid).exists()
    report = cash_flow_with_projection(
        owner, date_from=date(2026, 6, 1), date_to=date(2026, 11, 30), grouping="month", today=TODAY, horizon=3,
    )
    set_asides = [c.amount_minor for c in report.projected_periods[0].contributions if c.source == SOURCE_SET_ASIDE]
    assert set_asides == [10_000]


# -- visibility --


@pytest.mark.django_db
def test_another_member_never_sees_a_private_fund():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Synthetic Owner Vault")
    add_balance(private, 900_000, on=timezone.localdate())
    shared = make_account(owner, name="Synthetic Joint Cash", household=household)
    add_balance(shared, 100_000, on=timezone.localdate())
    secret = make_fund(owner, private, name="Synthetic Secret Fund",
                       target_date=timezone.localdate() + timedelta(days=90))
    assign_to_fund(owner, secret, 123_456, today=timezone.localdate())
    joint = make_fund(owner, shared, name="Synthetic Joint Fund", household=household,
                      target_date=timezone.localdate() + timedelta(days=90))
    assign_to_fund(owner, joint, 10_000, today=timezone.localdate())
    evaluate_fund_alerts(today=timezone.localdate())
    client = signed_in(member)
    for url in (
        reverse("savings-goals"),
        reverse("fund-set-asides"),
        reverse("account-balances", args=[shared.pk]),
        reverse("home"),
        reverse("alert-list"),
    ):
        page = client.get(url).content.decode()
        assert "Synthetic Secret Fund" not in page, url
        assert "1,234.56" not in page, url
        assert "Owner Vault" not in page, url
    assert "Synthetic Joint Fund" in client.get(reverse("savings-goals")).content.decode()
    for url in (
        reverse("savings-goal-fund", args=[secret.pk]),
        reverse("fund-shortfall", args=[private.pk]),
    ):
        assert client.get(url).status_code == 404
    assert client.post(reverse("savings-goal-fund", args=[secret.pk]),
                       {"action": "release", "amount": "1"}).status_code == 404
    with pytest.raises(PermissionDenied):
        assign_to_fund(member, secret, 1, today=timezone.localdate())
    assert assigned_minor(secret) == 123_456
    # The joint account's unassigned money counts only household funds.
    assert account_position(shared, today=timezone.localdate()).unassigned_minor == 90_000


@pytest.mark.django_db
def test_a_former_member_loses_the_household_funds():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, household=household)
    add_balance(shared, 100_000, on=timezone.localdate())
    joint = make_fund(owner, shared, name="Synthetic Joint Fund", household=household)
    Membership.objects.filter(person=member).update(ended_at=timezone.now())
    assert signed_in(member).get(reverse("savings-goal-fund", args=[joint.pk])).status_code == 404
    assert monthly_set_asides(member, today=TODAY).rows == []


# -- existing goals and migration --


@pytest.mark.django_db
def test_whole_account_and_manual_goals_are_unchanged():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 70_000)
    whole = SavingsGoal.objects.create(
        owner=owner, name="Synthetic Whole", target_amount_minor=100_000, target_date=date(2027, 2, 28),
        linked_account=account,
    )
    manual = SavingsGoal.objects.create(
        owner=owner, name="Synthetic Manual", target_amount_minor=100_000, target_date=date(2027, 2, 28),
        manual_amount_minor=25_000, manual_amount_date=TODAY,
    )
    # A fund in the same account does not change the whole-account goal.
    fund = make_fund(owner, account, name="Synthetic Fund")
    assign_to_fund(owner, fund, 30_000, today=TODAY)
    whole_card = goal_progress(owner, whole, today=TODAY)
    manual_card = goal_progress(owner, manual, today=TODAY)
    assert (whole_card.current_amount_minor, whole_card.source, whole_card.monthly_needed_minor) == (
        70_000, SOURCE_SNAPSHOT, 7_500
    )
    assert (manual_card.current_amount_minor, manual_card.source, manual_card.monthly_needed_minor) == (
        25_000, SOURCE_MANUAL, 18_750
    )
    assert whole_card.due_date == whole.target_date and not whole_card.share_of_account
    assert not whole.share_of_account and whole.funds_planned_item_id is None
    # Editing a whole-account goal through the old payload keeps it whole-account.
    saved = save_savings_goal(owner, {"scope": "private", "name": "Synthetic Whole", "target_amount_minor": 90_000},
                              goal=whole)
    assert not saved.share_of_account and saved.linked_account_id == account.pk


@pytest.mark.django_db
def test_fund_detail_requires_a_share_goal():
    owner = make_person("owner")
    goal = SavingsGoal.objects.create(owner=owner, name="Synthetic Plain", target_amount_minor=1_000)
    assert signed_in(owner).get(reverse("savings-goal-fund", args=[goal.pk])).status_code == 404


@pytest.mark.django_db
def test_deleting_a_fund_account_keeps_the_fund_and_its_money():
    from finance.lifecycle_services import delete_account

    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 50_000)
    fund = make_fund(owner, account)
    assign_to_fund(owner, fund, 20_000, today=TODAY)
    delete_account(owner, account.pk)
    fund.refresh_from_db()
    assert fund.linked_account_id is None and fund.share_of_account
    assert assigned_minor(fund) == 20_000
    with pytest.raises(ValidationError):
        assign_to_fund(owner, fund, 1, today=TODAY)


@pytest.mark.django_db(transaction=True)
def test_migration_keeps_existing_goals_whole_account_or_manual():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("finance", "0086_ai_proposal_balance_cards")]
    after = [("finance", "0087_sinking_funds")]
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        user = old.get_model("auth", "User").objects.create(username="synthetic-migrate")
        person = old.get_model("finance", "Person").objects.create(user=user, display_name="Synthetic")
        account = old.get_model("finance", "Account").objects.create(
            name="Synthetic Savings", account_type="savings", owner=person, scope="private"
        )
        goal_model = old.get_model("finance", "SavingsGoal")
        linked = goal_model.objects.create(owner=person, name="Synthetic Linked", target_amount_minor=10_000,
                                           linked_account=account)
        manual = goal_model.objects.create(owner=person, name="Synthetic Manual", target_amount_minor=10_000,
                                           manual_amount_minor=2_500, manual_amount_date=date(2026, 1, 1))
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new = executor.loader.project_state(after).apps
        rows = new.get_model("finance", "SavingsGoal").objects.order_by("pk").values_list(
            "pk", "share_of_account", "funds_planned_item_id", "linked_account_id", "manual_amount_minor"
        )
        assert list(rows) == [
            (linked.pk, False, None, account.pk, None),
            (manual.pk, False, None, None, 2_500),
        ]
        assert new.get_model("finance", "FundEntry").objects.count() == 0
        assert new.get_model("finance", "AlertSettings")._meta.get_field("fund_enabled").default is True
    finally:
        MigrationExecutor(connection).migrate(head)


def test_money_parsing_of_fund_forms_uses_money_field():
    from finance.forms import FundMoveForm

    form = FundMoveForm({"action": "assign", "amount": "$1,000.25"})
    assert form.is_valid() and form.amount_minor() == 100_025
    assert Decimal("0.01") == FundMoveForm.base_fields["amount"].min_value


@pytest.mark.django_db(transaction=True)
def test_moving_a_fund_waits_for_an_assignment_in_flight_and_then_refuses():
    """An edit that moves a fund checks its money under the assignment's locks (PostgreSQL only)."""
    import threading
    from unittest.mock import patch

    from django.db import connection, connections

    from finance import fund_services
    from tests.lock_races import JOIN_SECONDS, wait_until_blocked_by

    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL")
    owner = make_person("owner")
    first = make_account(owner, name="Synthetic First Cash")
    second = make_account(owner, name="Synthetic Second Cash")
    add_balance(first, 100_000)
    add_balance(second, 100_000)
    fund = make_fund(owner, first)
    stale = SavingsGoal.objects.get(pk=fund.pk)
    locked, release = threading.Event(), threading.Event()
    done = {"assign": threading.Event(), "move": threading.Event()}
    pids, errors, outcome = {}, [], {}
    original_check = fund_services._check_room

    def pausing_check(position, amount_minor):
        original_check(position, amount_minor)
        if threading.current_thread().name == "assign":
            locked.set()
            release.wait(JOIN_SECONDS)

    def run(name, action):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[name] = cursor.fetchone()[0]
            outcome[name] = action()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            outcome[name] = exc
        finally:
            connections.close_all()
            done[name].set()

    payload = {"scope": "private", "name": fund.name, "share_of_account": True, "linked_account": second,
               "target_amount_minor": fund.target_amount_minor}
    with patch.object(fund_services, "_check_room", pausing_check):
        assign = threading.Thread(
            target=run, args=("assign", lambda: assign_to_fund(owner, fund, 5_000, today=TODAY)), name="assign",
            daemon=True,
        )
        assign.start()
        assert locked.wait(JOIN_SECONDS), outcome
        move = threading.Thread(
            target=run, args=("move", lambda: save_savings_goal(owner, payload, goal=stale)), name="move", daemon=True
        )
        move.start()
        try:
            while "move" not in pids and not done["move"].is_set():
                done["move"].wait(0.02)
            wait_until_blocked_by(pids.get("move"), pids["assign"], done["move"], errors)
        finally:
            release.set()
            assign.join(JOIN_SECONDS)
            move.join(JOIN_SECONDS)
    assert isinstance(outcome["assign"], FundEntry), outcome
    assert isinstance(outcome["move"], ValidationError) and FUND_MOVE_ERROR in outcome["move"].messages, outcome
    fund.refresh_from_db()
    assert fund.linked_account_id == first.pk
    assert account_position(first, today=TODAY).assigned_minor == 5_000


# -- second review round: far-off items, early and unfunded payments, pre-link occurrences --


@pytest.mark.django_db
def test_linking_a_one_time_expense_years_away_still_takes_its_date():
    owner = make_person("owner")
    account = make_account(owner)
    far = timezone.localdate() + timedelta(days=730)
    item = make_item(owner, amount=500_000, start=far, cadence=PlannedItem.Cadence.ONE_TIME)
    goal = save_savings_goal(owner, {"scope": "private", "name": "Synthetic Roof Fund", "share_of_account": True,
                                     "linked_account": account, "funds_planned_item": item,
                                     "target_amount_minor": None, "target_date": None})
    assert goal.target_date == far
    assert goal_progress(owner, goal, today=timezone.localdate()).monthly_needed_minor is not None


@pytest.mark.django_db
def test_an_early_payment_rolls_the_due_date_to_the_next_occurrence():
    owner, _savings, checking, fund, _item = _drawn_fund()
    # Due December 10; paid December 8, inside the tolerance.
    make_transaction(owner, checking, on=date(2026, 12, 8), amount_minor=-100_000,
                     description="SYNTHETIC CAR INSURANCE")
    assert len(apply_fund_draw_downs(today=date(2026, 12, 8))) == 1
    assert fund_due_date(fund, date(2026, 12, 9)) == date(2027, 12, 10)
    row = next(row for row in monthly_set_asides(owner, today=date(2026, 12, 9)).rows if row.goal.pk == fund.pk)
    assert row.suggested_minor == suggested_set_aside(120_000, 0, date(2027, 12, 10), on=date(2026, 12, 9))
    assert row.suggested_minor < 120_000


@pytest.mark.django_db
def test_a_payment_that_finds_the_fund_empty_never_takes_money_assigned_later():
    owner = make_person("owner")
    savings = make_account(owner)
    checking = make_account(owner, name="Synthetic Checking", account_type=Account.Type.CHECKING)
    add_balance(savings, 200_000)
    fund = make_fund(owner, savings)
    item = make_item(owner, amount=120_000, start=date(2026, 11, 10))
    link_item(fund, item, linked_on=date(2026, 11, 1))
    paid = make_transaction(owner, checking, on=date(2026, 11, 10), amount_minor=-120_000,
                            description="SYNTHETIC CAR INSURANCE")
    created = apply_fund_draw_downs(today=date(2026, 11, 11))
    assert [(entry.transaction_id, entry.amount_minor) for entry in created] == [(paid.pk, 0)]
    assign_to_fund(owner, fund, 50_000, today=date(2026, 11, 12))
    assert apply_fund_draw_downs(today=date(2026, 11, 13)) == []
    assert assigned_minor(fund) == 50_000
    page = signed_in(owner).get(reverse("savings-goal-fund", args=[fund.pk])).content.decode()
    assert "Undo" not in page


@pytest.mark.django_db
def test_projection_does_not_expect_an_occurrence_from_before_the_link():
    owner = make_person("owner")
    account = make_account(owner)
    add_balance(account, 500_000)
    fund = make_fund(owner, account, target=120_000)
    item = make_item(owner, amount=120_000, start=date(2026, 11, 1))
    # Linked and fully funded on November 20, after this year's November 1 payment.
    link_item(fund, item, linked_on=date(2026, 11, 20))
    assign_to_fund(owner, fund, 120_000, today=date(2026, 11, 20))
    report = cash_flow_with_projection(
        owner, date_from=date(2026, 6, 1), date_to=date(2026, 11, 30), grouping="month",
        today=date(2026, 11, 20), horizon=3,
    )
    set_asides = [c for m in report.projected_periods for c in m.contributions if c.source == SOURCE_SET_ASIDE]
    assert set_asides == []


# -- third review round --


@pytest.mark.django_db
def test_undoing_a_draw_down_cannot_put_back_more_than_the_unassigned_balance():
    owner = make_person("owner")
    savings = make_account(owner)
    checking = make_account(owner, name="Synthetic Checking", account_type=Account.Type.CHECKING)
    add_balance(savings, 100_000)
    first = make_fund(owner, savings, name="Synthetic First")
    second = make_fund(owner, savings, name="Synthetic Second")
    item = make_item(owner, amount=100_000, start=date(2026, 12, 10))
    link_item(first, item, linked_on=date(2026, 11, 1))
    assign_to_fund(owner, first, 100_000, today=TODAY)
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-100_000,
                     description="SYNTHETIC CAR INSURANCE")
    [drawn] = apply_fund_draw_downs(today=date(2026, 12, 11))
    # The payment came from checking, so the savings money is unassigned again; B takes it.
    assign_to_fund(owner, second, 100_000, today=TODAY)
    with pytest.raises(ValidationError, match="Release money from another fund first"):
        undo_draw_down(owner, drawn)
    assert account_position(savings, today=TODAY).shortfall_minor == 0
    release_from_fund(owner, second, 100_000, today=TODAY)
    undo_draw_down(owner, drawn)
    assert assigned_minor(first) == 100_000


@pytest.mark.django_db
def test_deleting_the_account_of_an_undone_import_keeps_its_draw_down_uncounted():
    from finance.lifecycle_services import delete_account

    owner, savings, checking, fund, _item = _drawn_fund()
    txn = make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-100_000,
                           description="SYNTHETIC CAR INSURANCE")
    apply_fund_draw_downs(today=date(2026, 12, 11))
    Transaction.objects.filter(pk=txn.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())
    release_from_fund(owner, fund, 100_000, today=TODAY)
    assert assigned_minor(fund) == 0
    delete_account(owner, checking.pk)
    entry = FundEntry.objects.get(kind="draw_down", goal=fund)
    assert entry.transaction_id is None and entry.undone_at is not None
    assert assigned_minor(fund) == 0


@pytest.mark.django_db
def test_deleting_a_payments_account_keeps_a_counted_draw_down():
    from finance.lifecycle_services import delete_account

    owner, savings, checking, fund, _item = _drawn_fund()
    make_transaction(owner, checking, on=date(2026, 12, 10), amount_minor=-60_000,
                     description="SYNTHETIC CAR INSURANCE")
    item = fund.funds_planned_item
    item.amount_minor = 60_000
    item.save()
    apply_fund_draw_downs(today=date(2026, 12, 11))
    delete_account(owner, checking.pk)
    entry = FundEntry.objects.get(kind="draw_down", goal=fund)
    assert entry.transaction_id is None and entry.undone_at is None
    assert assigned_minor(fund) == 40_000


def test_the_fund_does_not_pay_an_occurrence_it_already_paid_early():
    from types import SimpleNamespace

    item = PlannedItem(pk=7, owner_id=1, scope="private", kind="expense", amount_minor=120_000,
                       start_date=date(2026, 12, 1), cadence="annual", enabled=True)
    goal = SavingsGoal(pk=1, owner_id=1, scope="private", target_amount_minor=120_000, share_of_account=True)
    goal.funds_planned_item = item
    # December 1's payment drew the fund down in November; the projection still lists it.
    payment = SimpleNamespace(source="planned", source_id=7, kind="expense", amount_minor=120_000)
    months = [_month(date(2026, 12, 1), [payment]), _month(date(2027, 1, 1))]
    schedule = fund_schedule([goal], {1: 0}, months, {1: {date(2026, 12, 1)}})
    december = schedule[date(2026, 12, 1)]
    assert december.covered == {}
    # Saving starts for next December and is not spent on the paid occurrence.
    first = suggested_set_aside(120_000, 0, date(2027, 12, 1), on=date(2026, 12, 1))
    assert [amount for _goal, amount in december.set_asides] == [first]
    january = [amount for _goal, amount in schedule[date(2027, 1, 1)].set_asides]
    assert january == [suggested_set_aside(120_000, first, date(2027, 12, 1), on=date(2027, 1, 1))]


@pytest.mark.django_db
def test_linking_an_annual_expense_that_starts_years_away_takes_its_first_date():
    owner = make_person("owner")
    account = make_account(owner)
    far = timezone.localdate() + timedelta(days=760)
    item = make_item(owner, amount=240_000, start=far)
    goal = save_savings_goal(owner, {"scope": "private", "name": "Synthetic Far Fund", "share_of_account": True,
                                     "linked_account": account, "funds_planned_item": item,
                                     "target_amount_minor": None, "target_date": None})
    assert goal.target_date == far
    assert goal_progress(owner, goal, today=timezone.localdate()).monthly_needed_minor is not None


@pytest.mark.django_db
def test_a_paid_one_time_item_asks_for_nothing_more():
    owner, _savings, checking, fund, item = _drawn_fund()
    item.cadence = PlannedItem.Cadence.ONE_TIME
    item.save()
    make_transaction(owner, checking, on=date(2026, 12, 9), amount_minor=-100_000,
                     description="SYNTHETIC CAR INSURANCE")
    assert len(apply_fund_draw_downs(today=date(2026, 12, 9))) == 1
    fund.refresh_from_db()
    assert fund_due_date(fund, date(2026, 12, 9)) is None
    row = next(row for row in monthly_set_asides(owner, today=date(2026, 12, 9)).rows if row.goal.pk == fund.pk)
    assert row.suggested_minor is None


@pytest.mark.django_db
def test_fund_writes_lock_the_goal_before_the_account():
    """Goal, then account: the order a chat edit (which holds the goal) takes, so none deadlock."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    if connection.vendor != "postgresql":
        pytest.skip("SQLite emits no FOR UPDATE")
    owner = make_person("owner")
    account = make_account(owner)
    other = make_account(owner, name="Synthetic Other Cash")
    add_balance(account, 100_000)
    fund = make_fund(owner, account)

    def lock_order(action):
        with CaptureQueriesContext(connection) as queries:
            action()
        locked = {'FROM "finance_savingsgoal"': "goal", 'FROM "finance_account"': "account"}
        return [
            name
            for q in queries if "FOR UPDATE" in q["sql"]
            for prefix, name in locked.items() if prefix in q["sql"]
        ]

    assert lock_order(lambda: assign_to_fund(owner, fund, 1_000, today=TODAY))[:2] == ["goal", "account"]
    assert lock_order(lambda: confirm_set_asides(owner, [(fund, 1_000)], today=TODAY))[:2] == ["goal", "account"]
    release_from_fund(owner, fund, 2_000, today=TODAY)
    payload = {"scope": "private", "name": fund.name, "share_of_account": True, "linked_account": other,
               "target_amount_minor": fund.target_amount_minor}
    assert lock_order(lambda: save_savings_goal(owner, payload, goal=fund))[:2] == ["goal", "account"]
