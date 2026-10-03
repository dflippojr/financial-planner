from datetime import date, datetime, timedelta, timezone as dt_timezone
from hashlib import sha256
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.budget_services import save_budget
from finance.cash_flow import cash_flow_report, spending_by_category_report
from finance.category_services import assign_category, ensure_household_categories, income_and_spending_totals
from finance.models import (
    Account,
    Alert,
    AlertSettings,
    BalanceSnapshot,
    Budget,
    Household,
    ImportBatch,
    Membership,
    MonthlyReview,
    Person,
    RecurringSeries,
    RecurringSeriesMember,
    SavingsGoal,
    Transaction,
)
from finance.monthly_review import (
    compute_monthly_review_facts,
    generate_due_monthly_reviews,
    latest_closed_month,
    store_monthly_review,
    visibility_key,
)
from finance.net_worth import net_worth_report
from finance.recurring_services import cancel_recurring_series, confirm_recurring_series
from finance.savings_goal_services import goal_progress


PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 10, 3)
SEP = date(2026, 9, 1)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(
    owner,
    *,
    name="Synthetic Checking",
    account_type=Account.Type.CHECKING,
    scope=Account.Scope.PRIVATE,
    household=None,
):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 9, 15),
    amount_minor=-1000,
    description="Synthetic row",
    range_start=None,
    range_end=None,
):
    start = range_start or date(transaction_date.year, transaction_date.month, 1)
    end = range_end or date(transaction_date.year, transaction_date.month, 28)
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=sha256(
            f"{account.pk}-{transaction_date}-{amount_minor}-{description}".encode()
        ).hexdigest(),
        date_range_start=start,
        date_range_end=end,
    )
    digest = sha256(f"txn-{account.pk}-{amount_minor}-{transaction_date}-{description}".encode()).hexdigest()
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_facts_reconcile_with_cash_flow_spending_budgets_and_net_worth():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    groc = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 8), amount_minor=-40_000, description="Synthetic groceries"
    )
    dine = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 9), amount_minor=-10_000, description="Synthetic dining"
    )
    make_transaction(owner, checking, transaction_date=date(2026, 9, 5), amount_minor=200_000, description="Synthetic pay")
    assign_category(owner, groc.pk, groceries.pk)
    assign_category(owner, dine.pk, dining.pk)
    prior = make_transaction(
        owner, checking, transaction_date=date(2026, 8, 8), amount_minor=-5_000, description="Synthetic groceries prior"
    )
    assign_category(owner, prior.pk, groceries.pk)
    make_transaction(
        owner, checking, transaction_date=date(2025, 9, 8), amount_minor=50_000, description="Synthetic pay last year"
    )
    save_budget(
        owner.user,
        {
            "scope": Budget.Scope.PRIVATE,
            "category": groceries,
            "amount_minor": 20_000,
            "effective_month": SEP,
        },
    )
    save_budget(
        owner.user,
        {
            "scope": Budget.Scope.PRIVATE,
            "category": dining,
            "amount_minor": 50_000,
            "effective_month": SEP,
        },
    )
    BalanceSnapshot.objects.create(
        account=checking,
        snapshot_date=date(2026, 8, 31),
        amount_minor=100_000,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    BalanceSnapshot.objects.create(
        account=checking,
        snapshot_date=date(2026, 9, 30),
        amount_minor=250_000,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    SavingsGoal.objects.create(
        owner=owner,
        name="Synthetic vacation",
        target_amount_minor=500_000,
        target_date=date(2026, 12, 31),
        manual_amount_minor=100_000,
        manual_amount_date=date(2026, 9, 30),
    )

    facts = compute_monthly_review_facts(owner, SEP, today=TODAY)
    totals = income_and_spending_totals(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    cash = cash_flow_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), today=TODAY)
    spending = spending_by_category_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    worth = net_worth_report(owner, date_from=date(2026, 8, 1), date_to=date(2026, 9, 30), today=date(2026, 9, 30))
    goal = SavingsGoal.objects.get(name="Synthetic vacation")
    progress = goal_progress(owner, goal, today=date(2026, 9, 30))

    assert facts["income_minor"] == totals.income_minor == cash.periods[0].income_minor
    assert facts["spending_minor"] == totals.spending_minor == cash.periods[0].spending_minor
    assert facts["net_minor"] == totals.net_minor
    groc_row = next(row for row in spending.rows if row.name == "Groceries")
    assert facts["category_increases"][0]["name"] == "Groceries"
    assert facts["category_increases"][0]["current_minor"] == groc_row.spending_minor
    assert facts["budgets_over"][0]["name"] == "Groceries"
    assert facts["largest_remaining"]["name"] == "Dining"
    assert facts["net_worth_minor"] == worth.periods[-1].net_minor
    assert facts["net_worth_change_minor"] == worth.periods[-1].net_minor - worth.periods[0].net_minor
    assert facts["savings_goals"][0]["percent"] == progress.percent
    assert facts["month_label"] == "September 2026"


@pytest.mark.django_db
def test_private_facts_never_appear_for_another_member():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    secret = make_transaction(
        owner, private, transaction_date=date(2026, 9, 4), amount_minor=-88_888, description="Secret private spend"
    )
    shared_tx = make_transaction(
        owner, shared, transaction_date=date(2026, 9, 4), amount_minor=-3_000, description="Shared groceries"
    )
    groceries = household.categories.get(name="Groceries")
    assign_category(owner, secret.pk, groceries.pk)
    assign_category(owner, shared_tx.pk, groceries.pk)

    owner_facts = compute_monthly_review_facts(owner, SEP, today=TODAY)
    member_facts = compute_monthly_review_facts(member, SEP, today=TODAY)

    assert owner_facts["spending_minor"] == 91_888
    assert member_facts["spending_minor"] == 3_000
    assert "Secret private spend" not in str(member_facts)
    page = signed_in(member).get(reverse("monthly-review") + "?month=2026-09")
    body = page.content.decode()
    assert "Secret private spend" not in body
    assert "888.88" not in body


@pytest.mark.django_db
def test_scheduled_generation_raises_one_alert_per_member():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    generate_due_monthly_reviews(today=TODAY)
    generate_due_monthly_reviews(today=TODAY)
    alerts = list(Alert.objects.filter(kind=Alert.Kind.MONTHLY_REVIEW).order_by("recipient_id"))
    assert len(alerts) == 2
    assert {row.recipient_id for row in alerts} == {owner.pk, member.pk}
    assert {row.dedupe_key for row in alerts} == {"review:2026-09"}
    assert alerts[0].title == "Your September review is ready"
    assert MonthlyReview.objects.filter(month=SEP).count() == 2


@pytest.mark.django_db
def test_daily_pass_generates_latest_closed_month():
    owner = make_person("owner")
    make_household(owner)
    run_daily_alert_pass(today=TODAY)
    assert MonthlyReview.objects.filter(person=owner, month=SEP).exists()
    assert Alert.objects.filter(recipient=owner, dedupe_key="review:2026-09").exists()


@pytest.mark.django_db
def test_regenerate_rebuilds_facts_and_page():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    make_transaction(owner, checking, transaction_date=date(2026, 9, 12), amount_minor=-2_000, description="First spend")
    review, wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert wrote
    assert review.facts["spending_minor"] == 2_000
    make_transaction(owner, checking, transaction_date=date(2026, 9, 13), amount_minor=-7_000, description="Second spend")
    client = signed_in(owner)
    page = client.get(reverse("monthly-review") + "?month=2026-09")
    assert page.status_code == 200
    response = client.post(reverse("monthly-review-regenerate"), {"month": "2026-09"})
    assert response.status_code == 302
    review.refresh_from_db()
    assert review.facts["spending_minor"] == 9_000
    assert "Monthly review" in client.get(reverse("home")).content.decode()


@pytest.mark.django_db
def test_stale_visibility_regenerates_instead_of_showing_private_data():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(owner, shared, transaction_date=date(2026, 9, 4), amount_minor=-4_000, description="Shared spend")
    store_monthly_review(member, SEP, today=TODAY)
    shared.scope = Account.Scope.PRIVATE
    shared.household = None
    shared.share_mode = ""
    shared.save()
    review, wrote = store_monthly_review(member, SEP, today=TODAY)
    assert wrote
    assert review.facts["spending_minor"] == 0
    assert review.visibility_key == visibility_key(member)


@pytest.mark.django_db
def test_large_transactions_use_threshold_or_top_three():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    make_transaction(owner, checking, transaction_date=date(2026, 9, 1), amount_minor=-500, description="Small")
    make_transaction(owner, checking, transaction_date=date(2026, 9, 2), amount_minor=-8_000, description="Mid")
    make_transaction(owner, checking, transaction_date=date(2026, 9, 3), amount_minor=-9_000, description="Big")
    make_transaction(owner, checking, transaction_date=date(2026, 9, 4), amount_minor=-10_000, description="Biggest")
    none = compute_monthly_review_facts(owner, SEP, today=TODAY)
    names = [item["description"] for item in none["large_transactions"]]
    assert names == ["Biggest", "Big", "Mid"]
    AlertSettings.objects.update_or_create(person=owner, defaults={"large_transaction_minor": 9_500})
    filtered = compute_monthly_review_facts(owner, SEP, today=TODAY)
    assert [item["description"] for item in filtered["large_transactions"]] == ["Biggest"]


@pytest.mark.django_db
def test_recurring_facts_include_price_missed_cancel_and_new():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    flagged = RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic rise",
        display_name="Synthetic Rise",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1000,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=sha256(b"rise").hexdigest(),
        confirmed_at=timezone.now() - timedelta(days=40),
    )
    for day, amount in ((date(2026, 7, 10), -1000), (date(2026, 8, 10), -1000), (date(2026, 9, 10), -1100)):
        RecurringSeriesMember.objects.create(
            series=flagged,
            transaction=make_transaction(
                owner, account, transaction_date=day, amount_minor=amount, description="Synthetic Rise"
            ),
        )
    missed = RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic missed",
        display_name="Synthetic Missed",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1000,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=sha256(b"missed").hexdigest(),
        confirmed_at=timezone.now() - timedelta(days=40),
    )
    RecurringSeriesMember.objects.create(
        series=missed,
        transaction=make_transaction(
            owner,
            account,
            transaction_date=date(2026, 8, 15),
            description="Synthetic Missed",
            range_start=date(2026, 8, 1),
            range_end=date(2026, 8, 31),
        ),
    )
    cancelled = RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic cancel",
        display_name="Synthetic Cancel",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1000,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=sha256(b"cancel").hexdigest(),
        confirmed_at=timezone.now() - timedelta(days=40),
    )
    RecurringSeriesMember.objects.create(
        series=cancelled,
        transaction=make_transaction(
            owner, account, transaction_date=date(2026, 8, 1), description="Synthetic Cancel"
        ),
    )
    cancel_recurring_series(owner, cancelled.pk)
    RecurringSeries.objects.filter(pk=cancelled.pk).update(
        cancelled_at=datetime(2026, 9, 20, tzinfo=dt_timezone.utc)
    )
    suggestion = RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic new",
        display_name="Synthetic New",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-2000,
        status=RecurringSeries.Status.SUGGESTED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=sha256(b"new").hexdigest(),
    )
    RecurringSeriesMember.objects.create(
        series=suggestion,
        transaction=make_transaction(
            owner, account, transaction_date=date(2026, 7, 2), amount_minor=-2000, description="Synthetic New"
        ),
    )
    september = datetime(2026, 9, 12, 12, 0, tzinfo=dt_timezone.utc)
    with patch("django.utils.timezone.now", return_value=september):
        confirm_recurring_series(owner, suggestion.pk)

    facts = compute_monthly_review_facts(owner, SEP, today=TODAY)
    assert facts["price_changes"][0]["name"] == "Synthetic Rise"
    assert facts["missed_charges"][0]["name"] == "Synthetic Missed"
    assert facts["cancellations"][0]["name"] == "Synthetic Cancel"
    assert facts["new_recurring"][0]["name"] == "Synthetic New"


@pytest.mark.django_db
def test_management_command_and_nav():
    owner = make_person("owner")
    make_household(owner)
    assert latest_closed_month(TODAY) == SEP
    call_command("generate_monthly_reviews")
    page = signed_in(owner).get(reverse("monthly-review"))
    assert page.status_code == 200
    assert "Monthly review" in page.content.decode()
    assert "Computed facts" in page.content.decode()


@pytest.mark.django_db
def test_largest_increases_exclude_categories_that_fell():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    transport = household.categories.get(name="Transportation")
    groc = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 8), amount_minor=-4_000, description="Synthetic groceries"
    )
    dine = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 9), amount_minor=-1_000, description="Synthetic dining"
    )
    ride = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 10), amount_minor=-2_500, description="Synthetic transit"
    )
    prior_dine = make_transaction(
        owner, checking, transaction_date=date(2026, 8, 9), amount_minor=-2_000, description="Synthetic dining prior"
    )
    prior_ride = make_transaction(
        owner, checking, transaction_date=date(2026, 8, 10), amount_minor=-5_000, description="Synthetic transit prior"
    )
    assign_category(owner, groc.pk, groceries.pk)
    assign_category(owner, dine.pk, dining.pk)
    assign_category(owner, ride.pk, transport.pk)
    assign_category(owner, prior_dine.pk, dining.pk)
    assign_category(owner, prior_ride.pk, transport.pk)

    facts = compute_monthly_review_facts(owner, SEP, today=TODAY)

    increase_names = [item["name"] for item in facts["category_increases"]]
    decrease_names = [item["name"] for item in facts["category_decreases"]]
    assert increase_names == ["Groceries"]
    assert facts["category_increases"][0]["delta_minor"] == 4_000
    assert "Dining" not in increase_names
    assert "Transportation" not in increase_names
    assert decrease_names == ["Transportation", "Dining"]


@pytest.mark.django_db
def test_goal_progress_uses_snapshot_on_or_before_closed_month_end():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    BalanceSnapshot.objects.create(
        account=checking,
        snapshot_date=date(2026, 9, 30),
        amount_minor=200_000,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    BalanceSnapshot.objects.create(
        account=checking,
        snapshot_date=date(2026, 10, 2),
        amount_minor=500_000,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    SavingsGoal.objects.create(
        owner=owner,
        name="Synthetic emergency",
        target_amount_minor=1_000_000,
        target_date=date(2026, 12, 31),
        linked_account=checking,
        manual_amount_minor=500_000,
        manual_amount_date=date(2026, 10, 3),
    )

    facts = compute_monthly_review_facts(owner, SEP, today=TODAY)

    assert facts["savings_goals"][0]["name"] == "Synthetic emergency"
    assert facts["savings_goals"][0]["current_display"] == "2,000.00 USD"
    assert facts["savings_goals"][0]["percent"] == 20
