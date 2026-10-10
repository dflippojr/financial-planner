"""Bounded history reads and differential checks against the pre-batching path."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from finance import budget_services
from finance.alert_services import evaluate_active_budget_alerts
from finance.cash_flow import spending_by_category_report
from finance.models import Account, Alert, Budget, BudgetAmount, BudgetRolloverReset, Category, Membership, Transaction
from tests.test_budgets import add_budget, make_account, make_household, make_person, signed_in
from tests.test_totals_differential import ledger as ledger_fixture  # noqa: F401 -- shared synthetic ledger fixture


def legacy_reports(principal, months, scope, accounts=None, named=None):
    """The old one-full-report-per-month implementation, kept as the oracle."""
    return {
        month: spending_by_category_report(
            principal, date_from=month, date_to=budget_services.month_end(month), scope=scope, accounts=accounts
        )
        for month in months
    }


def card_values(cards):
    return {card.budget.pk: {key: value for key, value in vars(card).items() if key != "budget"} for card in cards}


@pytest.mark.django_db
@pytest.mark.parametrize("viewer", ["owner", "partner"])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("period", ["off", "on", "reset", "reenabled"])
def test_history_cards_and_alerts_match_old_reports(ledger, viewer, selected, period):
    household = ledger.shared.household
    categories = [None, *Category.objects.filter(
        household=household, name__in=["Groceries", "Dining", "Shopping", "Uncategorized", "Transfer"]
    )]
    budgets = []
    for scope in (Budget.Scope.PRIVATE, Budget.Scope.HOUSEHOLD):
        for category in categories:
            budget = add_budget(
                ledger.owner, category=category, scope=scope, month=date(2025, 12, 1),
                amount_minor=100, rollover=period != "off",
            )
            BudgetAmount.objects.create(budget=budget, effective_month=date(2026, 1, 1), amount_minor=200)
            # Future changes must not rewrite historical carry or current availability.
            BudgetAmount.objects.create(budget=budget, effective_month=date(2026, 3, 1), amount_minor=99_999)
            if period in ("reset", "reenabled"):
                reset = BudgetRolloverReset.objects.create(budget=budget, actor=ledger.owner, month=date(2026, 1, 1))
                if period == "reenabled":
                    BudgetRolloverReset.objects.filter(pk=reset.pk).update(created_at=timezone.now() - timedelta(days=1))
                    budget_services.set_budget_rollover(ledger.owner, budget, False, month=date(2026, 1, 1))
                    budget_services.set_budget_rollover(ledger.owner, budget, True, month=date(2025, 12, 1))
            budgets.append(budget)
    # Stored-category refund reporting must survive a now-hidden purchase.
    purchase = Transaction.objects.get(amount_minor=-8000)
    purchase.account = Account.objects.get(owner=ledger.partner, name="Partner private")
    purchase.save(update_fields=["account"])
    refund = Transaction.objects.get(amount_minor=3000)
    refund.category = household.categories.get(name="Dining")
    refund.save(update_fields=["category"])
    budget_services.set_budget_archived(ledger.owner, budgets[-1], True)
    principal = getattr(ledger, viewer)
    options = {"accounts": [ledger.checking, ledger.shared]} if selected else {}
    for month in (date(2026, 1, 1), date(2026, 2, 1)):
        for archived in (False, True):
            with patch.object(budget_services, "_reports_for_months", legacy_reports):
                expected = card_values(budget_services.month_budget_cards(principal, month, include_archived=archived, **options))
            actual = card_values(budget_services.month_budget_cards(principal, month, include_archived=archived, **options))
            assert actual == expected
        visible = list(Budget.objects.visible_to(principal))
        with patch.object(budget_services, "_reports_for_months", legacy_reports):
            expected = card_values(budget_services.progress_snapshots(visible, month, principal).values())
        actual = card_values(budget_services.progress_snapshots(visible, month, principal).values())
        assert actual == expected
        for budget in visible:
            assert card_values([budget_services.progress_snapshot(budget, month, principal)])[budget.pk] == expected[budget.pk]
        with patch.object(budget_services, "_reports_for_months", legacy_reports):
            evaluate_active_budget_alerts(today=month)
        expected_alerts = set(Alert.objects.values_list("recipient_id", "dedupe_key", "title", "link"))
        Alert.objects.all().delete()
        evaluate_active_budget_alerts(today=month)
        assert set(Alert.objects.values_list("recipient_id", "dedupe_key", "title", "link")) == expected_alerts
        Alert.objects.all().delete()


@pytest.mark.django_db
def test_three_year_page_and_snapshot_query_counts_are_bounded(monkeypatch):
    owner = make_person("history-owner")
    household = make_household(owner)
    make_account(owner)
    for category in Category.objects.filter(household=household).order_by("pk")[:12]:
        add_budget(owner, category=category, month=date(2023, 10, 1), amount_minor=100_000, rollover=True)
    monkeypatch.setattr(timezone, "localdate", lambda: date(2026, 10, 8))
    client = signed_in(owner)
    operations = {
        "home": lambda: client.get("/"),
        "budgets": lambda: client.get("/planning/budgets/"),
        "snapshots": lambda: budget_services.progress_snapshots(list(Budget.objects.all()), date(2026, 10, 1), owner),
        "snapshot": lambda: budget_services.progress_snapshot(Budget.objects.first(), date(2026, 10, 1), owner),
        "alerts": lambda: evaluate_active_budget_alerts(today=date(2026, 10, 8)),
    }
    # Match the benchmark's warm-up and exclude first-use settings creation.
    for operation in operations.values():
        operation()
    counts = {}
    for history in (12, 36):
        Budget.objects.update(rollover_started_month=date(2026 - history // 12, 10, 1))
        for name, operation in operations.items():
            with CaptureQueriesContext(connection) as queries:
                result = operation()
            if name in ("home", "budgets"):
                assert result.status_code == 200
            assert len(queries) <= 60, (name, len(queries))
            for table in ("finance_budgetamount", "finance_budgetrolloverreset"):
                reads = [q for q in queries if f'FROM "{table}"' in q["sql"]]
                assert len(reads) == 1, (name, table, reads)
            counts[name, history] = len(queries)
    for name in operations:
        assert counts[name, 12] == counts[name, 36], (name, counts)


@pytest.mark.django_db
def test_membership_loss_removes_shared_history_immediately(ledger):
    budget = add_budget(ledger.owner, category=None, month=date(2025, 12, 1), rollover=True)
    shared_budget = add_budget(ledger.owner, category=None, scope=Budget.Scope.HOUSEHOLD, month=date(2025, 12, 1), rollover=True)
    month = date(2026, 2, 1)
    before = {card.budget.pk: card for card in budget_services.month_budget_cards(ledger.owner, month)}
    assert shared_budget.pk in before
    Membership.objects.filter(person=ledger.owner, ended_at__isnull=True).update(ended_at=timezone.now())
    after = {card.budget.pk: card for card in budget_services.month_budget_cards(ledger.owner, month)}
    assert shared_budget.pk not in after
    with patch.object(budget_services, "_reports_for_months", legacy_reports):
        expected = budget_services.month_budget_cards(ledger.owner, month)
    assert card_values(after.values()) == card_values(expected)
    assert after[budget.pk].carry_minor != before[budget.pk].carry_minor
