import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.debt_planner_services import build_debt_plan
from finance.lifecycle_services import update_debt_terms
from finance.models import Account, BalanceSnapshot, Household, Membership, Person
from tests.page_payload import json_script_payload


PASSWORD = "Synthetic-passphrase-42!"
SECRET_CARD = "Owner Secret Card"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, *, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def add_snapshot(account, snapshot_date, amount_minor, source=BalanceSnapshot.Source.MANUAL):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=source,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def _ready_card(owner, name, *, balance_minor=10_000, apr="12.000", minimum="50.00"):
    account = make_account(owner, name=name)
    add_snapshot(account, timezone.localdate(), balance_minor)
    update_debt_terms(
        owner.user,
        account.pk,
        apr_percent=Decimal(apr),
        minimum_payment_minor=int(Decimal(minimum) * 100),
        payment_day=None,
    )
    return account


@pytest.mark.django_db
def test_planner_matches_hand_computed_interest_and_lists_needs_details():
    owner = make_person("owner")
    ready = _ready_card(owner, "Synthetic Ready Card")
    incomplete = make_account(owner, name="Synthetic Incomplete Card")
    add_snapshot(incomplete, timezone.localdate(), 8_000)
    client = signed_in(owner)
    page = client.get(reverse("debt-payoff"), {"include": ready.pk, "strategy": "minimums"})
    html = page.content.decode()
    assert page.status_code == 200
    assert "\N{MINUS SIGN}$1.53" in html
    assert "needs details" in html
    assert "Synthetic Incomplete Card" in html
    assert "not financial advice" in html
    payload = json_script_payload(html, "debt-payoff-chart-data")
    assert payload["remaining_minor"][-1] == 0
    assert 'data-chart="debt-payoff"' in html
    assert "vendor/chart.umd.min.js" in html
    assert "js/charts.js" in html


@pytest.mark.django_db
def test_private_debt_never_appears_to_another_member():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    secret = _ready_card(owner, SECRET_CARD)
    shared = make_account(
        owner,
        name="Household Loan",
        account_type=Account.Type.LOAN,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    add_snapshot(shared, timezone.localdate(), 10_000)
    update_debt_terms(
        owner.user,
        shared.pk,
        apr_percent=Decimal("12.000"),
        minimum_payment_minor=5_000,
        payment_day=15,
    )
    member_page = signed_in(member).get(reverse("debt-payoff"))
    html = member_page.content.decode()
    assert "Household Loan" in html
    hidden = signed_in(member).get(
        reverse("debt-payoff"),
        {"include": secret.pk, "strategy": "minimums"},
    )
    hidden_html = hidden.content.decode()
    assert SECRET_CARD not in hidden_html
    assert hidden.status_code == 200
    outsider = make_person("outsider")
    denied = signed_in(outsider).post(
        reverse("account-debt-terms", args=[secret.pk]),
        {"apr_percent": "9.000", "minimum_payment": "10.00"},
    )
    assert denied.status_code == 404
    secret.refresh_from_db()
    assert secret.apr_percent == Decimal("12.000")


@pytest.mark.django_db
def test_household_member_can_edit_shared_debt_terms():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    loan = make_account(
        owner,
        name="Shared Loan",
        account_type=Account.Type.LOAN,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    add_snapshot(loan, timezone.localdate(), 20_000)
    response = signed_in(member).post(
        reverse("account-debt-terms", args=[loan.pk]),
        {"apr_percent": "6.250", "minimum_payment": "25.00", "payment_day": "12"},
    )
    assert response.status_code == 302
    loan.refresh_from_db()
    assert loan.apr_percent == Decimal("6.250")
    assert loan.minimum_payment_minor == 2_500
    assert loan.payment_day == 12


@pytest.mark.django_db
def test_update_debt_terms_rejects_invisible_and_non_liability_accounts():
    owner = make_person("owner")
    other = make_person("other")
    checking = make_account(owner, name="Synthetic Checking", account_type=Account.Type.CHECKING)
    card = make_account(owner, name="Synthetic Card")
    with pytest.raises(PermissionDenied):
        update_debt_terms(other.user, card.pk, apr_percent=Decimal("1.000"), minimum_payment_minor=100, payment_day=None)
    with pytest.raises(PermissionDenied):
        update_debt_terms(
            owner.user,
            checking.pk,
            apr_percent=Decimal("1.000"),
            minimum_payment_minor=100,
            payment_day=None,
        )
    balances = signed_in(owner).get(reverse("account-balances", args=[card.pk]))
    html = balances.content.decode()
    assert "Debt payoff details" in html
    assert 'name="payment_day"' not in html


@pytest.mark.django_db
def test_future_snapshot_is_ignored_and_simplefin_owed_sign_is_used():
    owner = make_person("owner")
    card = make_account(owner, name="Synthetic SimpleFIN Card")
    today = timezone.localdate()
    add_snapshot(card, today + timedelta(days=1), 99_999)
    add_snapshot(card, today - timedelta(days=2), -4_000, source=BalanceSnapshot.Source.SIMPLEFIN)
    update_debt_terms(
        owner.user,
        card.pk,
        apr_percent=Decimal("0.000"),
        minimum_payment_minor=4_000,
        payment_day=None,
    )
    html = signed_in(owner).get(reverse("debt-payoff")).content.decode()
    assert "\N{MINUS SIGN}$40.00" in html
    assert "+$40.00" not in html
    assert "$999.99" not in html


@pytest.mark.django_db
def test_custom_ranks_are_kept_after_comparing():
    owner = make_person("owner")
    first = _ready_card(owner, "Synthetic First Card")
    second = _ready_card(owner, "Synthetic Second Card")

    page = signed_in(owner).get(
        reverse("debt-payoff"),
        {
            "include": [first.pk, second.pk],
            "strategy": "custom",
            f"rank_{first.pk}": "2",
            f"rank_{second.pk}": "1",
        },
    )

    html = page.content.decode()
    for card, rank in ((first, "2"), (second, "1")):
        assert re.search(rf'<input[^>]*name="rank_{card.pk}"[^>]*value="{rank}"', html)


def _plan_debt(owner, name, snapshot_date, *, payment_day=None):
    account = make_account(
        owner, name=name, account_type=Account.Type.LOAN if payment_day else Account.Type.CREDIT_CARD,
    )
    add_snapshot(account, snapshot_date, 100_000)
    update_debt_terms(
        owner.user, account.pk, apr_percent=Decimal("0.000"), minimum_payment_minor=50_000, payment_day=payment_day,
    )
    return account


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("snapshot_date", "payment_day", "payoff"),
    [
        # A payment day that passed before the snapshot is already in the balance.
        (date(2026, 10, 10), 5, "2026-12"),
        (date(2026, 10, 10), 10, "2026-12"),
        # The snapshot predates this month's payment day.
        (date(2026, 10, 10), 20, "2026-11"),
        # Without a payment day, a snapshot from this month is taken to include it.
        (date(2026, 10, 1), None, "2026-12"),
        (date(2026, 9, 28), None, "2026-11"),
    ],
)
def test_current_month_is_simulated_only_when_the_snapshot_predates_its_payment(snapshot_date, payment_day, payoff):
    owner = make_person("owner")
    debt = _plan_debt(owner, "Synthetic Debt", snapshot_date, payment_day=payment_day)

    plan = build_debt_plan(
        owner.user, include_ids=[debt.pk], extra_minor=0, strategy="minimums", custom_order=None,
        today=date(2026, 10, 15),
    )

    assert plan.comparison.chosen.debts[0].payoff_label == payoff
    assert plan.start == (date(2026, 10, 1) if payoff == "2026-11" else date(2026, 11, 1))


@pytest.mark.django_db
def test_a_debt_already_paid_this_month_waits_while_others_pay_this_month():
    owner = make_person("owner")
    paid = _plan_debt(owner, "Synthetic Paid Loan", date(2026, 10, 10), payment_day=5)
    due = _plan_debt(owner, "Synthetic Due Card", date(2026, 9, 28))

    plan = build_debt_plan(
        owner.user, include_ids=[paid.pk, due.pk], extra_minor=0, strategy="minimums", custom_order=None,
        today=date(2026, 10, 15),
    )

    assert plan.start == date(2026, 10, 1)
    payoffs = {row.account_id: row.payoff_label for row in plan.comparison.chosen.debts}
    assert payoffs == {paid.pk: "2026-12", due.pk: "2026-11"}
