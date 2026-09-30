from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from finance.category_services import (
    STARTER_CUSTOM_NAMES,
    assign_category,
    ensure_household_categories,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    undo_transfer_pair,
)
from finance.models import (
    Account,
    Category,
    Household,
    ImportBatch,
    Membership,
    Person,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
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


def make_transaction(owner, account, *, transaction_date=date(2026, 1, 2), amount_minor=-1000, description="Synthetic row", fingerprint=None, kind=Transaction.Kind.CASH_FLOW):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
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
def test_household_starter_categories_are_seeded_and_editable():
    owner = make_person("owner")
    household = make_household(owner)
    names = set(household.categories.values_list("name", flat=True))

    assert "Transfer" in names
    assert "Uncategorized" in names
    assert set(STARTER_CUSTOM_NAMES).issubset(names)

    client = Client()
    client.force_login(owner.user)
    response = client.post(reverse("category-list"), {"action": "add", "name": "Synthetic hobby"})

    assert response.status_code == 302
    assert household.categories.filter(name="Synthetic hobby").exists()


@pytest.mark.django_db
def test_transaction_can_be_assigned_and_cleared():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    financial_transaction = make_transaction(owner, account)
    groceries = household.categories.get(name="Groceries")
    client = Client()
    client.force_login(owner.user)

    assign_response = client.post(
        reverse("transaction-categorize", args=(financial_transaction.pk,)),
        {"category": groceries.pk},
    )
    financial_transaction.refresh_from_db()
    assert assign_response.status_code == 302
    assert financial_transaction.category == groceries

    clear_response = client.post(reverse("transaction-categorize", args=(financial_transaction.pk,)), {"category": ""})
    financial_transaction.refresh_from_db()
    assert clear_response.status_code == 302
    assert financial_transaction.category_id is None
    assert TransactionCorrectionHistory.objects.filter(
        transaction=financial_transaction,
        field_name=TransactionCorrectionHistory.Field.CATEGORY,
    ).count() == 2


@pytest.mark.django_db
def test_high_confidence_pair_is_auto_marked_with_reasons_and_can_be_undone():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    groceries = household.categories.get(name="Groceries")
    outflow = make_transaction(owner, checking, amount_minor=-2500, description="Synthetic to savings")
    inflow = make_transaction(owner, savings, amount_minor=2500, description="Synthetic from checking")
    assign_category(owner, outflow.pk, groceries.pk)

    client = Client()
    client.force_login(owner.user)
    review = client.get(reverse("transfer-review"))
    pair = TransferPair.objects.get()

    assert review.status_code == 200
    assert pair.status == TransferPair.Status.AUTO_MARKED
    assert pair.confidence == TransferPair.Confidence.HIGH
    assert "exact opposite amounts" in pair.reasons
    assert "only candidate for both legs in the window" in pair.reasons
    assert b"Undo exclusion" in review.content
    outflow.refresh_from_db()
    assert outflow.category_display == "Transfer"

    undo = client.post(reverse("transfer-review"), {"pair_id": pair.pk, "action": "undo"})
    pair.refresh_from_db()
    outflow.refresh_from_db()
    assert undo.status_code == 302
    assert pair.status == TransferPair.Status.UNDONE
    assert outflow.category_id == groceries.pk
    totals = income_and_spending_totals(owner)
    assert totals.spending_minor == 2500
    assert totals.income_minor == 2500


@pytest.mark.django_db
def test_low_confidence_pairs_are_suggestions_and_can_be_dismissed():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    extra = make_account(owner, name="Synthetic Extra", account_type=Account.Type.SAVINGS)
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic split out")
    make_transaction(owner, savings, amount_minor=4000, description="Synthetic split in one")
    make_transaction(owner, extra, amount_minor=4000, description="Synthetic split in two")

    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()

    assert pair.status == TransferPair.Status.SUGGESTED
    assert pair.confidence == TransferPair.Confidence.LOW
    totals = income_and_spending_totals(owner)
    assert totals.spending_minor == 4000
    assert totals.income_minor == 8000

    client = Client()
    client.force_login(owner.user)
    client.post(reverse("transfer-review"), {"pair_id": pair.pk, "action": "dismiss"})
    pair.refresh_from_db()
    dismissed_totals = income_and_spending_totals(owner)
    assert pair.status == TransferPair.Status.DISMISSED
    assert dismissed_totals.spending_minor == 4000
    refresh_transfer_pairs(owner)
    pair.refresh_from_db()
    assert pair.status == TransferPair.Status.DISMISSED


@pytest.mark.django_db
def test_low_confidence_suggestion_can_be_confirmed():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    extra = make_account(owner, name="Synthetic Extra", account_type=Account.Type.SAVINGS)
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic split out")
    make_transaction(owner, savings, amount_minor=4000, description="Synthetic split in one")
    make_transaction(owner, extra, amount_minor=4000, description="Synthetic split in two")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    client = Client()
    client.force_login(owner.user)
    client.post(reverse("transfer-review"), {"pair_id": pair.pk, "action": "confirm"})
    pair.refresh_from_db()
    confirmed = income_and_spending_totals(owner)
    assert pair.status == TransferPair.Status.CONFIRMED
    assert confirmed.spending_minor == 0
    assert confirmed.income_minor == 4000


@pytest.mark.django_db
def test_pairs_respect_window_exact_amounts_and_visibility():
    owner = make_person("owner")
    outsider = make_person("outsider")
    make_household(owner)
    make_household(outsider, name="Other Household")
    checking = make_account(owner, name="Owner Checking")
    savings = make_account(owner, name="Owner Savings", account_type=Account.Type.SAVINGS)
    other = make_account(outsider, name="Outsider Checking")
    make_transaction(owner, checking, amount_minor=-1500, transaction_date=date(2026, 1, 1))
    make_transaction(owner, savings, amount_minor=1500, transaction_date=date(2026, 1, 7))
    make_transaction(outsider, other, amount_minor=1500, transaction_date=date(2026, 1, 1))

    refresh_transfer_pairs(owner)

    assert TransferPair.objects.count() == 0

    household = owner.memberships.get().household
    household.transfer_match_window_days = 6
    household.save(update_fields=("transfer_match_window_days", "updated_at"))
    refresh_transfer_pairs(owner)
    assert TransferPair.objects.count() == 1
    assert TransferPair.objects.get().leg_a.account.owner_id == owner.pk


@pytest.mark.django_db
def test_private_accounts_of_different_people_are_never_paired():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    owner_private = make_account(owner, name="Owner Private")
    member_private = make_account(member, name="Member Private")
    make_transaction(owner, owner_private, amount_minor=-900)
    make_transaction(member, member_private, amount_minor=900)
    make_transaction(owner, shared, amount_minor=50, description="Synthetic shared noise")

    refresh_transfer_pairs(owner)
    refresh_transfer_pairs(member)

    assert TransferPair.objects.count() == 0


@pytest.mark.django_db
def test_unpaired_transaction_and_one_sided_card_payment_stay_in_totals():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Checking")
    card = make_account(owner, name="Card", account_type=Account.Type.CREDIT_CARD)
    make_transaction(owner, checking, amount_minor=-8800, description="Synthetic card payment outflow")
    make_transaction(owner, card, amount_minor=-1200, description="Synthetic purchase")

    refresh_transfer_pairs(owner)
    totals = income_and_spending_totals(owner)

    assert TransferPair.objects.count() == 0
    assert totals.spending_minor == 10000
    assert totals.income_minor == 0


@pytest.mark.django_db
def test_card_payment_is_excluded_only_with_both_legs():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Checking")
    card = make_account(owner, name="Card", account_type=Account.Type.CREDIT_CARD)
    make_transaction(owner, checking, amount_minor=-8800, description="Synthetic payment")
    make_transaction(owner, card, amount_minor=8800, description="Synthetic payment credit")

    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    totals = income_and_spending_totals(owner)

    assert pair.kind == TransferPair.Kind.CARD_PAYMENT
    assert pair.status == TransferPair.Status.AUTO_MARKED
    assert totals.income_minor == 0
    assert totals.spending_minor == 0


@pytest.mark.django_db
def test_refund_inherits_category_and_reduces_spending_not_income():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    original = make_transaction(owner, account, amount_minor=-4000, description="Synthetic store")
    refund = make_transaction(owner, account, amount_minor=1500, description="Synthetic store refund")
    assign_category(owner, original.pk, groceries.pk)
    link_refund(owner, refund.pk, original.pk)
    refund.refresh_from_db()
    totals = income_and_spending_totals(owner)

    assert refund.category_id == groceries.pk
    assert totals.income_minor == 0
    assert totals.spending_minor == 2500
    assert totals.spending_by_category_id[groceries.pk] == 2500


@pytest.mark.django_db
def test_investment_activity_is_omitted_from_income_and_spending():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner, account_type=Account.Type.INVESTMENT)
    make_transaction(
        owner,
        account,
        amount_minor=-2000,
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
        description="Synthetic investment activity",
    )

    totals = income_and_spending_totals(owner)
    assert totals.income_minor == 0
    assert totals.spending_minor == 0


@pytest.mark.django_db
def test_exclusion_hidden_when_counterpart_is_not_visible():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Owner Private")
    make_transaction(owner, private, amount_minor=-3000)
    shared_leg = make_transaction(owner, shared, amount_minor=3000)
    refresh_transfer_pairs(owner)

    owner_totals = income_and_spending_totals(owner)
    member_totals = income_and_spending_totals(member)
    client = Client()
    client.force_login(member.user)
    review = client.get(reverse("transfer-review"))
    listed = client.get(reverse("transaction-list"))

    assert owner_totals.income_minor == 0
    assert member_totals.income_minor == 3000
    assert TransferPair.objects.visible_to(member).count() == 0
    assert b"Undo exclusion" not in review.content
    assert b"Owner Private" not in listed.content
    assert shared_leg.description.encode() in listed.content


@pytest.mark.django_db
def test_outsider_cannot_categorize_or_see_private_errors():
    owner = make_person("owner")
    outsider = make_person("outsider")
    make_household(owner)
    make_household(outsider, name="Other")
    account = make_account(owner)
    financial_transaction = make_transaction(owner, account)
    groceries = Category.objects.get(household=owner.memberships.get().household, name="Groceries")

    with pytest.raises(PermissionDenied):
        assign_category(outsider, financial_transaction.pk, groceries.pk)

    client = Client()
    client.force_login(outsider.user)
    response = client.post(
        reverse("transaction-categorize", args=(financial_transaction.pk,)),
        {"category": groceries.pk},
    )
    assert response.status_code == 404
    assert b"Owner Private" not in response.content
    assert b"Synthetic row" not in response.content
