from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from finance.category_services import (
    STARTER_CUSTOM_NAMES,
    assign_category,
    confirm_transfer_pair,
    ensure_household_categories,
    exclusion_exists_for,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    rename_category,
    set_transfer_window_days,
    split_transaction,
    undo_transfer_pair,
    unsplit_transaction,
)
from finance.lifecycle_services import archive_account, share_account, unshare_account
from finance.csv_import.services import undo_import_batch
from finance.models import (
    Account,
    Category,
    Household,
    ImportBatch,
    Membership,
    Person,
    RefundLink,
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


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None, share_mode=None):
    if share_mode is None:
        share_mode = Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else ""
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
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
    outflow = make_transaction(owner, checking, amount_minor=-2500, description="Synthetic transfer to savings")
    make_transaction(owner, savings, amount_minor=2500, description="Synthetic transfer from checking")
    assign_category(owner, outflow.pk, groceries.pk)

    refresh_transfer_pairs(owner)
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
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic split transfer out")
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
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic split transfer out")
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
    make_transaction(owner, private, amount_minor=-3000, description="Synthetic transfer out")
    shared_leg = make_transaction(owner, shared, amount_minor=3000, description="Synthetic transfer in")
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
    shared_on_list = (
        Transaction.objects.visible_to(member)
        .annotate(_excluded=exclusion_exists_for(member))
        .get(pk=shared_leg.pk)
    )
    assert shared_on_list.category_display == "Uncategorized"
    transfer_filter = client.get(reverse("transaction-list"), {"category": "transfer"})
    assert shared_leg.description.encode() not in transfer_filter.content


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


@pytest.mark.django_db
def test_renamed_system_category_is_not_reseeded():
    owner = make_person("owner")
    household = make_household(owner)
    uncategorized = household.categories.get(code=Category.Code.UNCATEGORIZED)
    rename_category(owner, uncategorized.pk, "Inbox")
    client = Client()
    client.force_login(owner.user)

    categories = client.get(reverse("category-list"))
    listed = client.get(reverse("transaction-list"))

    assert categories.status_code == 200
    assert listed.status_code == 200
    assert household.categories.filter(code=Category.Code.UNCATEGORIZED).count() == 1
    assert household.categories.filter(code=Category.Code.TRANSFER).count() == 1
    uncategorized.refresh_from_db()
    assert uncategorized.name == "Inbox"


@pytest.mark.django_db
def test_transfer_review_get_does_not_write_pairs():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    make_transaction(owner, checking, amount_minor=-2500, description="Synthetic transfer to savings")
    make_transaction(owner, savings, amount_minor=2500, description="Synthetic transfer from checking")
    client = Client()
    client.force_login(owner.user)

    review = client.get(reverse("transfer-review"))

    assert review.status_code == 200
    assert TransferPair.objects.count() == 0


@pytest.mark.django_db
def test_cross_month_transfer_is_excluded_from_each_monthly_report():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    make_transaction(
        owner,
        checking,
        amount_minor=-2500,
        transaction_date=date(2026, 1, 31),
        description="Synthetic month-end transfer out",
    )
    make_transaction(
        owner,
        savings,
        amount_minor=2500,
        transaction_date=date(2026, 2, 1),
        description="Synthetic month-start transfer in",
    )
    refresh_transfer_pairs(owner)

    january = income_and_spending_totals(owner, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    february = income_and_spending_totals(owner, date_from=date(2026, 2, 1), date_to=date(2026, 2, 28))

    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    assert january.income_minor == 0
    assert january.spending_minor == 0
    assert february.income_minor == 0
    assert february.spending_minor == 0


@pytest.mark.django_db
def test_visible_investment_transfer_does_not_count_as_spending():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    brokerage = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic transfer contribution")
    make_transaction(
        owner,
        brokerage,
        amount_minor=4000,
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
        description="Synthetic investment activity",
    )
    refresh_transfer_pairs(owner)
    totals = income_and_spending_totals(owner)

    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    assert totals.income_minor == 0
    assert totals.spending_minor == 0


@pytest.mark.django_db
def test_corrected_amounts_that_no_longer_cancel_restore_original_categories():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    groceries = household.categories.get(name="Groceries")
    outflow = make_transaction(owner, checking, amount_minor=-10000, description="Synthetic transfer to savings")
    inflow = make_transaction(owner, savings, amount_minor=10000, description="Synthetic transfer from checking")
    assign_category(owner, outflow.pk, groceries.pk)
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED

    client = Client()
    client.force_login(owner.user)
    response = client.post(
        reverse("transaction-edit", args=(inflow.pk,)),
        {
            "transaction_date": "2026-01-02",
            "description": "Synthetic transfer from checking",
            "amount": "150.00",
        },
    )
    pair.refresh_from_db()
    outflow.refresh_from_db()
    inflow.refresh_from_db()
    totals = income_and_spending_totals(owner)

    assert response.status_code == 302
    assert pair.status == TransferPair.Status.UNDONE
    assert outflow.category_id == groceries.pk
    assert inflow.amount_minor == 15000
    assert totals.spending_minor == 10000
    assert totals.income_minor == 15000


@pytest.mark.django_db
def test_undoing_one_transfer_leg_restores_the_survivor():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    groceries = household.categories.get(name="Groceries")
    outflow = make_transaction(owner, checking, amount_minor=-2500, description="Synthetic transfer to savings")
    inflow = make_transaction(owner, savings, amount_minor=2500, description="Synthetic transfer from checking")
    assign_category(owner, outflow.pk, groceries.pk)
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED

    undo_import_batch(owner, savings.pk, inflow.import_batch_id)
    pair.refresh_from_db()
    outflow.refresh_from_db()
    totals = income_and_spending_totals(owner)
    listed = (
        Transaction.objects.visible_to(owner)
        .filter(status=Transaction.Status.ACTIVE)
        .annotate(_excluded=exclusion_exists_for(owner))
        .get(pk=outflow.pk)
    )

    assert pair.status == TransferPair.Status.UNDONE
    assert outflow.category_id == groceries.pk
    assert listed.category_display == "Groceries"
    assert totals.spending_minor == 2500
    assert totals.income_minor == 0


@pytest.mark.django_db
def test_stale_suggestion_is_invalidated_so_remaining_unique_pair_can_match():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    extra = make_account(owner, name="Synthetic Extra", account_type=Account.Type.SAVINGS)
    outflow = make_transaction(owner, checking, amount_minor=-10000, description="Synthetic split transfer out")
    suggested_in = make_transaction(owner, savings, amount_minor=10000, description="Synthetic split in one")
    remaining_in = make_transaction(owner, extra, amount_minor=10000, description="Synthetic split in two")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.SUGGESTED
    counterpart_id = pair.leg_a_id if pair.leg_a_id != outflow.pk else pair.leg_b_id
    counterpart = suggested_in if suggested_in.pk == counterpart_id else remaining_in
    leftover = remaining_in if counterpart is suggested_in else suggested_in

    client = Client()
    client.force_login(owner.user)
    response = client.post(
        reverse("transaction-edit", args=(counterpart.pk,)),
        {
            "transaction_date": "2026-01-02",
            "description": counterpart.description,
            "amount": "200.00",
        },
    )
    pair.refresh_from_db()
    rematch = TransferPair.objects.exclude(pk=pair.pk).get()
    leftover.refresh_from_db()
    totals = income_and_spending_totals(owner)

    assert response.status_code == 302
    assert pair.status == TransferPair.Status.UNDONE
    assert rematch.status == TransferPair.Status.AUTO_MARKED
    assert {rematch.leg_a_id, rematch.leg_b_id} == {outflow.pk, leftover.pk}
    assert totals.income_minor == 20000
    assert totals.spending_minor == 0


@pytest.mark.django_db
def test_refund_link_rejects_same_sign_original():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    deposit = make_transaction(owner, account, amount_minor=100000, description="Synthetic deposit")
    other_income = make_transaction(owner, account, amount_minor=5000, description="Synthetic other income")

    with pytest.raises(ValidationError, match="positive amount"):
        link_refund(owner, deposit.pk, other_income.pk)

    client = Client()
    client.force_login(owner.user)
    response = client.post(
        reverse("transaction-link-refund", args=(deposit.pk,)),
        {"original": other_income.pk},
    )
    totals = income_and_spending_totals(owner)

    assert response.status_code == 200
    assert b"positive amount" in response.content
    assert not RefundLink.objects.exists()
    assert totals.income_minor == 105000
    assert totals.spending_minor == 0


@pytest.mark.django_db
def test_linked_refund_reduces_spending_from_its_own_fields_when_original_is_hidden():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    purchase_account = make_account(owner, name="Synthetic Purchase Account")
    share_account(owner, purchase_account.pk, Account.ShareMode.CO_OWNED)
    refund_account = make_account(
        member,
        name="Synthetic Refund Account",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    original = make_transaction(owner, purchase_account, amount_minor=-4000, description="Synthetic store")
    refund = make_transaction(member, refund_account, amount_minor=1500, description="Synthetic store refund")
    assign_category(owner, original.pk, groceries.pk)
    link_refund(member, refund.pk, original.pk)
    unshare_account(owner, purchase_account.pk)

    member_totals = income_and_spending_totals(member)
    refund.refresh_from_db()
    client = Client()
    client.force_login(member.user)
    edit = client.get(reverse("transaction-edit", args=(refund.pk,)))

    assert refund.category_id == groceries.pk
    assert member_totals.income_minor == 0
    assert member_totals.spending_minor == -1500
    assert member_totals.spending_by_category_id[groceries.pk] == -1500
    assert not RefundLink.objects.visible_to(member).exists()
    assert b"This refund is linked" not in edit.content
    assert b"Synthetic Purchase Account" not in edit.content
    assert b"Search purchases" not in edit.content


@pytest.mark.django_db
def test_recategorizing_original_propagates_to_linked_refunds():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    original = make_transaction(owner, account, amount_minor=-4000, description="Synthetic store")
    refund = make_transaction(owner, account, amount_minor=1500, description="Synthetic store refund")
    assign_category(owner, original.pk, groceries.pk)
    link_refund(owner, refund.pk, original.pk)
    assign_category(owner, original.pk, dining.pk)
    refund.refresh_from_db()
    original.refresh_from_db()
    totals = income_and_spending_totals(owner)

    assert original.category_id == dining.pk
    assert refund.category_id == dining.pk
    assert totals.spending_minor == 2500
    assert totals.spending_by_category_id[dining.pk] == 2500
    assert groceries.pk not in totals.spending_by_category_id
    assert TransactionCorrectionHistory.objects.filter(
        transaction=refund,
        field_name=TransactionCorrectionHistory.Field.CATEGORY,
        previous_description="Groceries",
        new_description="Dining",
    ).exists()


@pytest.mark.django_db
def test_narrowing_transfer_window_revalidates_pairs_then_refreshes():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    extra = make_account(owner, name="Synthetic Extra", account_type=Account.Type.SAVINGS)
    auto_out = make_transaction(
        owner,
        checking,
        amount_minor=-2100,
        transaction_date=date(2026, 1, 1),
        description="Synthetic auto transfer out",
    )
    auto_in = make_transaction(
        owner,
        savings,
        amount_minor=2100,
        transaction_date=date(2026, 1, 4),
        description="Synthetic auto transfer in",
    )
    make_transaction(
        owner,
        checking,
        amount_minor=-1100,
        transaction_date=date(2026, 3, 1),
        description="Synthetic suggested out",
    )
    make_transaction(
        owner,
        savings,
        amount_minor=1100,
        transaction_date=date(2026, 3, 4),
        description="Synthetic suggested in a",
    )
    make_transaction(
        owner,
        extra,
        amount_minor=1100,
        transaction_date=date(2026, 3, 4),
        description="Synthetic suggested in b",
    )
    make_transaction(
        owner,
        checking,
        amount_minor=-1300,
        transaction_date=date(2026, 4, 1),
        description="Synthetic confirm transfer out",
    )
    make_transaction(
        owner,
        savings,
        amount_minor=1300,
        transaction_date=date(2026, 4, 4),
        description="Synthetic confirm in a",
    )
    make_transaction(
        owner,
        extra,
        amount_minor=1300,
        transaction_date=date(2026, 4, 4),
        description="Synthetic confirm in b",
    )
    refresh_transfer_pairs(owner)
    auto_pair = TransferPair.objects.get(leg_a_id__in=(auto_out.pk, auto_in.pk), leg_b_id__in=(auto_out.pk, auto_in.pk))
    suggested_pairs = list(TransferPair.objects.filter(status=TransferPair.Status.SUGGESTED).order_by("pk"))
    confirm_pair = suggested_pairs[0]
    leftover_suggested = suggested_pairs[1]
    confirm_transfer_pair(owner, confirm_pair.pk)

    set_transfer_window_days(owner, 0)
    auto_pair.refresh_from_db()
    confirm_pair.refresh_from_db()
    leftover_suggested.refresh_from_db()
    auto_out.refresh_from_db()
    totals = income_and_spending_totals(owner)

    assert auto_pair.status == TransferPair.Status.UNDONE
    assert confirm_pair.status == TransferPair.Status.CONFIRMED
    assert leftover_suggested.status == TransferPair.Status.UNDONE
    assert auto_out.category_id is None
    assert totals.income_minor == 5800
    assert totals.spending_minor == 3400



@pytest.mark.django_db
def test_renamed_starter_category_is_not_reseeded():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")

    rename_category(owner, groceries.pk, "Synthetic food")
    ensure_household_categories(household)

    assert not household.categories.filter(name="Groceries").exists()
    assert household.categories.filter(pk=groceries.pk, name="Synthetic food").exists()


@pytest.mark.django_db
def test_narrowing_window_revalidates_other_members_private_pairs():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    checking = make_account(member, name="Synthetic Member Checking")
    savings = make_account(member, name="Synthetic Member Savings", account_type=Account.Type.SAVINGS)
    outflow = make_transaction(member, checking, amount_minor=-2700, transaction_date=date(2026, 1, 1), description="Synthetic transfer out")
    make_transaction(member, savings, amount_minor=2700, transaction_date=date(2026, 1, 4))
    refresh_transfer_pairs(member)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED

    set_transfer_window_days(owner, 0)
    pair.refresh_from_db()

    assert pair.status == TransferPair.Status.UNDONE
    assert income_and_spending_totals(member).spending_minor == 2700
    assert TransactionCorrectionHistory.objects.filter(
        transaction=outflow,
        field_name=TransactionCorrectionHistory.Field.EXCLUSION,
        new_description="included",
        actor=owner,
    ).exists()


@pytest.mark.django_db
def test_category_assignment_rechecks_visibility_after_locking(monkeypatch):
    import finance.category_services as category_services

    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    account = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    financial_transaction = make_transaction(owner, account)
    groceries = household.categories.get(name="Groceries")
    real_lock = category_services._lock_owned_transactions

    def unshare_then_lock(transactions):
        unshare_account(owner, account.pk)
        return real_lock(transactions)

    monkeypatch.setattr(category_services, "_lock_owned_transactions", unshare_then_lock)

    with pytest.raises(PermissionDenied):
        assign_category(member, financial_transaction.pk, groceries.pk)
    financial_transaction.refresh_from_db()
    assert financial_transaction.category_id is None
    assert not TransactionCorrectionHistory.objects.filter(transaction=financial_transaction).exists()


@pytest.mark.django_db
def test_rename_to_existing_name_shows_a_message_instead_of_failing():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    client = Client()
    client.force_login(owner.user)

    response = client.post(
        reverse("category-list"),
        {"action": "rename", "category_id": groceries.pk, "name": "Dining"},
    )

    assert response.status_code == 200
    assert b"A category with that name already exists." in response.content
    groceries.refresh_from_db()
    assert groceries.name == "Groceries"


@pytest.mark.django_db
def test_category_filter_leaves_out_excluded_transfers():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    groceries = household.categories.get(name="Groceries")
    outflow = make_transaction(owner, checking, amount_minor=-3100, description="Synthetic transfer out")
    kept = make_transaction(owner, checking, amount_minor=-900, description="Synthetic market")
    assign_category(owner, outflow.pk, groceries.pk)
    assign_category(owner, kept.pk, groceries.pk)
    make_transaction(owner, savings, amount_minor=3100, description="Synthetic transfer in")
    refresh_transfer_pairs(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("transaction-list"), {"category": groceries.pk})

    assert response.status_code == 200
    assert list(response.context["transactions"]) == [kept]


@pytest.mark.django_db
def test_undoing_a_transfer_keeps_linked_refunds_with_the_restored_category():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    purchase = make_transaction(owner, checking, amount_minor=-4200, description="Synthetic store")
    refund = make_transaction(owner, checking, amount_minor=700, description="Synthetic store refund")
    assign_category(owner, purchase.pk, groceries.pk)
    link_refund(owner, refund.pk, purchase.pk)
    make_transaction(owner, savings, amount_minor=4200, description="Synthetic transfer matching credit")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(status=TransferPair.Status.AUTO_MARKED)
    assign_category(owner, purchase.pk, dining.pk)

    undo_transfer_pair(owner, pair.pk)
    purchase.refresh_from_db()
    refund.refresh_from_db()

    assert purchase.category_id == groceries.pk
    assert refund.category_id == groceries.pk
    assert dining.pk not in income_and_spending_totals(owner).spending_by_category_id


@pytest.mark.django_db
def test_undo_records_the_restored_category_in_history():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    outflow = make_transaction(owner, checking, amount_minor=-3300, description="Synthetic transfer out")
    make_transaction(owner, savings, amount_minor=3300, description="Synthetic transfer in")
    assign_category(owner, outflow.pk, groceries.pk)
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(status=TransferPair.Status.AUTO_MARKED)
    assign_category(owner, outflow.pk, dining.pk)

    undo_transfer_pair(owner, pair.pk)
    latest = (
        TransactionCorrectionHistory.objects.filter(
            transaction=outflow,
            field_name=TransactionCorrectionHistory.Field.CATEGORY,
        )
        .order_by("-recorded_at", "-pk")
        .first()
    )

    assert latest.previous_description == "Dining"
    assert latest.new_description == "Groceries"


@pytest.mark.django_db
def test_archiving_shared_account_unmarks_pair_with_other_members_private_leg():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    shared = make_account(owner, name="Synthetic Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Member Savings", account_type=Account.Type.SAVINGS)
    shared_leg = make_transaction(owner, shared, amount_minor=-2800, description="Synthetic shared transfer out")
    private_leg = make_transaction(member, private, amount_minor=2800, description="Synthetic private transfer in")
    assign_category(member, private_leg.pk, groceries.pk)
    refresh_transfer_pairs(member)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED
    assign_category(member, private_leg.pk, dining.pk)

    archive_account(owner, shared.pk)
    pair.refresh_from_db()
    private_leg.refresh_from_db()

    assert pair.status == TransferPair.Status.UNDONE
    assert private_leg.category_id == groceries.pk
    assert income_and_spending_totals(member).income_minor == 2800
    assert TransactionCorrectionHistory.objects.filter(
        transaction=private_leg,
        field_name=TransactionCorrectionHistory.Field.EXCLUSION,
        new_description="included",
        actor=owner,
    ).exists()
    assert not Transaction.objects.visible_to(owner).filter(pk=private_leg.pk).exists()
    shared_leg.refresh_from_db()
    assert shared_leg.status == Transaction.Status.ARCHIVED


@pytest.mark.django_db
def test_unsharing_shared_account_unmarks_pair_with_other_members_private_leg():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Member Savings", account_type=Account.Type.SAVINGS)
    make_transaction(owner, shared, amount_minor=-2900, description="Synthetic shared transfer out")
    private_leg = make_transaction(member, private, amount_minor=2900, description="Synthetic private transfer in")
    refresh_transfer_pairs(member)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED

    unshare_account(owner, shared.pk)
    pair.refresh_from_db()
    private_leg.refresh_from_db()

    assert pair.status == TransferPair.Status.UNDONE
    assert private_leg.category_id is None
    assert income_and_spending_totals(member).income_minor == 2900
    assert TransactionCorrectionHistory.objects.filter(
        transaction=private_leg,
        field_name=TransactionCorrectionHistory.Field.EXCLUSION,
        new_description="included",
        actor=owner,
    ).exists()


@pytest.mark.django_db
def test_undoing_import_of_shared_leg_restores_other_members_snapshot_category():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    shared = make_account(member, name="Synthetic Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Synthetic Owner Savings", account_type=Account.Type.SAVINGS)
    shared_leg = make_transaction(member, shared, amount_minor=-3000, description="Synthetic shared transfer out")
    private_leg = make_transaction(owner, private, amount_minor=3000, description="Synthetic private transfer in")
    assign_category(owner, private_leg.pk, groceries.pk)
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED
    assign_category(owner, private_leg.pk, dining.pk)

    undo_import_batch(member, shared.pk, shared_leg.import_batch_id)
    pair.refresh_from_db()
    private_leg.refresh_from_db()

    assert pair.status == TransferPair.Status.UNDONE
    assert private_leg.category_id == groceries.pk
    assert income_and_spending_totals(owner).income_minor == 3000
    assert TransactionCorrectionHistory.objects.filter(
        transaction=private_leg,
        field_name=TransactionCorrectionHistory.Field.CATEGORY,
        previous_description="Dining",
        new_description="Groceries",
        actor=member,
    ).exists()


def _shared_and_private_counterparts():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Owner Private")
    private_leg = make_transaction(owner, private, amount_minor=-50000, transaction_date=date(2026, 1, 2), description="Synthetic neutral row")
    shared_leg = make_transaction(member, shared, amount_minor=50000, transaction_date=date(2026, 1, 2), description="Transfer")
    return owner, member, private_leg, shared_leg


def _exclusion_history(transaction):
    return TransactionCorrectionHistory.objects.filter(
        transaction=transaction,
        field_name=TransactionCorrectionHistory.Field.EXCLUSION,
    )


@pytest.mark.django_db
def test_window_change_by_member_leaves_other_members_private_rows_unpaired():
    owner, member, private_leg, shared_leg = _shared_and_private_counterparts()
    totals_before = income_and_spending_totals(owner)

    set_transfer_window_days(member, 30)

    assert not TransferPair.objects.filter(leg_a=private_leg).exists()
    assert not TransferPair.objects.filter(leg_b=private_leg).exists()
    assert not _exclusion_history(shared_leg).exists()
    assert not _exclusion_history(private_leg).exists()
    assert income_and_spending_totals(owner) == totals_before


@pytest.mark.django_db
def test_unsplit_by_member_leaves_other_members_private_rows_unpaired():
    owner, member, private_leg, shared_leg = _shared_and_private_counterparts()
    household = owner.memberships.get().household
    groceries = household.categories.get(name="Groceries")
    totals_before = income_and_spending_totals(owner)
    split_transaction(member, shared_leg.pk, [
        {"category_id": groceries.pk, "amount_minor": 20000},
        {"category_id": groceries.pk, "amount_minor": 30000},
    ])

    unsplit_transaction(member, shared_leg.pk, None)

    assert not TransferPair.objects.filter(leg_a=private_leg).exists()
    assert not TransferPair.objects.filter(leg_b=private_leg).exists()
    assert not _exclusion_history(shared_leg).exists()
    assert income_and_spending_totals(owner) == totals_before


@pytest.mark.django_db
def test_owner_refresh_pairs_shared_row_and_hides_exclusion_history_from_member():
    owner, member, private_leg, shared_leg = _shared_and_private_counterparts()
    set_transfer_window_days(member, 30)

    refresh_transfer_pairs(owner)

    pair = TransferPair.objects.get()
    assert {pair.leg_a_id, pair.leg_b_id} == {private_leg.pk, shared_leg.pk}
    assert pair.status == TransferPair.Status.AUTO_MARKED
    assert TransactionCorrectionHistory.objects.visible_to(owner).filter(
        transaction=shared_leg, field_name=TransactionCorrectionHistory.Field.EXCLUSION
    ).exists()
    assert not TransactionCorrectionHistory.objects.visible_to(member).filter(
        transaction=shared_leg, field_name=TransactionCorrectionHistory.Field.EXCLUSION
    ).exists()
    client = Client()
    client.force_login(member.user)
    response = client.get(reverse("transaction-edit", args=(shared_leg.pk,)))
    assert response.status_code == 200
    assert b"Transfer exclusion" not in response.content


@pytest.mark.django_db
def test_exclusion_history_stays_visible_when_both_legs_are_visible():
    owner, member, private_leg, shared_leg = _shared_and_private_counterparts()
    household = owner.memberships.get().household
    other_shared = make_account(owner, name="Shared Savings", account_type=Account.Type.SAVINGS, scope=Account.Scope.HOUSEHOLD, household=household)
    outflow = make_transaction(member, other_shared, amount_minor=-1200, description="Synthetic transfer out")
    inflow = make_transaction(member, shared_leg.account, amount_minor=1200, description="Synthetic transfer in")

    refresh_transfer_pairs(member)

    assert TransactionCorrectionHistory.objects.visible_to(member).filter(
        transaction__in=(outflow, inflow), field_name=TransactionCorrectionHistory.Field.EXCLUSION
    ).count() == 2
