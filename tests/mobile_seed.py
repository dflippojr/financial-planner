"""Synthetic data for the phone-layout checks and screenshots. No real figures."""

from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from finance.alert_services import raise_alert
from finance.budget_services import save_budget
from finance.category_services import assign_category, ensure_household_categories
from finance.models import Account, Budget, Category, Household, ImportBatch, Person, Transaction

ROWS = (
    # (days ago, amount minor, description, category name or None)
    (1, -8642, "Kroger #512 with an unusually long store description", "Groceries"),
    (1, 269000, "Payroll deposit", None),
    (1, -4118, "Shell Oil 5741", None),
    (2, -5890, "Corner Bistro", None),
    (2, -50000, "Transfer to Savings", None),
    (3, -61240, "Restaurant group dinner", "Dining"),
    (4, -54010, "Farmers market", "Groceries"),
    (6, -9500, "Metro pass", "Transportation"),
)


def seed_phone_data():
    """Load the synthetic household and add rows, budgets and an alert; return the person."""
    call_command("loaddata", "synthetic_demo", verbosity=0)
    person = Person.objects.get(user__username="synthetic_alex")
    household = Household.objects.get()
    ensure_household_categories(household)
    account = Account.objects.get(name="Synthetic Checking")
    batch = ImportBatch.objects.filter(account=account).first()
    today = timezone.localdate()
    categories = {category.name: category for category in Category.objects.filter(household=household)}
    for index, (days_ago, amount, description, category_name) in enumerate(ROWS):
        transaction = Transaction.objects.create(
            account=account,
            import_batch=batch,
            transaction_date=today - timedelta(days=days_ago),
            amount_minor=amount,
            description=description,
            kind=Transaction.Kind.CASH_FLOW,
            source_row_number=100 + index,
            fingerprint=f"{index:02d}".encode().hex().ljust(64, "c"),
            original_fields={"Synthetic Amount": str(amount)},
        )
        category = categories.get(category_name) if category_name else None
        if category is not None:
            assign_category(person.user, transaction.pk, category.pk)
    month = today.replace(day=1)
    for name, amount in (("Dining", 50_000), ("Groceries", 60_000), ("Transportation", 25_000)):
        category = categories.get(name)
        if category is not None:
            save_budget(
                person.user,
                {
                    "scope": Budget.Scope.PRIVATE,
                    "category": category,
                    "amount_minor": amount,
                    "effective_month": month,
                    "rollover_enabled": False,
                },
            )
    raise_alert([person], "budget", "Dining is over budget", "/planning/budgets/", "phone-seed-1")
    return person
