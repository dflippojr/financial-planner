from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from finance.category_services import (
    SPLIT_AMOUNT_ERROR,
    SPLIT_COUNT_ERROR,
    SPLIT_PART_REQUIRED,
    SPLIT_REFUND_ERROR,
    SPLIT_SIGN_ERROR,
    SPLIT_SUM_ERROR,
    SPLIT_TRANSFER_ERROR,
    assign_category,
    assign_split_part_category,
    ensure_household_categories,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    split_transaction,
    unsplit_transaction,
)
from finance.models import (
    Account,
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


def make_account(
    owner,
    *,
    name="Synthetic Checking",
    account_type=Account.Type.CHECKING,
    scope=Account.Scope.PRIVATE,
    household=None,
    share_mode=None,
):
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


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 2),
    amount_minor=-10000,
    description="Synthetic warehouse",
    fingerprint=None,
    kind=Transaction.Kind.CASH_FLOW,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        kind=kind,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"Description": description},
    )


@pytest.mark.django_db
def test_split_moves_spending_between_categories_without_changing_cash_flow():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    household_cat = household.categories.get(name="Housing")
    account = make_account(owner)
    txn = make_transaction(owner, account, amount_minor=-10000)
    assign_category(owner, txn.pk, groceries.pk)
    before = income_and_spending_totals(owner)

    split_transaction(
        owner,
        txn.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -6000},
            {"category_id": household_cat.pk, "amount_minor": -4000},
        ),
    )

    after = income_and_spending_totals(owner)
    txn.refresh_from_db()
    assert after.income_minor == before.income_minor
    assert after.spending_minor == before.spending_minor
    assert after.net_minor == before.net_minor
    assert after.spending_by_category_id[groceries.pk] == before.spending_by_category_id[groceries.pk] - 4000
    assert after.spending_by_category_id[household_cat.pk] == 4000
    assert txn.category_id is None
    assert txn.category_source == Transaction.CategorySource.SPLIT
    history = TransactionCorrectionHistory.objects.filter(transaction=txn, field_name="category").latest("pk")
    assert history.new_description == "Split: Groceries $60.00, Housing $40.00"


@pytest.mark.django_db
def test_split_refusals():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    purchase = make_transaction(owner, checking, amount_minor=-5000, description="Synthetic store")
    refund = make_transaction(owner, checking, amount_minor=1500, description="Synthetic refund")
    outflow = make_transaction(owner, checking, amount_minor=-3100, description="Synthetic transfer out")
    make_transaction(owner, savings, amount_minor=3100, description="Synthetic transfer in")
    refresh_transfer_pairs(owner)
    link_refund(owner, refund.pk, purchase.pk)

    with pytest.raises(ValidationError, match=SPLIT_TRANSFER_ERROR):
        split_transaction(
            owner,
            outflow.pk,
            (
                {"category_id": groceries.pk, "amount_minor": -2000},
                {"category_id": housing.pk, "amount_minor": -1100},
            ),
        )
    with pytest.raises(ValidationError, match=SPLIT_REFUND_ERROR):
        split_transaction(
            owner,
            refund.pk,
            (
                {"category_id": groceries.pk, "amount_minor": 500},
                {"category_id": housing.pk, "amount_minor": 1000},
            ),
        )
    with pytest.raises(ValidationError, match=SPLIT_SUM_ERROR):
        split_transaction(
            owner,
            purchase.pk,
            (
                {"category_id": groceries.pk, "amount_minor": -2000},
                {"category_id": housing.pk, "amount_minor": -2000},
            ),
        )
    with pytest.raises(ValidationError, match=SPLIT_SIGN_ERROR):
        split_transaction(
            owner,
            purchase.pk,
            (
                {"category_id": groceries.pk, "amount_minor": -6000},
                {"category_id": housing.pk, "amount_minor": 1000},
            ),
        )
    with pytest.raises(ValidationError, match=SPLIT_COUNT_ERROR):
        split_transaction(owner, purchase.pk, ({"category_id": groceries.pk, "amount_minor": -5000},))
    with pytest.raises(ValidationError, match=SPLIT_PART_REQUIRED):
        split_transaction(
            owner,
            purchase.pk,
            (
                {"category_id": groceries.pk, "amount_minor": -3000},
                {"category_id": housing.pk, "amount_minor": -2000},
            ),
        )


@pytest.mark.django_db
def test_split_assigns_existing_refunds_and_unsplit_restores_one_category():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    account = make_account(owner)
    purchase = make_transaction(owner, account, amount_minor=-8000, description="Synthetic purchase")
    refund = make_transaction(owner, account, amount_minor=2000, description="Synthetic refund")
    assign_category(owner, purchase.pk, groceries.pk)
    link_refund(owner, refund.pk, purchase.pk)

    split_transaction(
        owner,
        purchase.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -5000},
            {"category_id": housing.pk, "amount_minor": -3000},
        ),
        refund_assignments={refund.pk: 1},
    )

    refund.refresh_from_db()
    link = RefundLink.objects.get(refund=refund)
    assert refund.category_id == housing.pk
    assert link.original_part.category_id == housing.pk
    totals = income_and_spending_totals(owner)
    assert totals.spending_by_category_id[groceries.pk] == 5000
    assert totals.spending_by_category_id[housing.pk] == 1000

    unsplit_transaction(owner, purchase.pk, groceries.pk)
    purchase.refresh_from_db()
    refund.refresh_from_db()
    link.refresh_from_db()
    assert purchase.category_id == groceries.pk
    assert purchase.category_source == Transaction.CategorySource.MANUAL
    assert purchase.splits.count() == 0
    assert refund.category_id == groceries.pk
    assert link.original_part_id is None


@pytest.mark.django_db
def test_link_refund_to_split_requires_part_and_part_recategorize_propagates():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    purchase = make_transaction(owner, account, amount_minor=-9000)
    refund = make_transaction(owner, account, amount_minor=1000, description="Synthetic refund")
    split_transaction(
        owner,
        purchase.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -4000},
            {"category_id": housing.pk, "amount_minor": -5000},
        ),
    )
    with pytest.raises(ValidationError, match=SPLIT_PART_REQUIRED):
        link_refund(owner, refund.pk, purchase.pk)
    groceries_part = purchase.splits.get(category=groceries)
    link_refund(owner, refund.pk, purchase.pk, original_part_id=groceries_part.pk)
    refund.refresh_from_db()
    assert refund.category_id == groceries.pk
    assign_split_part_category(owner, groceries_part.pk, dining.pk)
    refund.refresh_from_db()
    assert refund.category_id == dining.pk


@pytest.mark.django_db
def test_split_skips_transfer_pairing_and_hides_from_outsiders():
    owner = make_person("owner")
    outsider = make_person("outsider")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    outflow = make_transaction(owner, checking, amount_minor=-3100, description="Synthetic transfer out")
    make_transaction(owner, savings, amount_minor=3100, description="Synthetic transfer in")
    split_transaction(
        owner,
        outflow.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -2000},
            {"category_id": housing.pk, "amount_minor": -1100},
        ),
    )
    refresh_transfer_pairs(owner)
    assert not TransferPair.objects.filter(leg_a=outflow).exists()
    assert not TransferPair.objects.filter(leg_b=outflow).exists()
    with pytest.raises(PermissionDenied):
        split_transaction(
            outsider,
            outflow.pk,
            (
                {"category_id": groceries.pk, "amount_minor": -2000},
                {"category_id": housing.pk, "amount_minor": -1100},
            ),
        )


@pytest.mark.django_db
def test_split_and_unsplit_pages_and_amount_edit_refusal():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    account = make_account(owner)
    txn = make_transaction(owner, account, amount_minor=-10000)
    client = Client()
    client.force_login(owner.user)

    edit = client.get(reverse("transaction-edit", args=(txn.pk,)))
    assert edit.status_code == 200
    assert b"Split" in edit.content

    response = client.post(
        reverse("transaction-split", args=(txn.pk,)),
        {
            "part_count": "2",
            "part_0_category": str(groceries.pk),
            "part_0_amount": "-60.00",
            "part_1_category": str(housing.pk),
            "part_1_amount": "-40.00",
        },
    )
    assert response.status_code == 302
    txn.refresh_from_db()
    assert txn.category_source == Transaction.CategorySource.SPLIT

    listed = client.get(reverse("transaction-list"))
    assert b"Split (2)" in listed.content
    assert b"Groceries" in listed.content
    assert b"Housing" in listed.content

    amount_response = client.post(
        reverse("transaction-edit", args=(txn.pk,)),
        {
            "transaction_date": "2026-01-02",
            "description": "Synthetic warehouse",
            "amount": "-50.00",
        },
    )
    assert amount_response.status_code == 200
    assert SPLIT_AMOUNT_ERROR.encode() in amount_response.content
    txn.refresh_from_db()
    assert txn.amount_minor == -10000

    unsplit = client.post(
        reverse("transaction-unsplit", args=(txn.pk,)),
        {"category": str(groceries.pk)},
    )
    assert unsplit.status_code == 302
    txn.refresh_from_db()
    assert txn.category_id == groceries.pk
    assert txn.splits.count() == 0


@pytest.mark.django_db
def test_rules_and_ai_skip_split_transactions():
    from finance.category_suggestion_services import eligible_uncategorized
    from finance.rule_services import apply_rule, preview_rule, save_category_rule

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    split_txn = make_transaction(owner, account, amount_minor=-10000, description="SYNTHETIC KROGER")
    open_txn = make_transaction(
        owner,
        account,
        amount_minor=-1200,
        description="SYNTHETIC KROGER extra",
        fingerprint="c" * 64,
    )
    split_transaction(
        owner,
        split_txn.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -6000},
            {"category_id": housing.pk, "amount_minor": -4000},
        ),
    )
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining.pk,
        priority=1,
    )
    _, matches = preview_rule(owner, rule.pk)
    assert {item.pk for item in matches} == {open_txn.pk}
    apply_rule(owner, rule.pk)
    split_txn.refresh_from_db()
    open_txn.refresh_from_db()
    assert split_txn.category_source == Transaction.CategorySource.SPLIT
    assert open_txn.category_id == dining.pk
    assert list(eligible_uncategorized(owner).values_list("pk", flat=True)) == []


@pytest.mark.django_db
def test_export_includes_split_parts_and_refund_part_links():
    from finance.export import collect_export_tables

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    account = make_account(owner)
    purchase = make_transaction(owner, account, amount_minor=-8000)
    refund = make_transaction(owner, account, amount_minor=2000, description="Synthetic refund")
    split_transaction(
        owner,
        purchase.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -5000},
            {"category_id": housing.pk, "amount_minor": -3000},
        ),
    )
    part = purchase.splits.get(category=housing)
    link_refund(owner, refund.pk, purchase.pk, original_part_id=part.pk)
    tables = collect_export_tables(owner)
    split_rows = {row["id"]: row for row in tables["transaction_splits"]}
    assert len(split_rows) == 2
    assert {row["amount_minor"] for row in split_rows.values()} == {-5000, -3000}
    refund_row = next(row for row in tables["transactions"] if row["id"] == refund.pk)
    assert refund_row["refund_original_id"] == purchase.pk
    assert refund_row["refund_original_part_id"] == part.pk


@pytest.mark.django_db
def test_deleting_account_removes_splits_and_repairs_refund_links():
    from finance.lifecycle_services import delete_account
    from finance.models import TransactionSplit

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    doomed = make_account(owner, name="Synthetic Doomed")
    kept = make_account(owner, name="Synthetic Kept")
    purchase = make_transaction(owner, doomed, amount_minor=-8000)
    refund = make_transaction(owner, kept, amount_minor=2000, description="Synthetic refund")
    split_transaction(
        owner,
        purchase.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -5000},
            {"category_id": housing.pk, "amount_minor": -3000},
        ),
    )
    part = purchase.splits.get(category=groceries)
    link_refund(owner, refund.pk, purchase.pk, original_part_id=part.pk)
    delete_account(owner, doomed.pk)
    assert not TransactionSplit.objects.exists()
    assert not RefundLink.objects.exists()
    refund.refresh_from_db()
    assert refund.category_id == groceries.pk


@pytest.mark.django_db
def test_split_hides_other_member_private_refund_and_assigns_largest_part():
    from finance.forms import SplitTransactionForm

    member_a = make_person("member_a")
    member_b = make_person("member_b")
    household = make_household(member_a, member_b)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    shared = make_account(
        member_a,
        name="Synthetic Shared Checking",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private_a = make_account(member_a, name="Synthetic A Private")
    purchase = make_transaction(
        member_a,
        shared,
        amount_minor=-9000,
        description="Synthetic shared warehouse",
    )
    refund = make_transaction(
        member_a,
        private_a,
        transaction_date=date(2026, 3, 17),
        amount_minor=1234,
        description="Synthetic A private refund XYZ",
    )
    assign_category(member_a, purchase.pk, groceries.pk)
    link_refund(member_a, refund.pk, purchase.pk)

    form = SplitTransactionForm(principal=member_b, transaction=purchase)
    assert form.refund_fields == []
    form_text = " ".join(str(field.label) for field in form.fields.values())
    assert "Synthetic A private refund XYZ" not in form_text
    assert "2026-03-17" not in form_text
    assert "12.34" not in form_text

    client = Client()
    client.force_login(member_b.user)
    page = client.get(reverse("transaction-edit", args=(purchase.pk,)))
    assert page.status_code == 200
    body = page.content.decode()
    assert "Synthetic A private refund XYZ" not in body
    assert "2026-03-17" not in body
    assert "12.34" not in body

    response = client.post(
        reverse("transaction-split", args=(purchase.pk,)),
        {
            "part_count": "2",
            "part_0_category": str(groceries.pk),
            "part_0_amount": "-60.00",
            "part_1_category": str(housing.pk),
            "part_1_amount": "-30.00",
        },
    )
    assert response.status_code == 302
    assert "Synthetic A private refund XYZ" not in response.content.decode()
    purchase.refresh_from_db()
    refund.refresh_from_db()
    link = RefundLink.objects.get(refund=refund)
    assert purchase.category_source == Transaction.CategorySource.SPLIT
    assert refund.category_id == groceries.pk
    assert link.original_part.category_id == groceries.pk


@pytest.mark.django_db
def test_splitting_suggested_transfer_leg_drops_pair_and_allows_rematch():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    extra = make_account(owner, name="Synthetic Extra", account_type=Account.Type.SAVINGS)
    outflow_a = make_transaction(owner, checking, amount_minor=-4000, description="Synthetic moved out a")
    inflow = make_transaction(owner, savings, amount_minor=4000, description="Synthetic transfer in")
    outflow_b = make_transaction(owner, extra, amount_minor=-4000, description="Synthetic moved out b")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get(status=TransferPair.Status.SUGGESTED)
    occupied = {pair.leg_a_id, pair.leg_b_id}
    split_leg = outflow_a if outflow_a.pk in occupied else outflow_b
    leftover = outflow_b if split_leg.pk == outflow_a.pk else outflow_a

    split_transaction(
        owner,
        split_leg.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -2500},
            {"category_id": housing.pk, "amount_minor": -1500},
        ),
    )

    pair.refresh_from_db()
    assert pair.status == TransferPair.Status.UNDONE
    rematch = TransferPair.objects.exclude(pk=pair.pk).get()
    rematch_ids = {rematch.leg_a_id, rematch.leg_b_id}
    assert rematch.status in (TransferPair.Status.SUGGESTED, TransferPair.Status.AUTO_MARKED)
    assert rematch_ids == {inflow.pk, leftover.pk}



@pytest.mark.django_db
def test_link_refund_page_requires_a_part_of_the_split_purchase():
    from finance.category_services import SPLIT_PART_MISMATCH

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    account = make_account(owner)
    purchase = make_transaction(owner, account, amount_minor=-9000, description="Synthetic split purchase")
    other = make_transaction(owner, account, amount_minor=-4000, description="Synthetic other purchase")
    refund = make_transaction(owner, account, amount_minor=1000, description="Synthetic refund")
    parts = ({"category_id": groceries.pk, "amount_minor": -4000}, {"category_id": housing.pk, "amount_minor": -5000})
    split_transaction(owner, purchase.pk, parts)
    split_transaction(
        owner,
        other.pk,
        ({"category_id": groceries.pk, "amount_minor": -1000}, {"category_id": housing.pk, "amount_minor": -3000}),
    )
    client = Client()
    client.force_login(owner.user)
    url = reverse("transaction-link-refund", args=(refund.pk,))

    missing = client.post(url, {"original": str(purchase.pk)})
    assert missing.status_code == 200
    assert SPLIT_PART_REQUIRED.encode() in missing.content
    wrong = client.post(url, {"original": str(purchase.pk), "original_part": str(other.splits.first().pk)})
    assert wrong.status_code == 200
    assert SPLIT_PART_MISMATCH.encode() in wrong.content
    assert not RefundLink.objects.filter(refund=refund).exists()

    housing_part = purchase.splits.get(category=housing)
    linked = client.post(url, {"original": str(purchase.pk), "original_part": str(housing_part.pk)})
    assert linked.status_code == 302
    refund.refresh_from_db()
    assert refund.category_id == housing.pk
    assert RefundLink.objects.get(refund=refund).original_part_id == housing_part.pk


@pytest.mark.django_db
def test_split_page_assigns_a_visible_refund_to_the_chosen_part():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    account = make_account(owner)
    purchase = make_transaction(owner, account, amount_minor=-8000, description="Synthetic purchase")
    refund = make_transaction(owner, account, amount_minor=2000, description="Synthetic refund")
    assign_category(owner, purchase.pk, groceries.pk)
    link_refund(owner, refund.pk, purchase.pk)
    client = Client()
    client.force_login(owner.user)

    edit = client.get(reverse("transaction-edit", args=(purchase.pk,)))
    assert f'name="refund_{refund.pk}_part"'.encode() in edit.content

    response = client.post(
        reverse("transaction-split", args=(purchase.pk,)),
        {
            "part_count": "2",
            "part_0_category": str(groceries.pk),
            "part_0_amount": "-50.00",
            "part_1_category": str(housing.pk),
            "part_1_amount": "-30.00",
            f"refund_{refund.pk}_part": "1",
        },
    )
    assert response.status_code == 302
    refund.refresh_from_db()
    assert refund.category_id == housing.pk
    assert RefundLink.objects.get(refund=refund).original_part.category_id == housing.pk


@pytest.mark.django_db
def test_part_category_page_recategorizes_one_part_and_its_refund():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    purchase = make_transaction(owner, account, amount_minor=-9000)
    refund = make_transaction(owner, account, amount_minor=1000, description="Synthetic refund")
    split_transaction(
        owner,
        purchase.pk,
        ({"category_id": groceries.pk, "amount_minor": -4000}, {"category_id": housing.pk, "amount_minor": -5000}),
    )
    groceries_part = purchase.splits.get(category=groceries)
    link_refund(owner, refund.pk, purchase.pk, original_part_id=groceries_part.pk)
    client = Client()
    client.force_login(owner.user)

    field = f"part{groceries_part.pk}-category"
    edit = client.get(reverse("transaction-edit", args=(purchase.pk,)))
    assert f'name="{field}"'.encode() in edit.content
    response = client.post(
        reverse("transaction-split-part-category", args=(purchase.pk, groceries_part.pk)),
        {field: str(dining.pk)},
    )

    assert response.status_code == 302
    groceries_part.refresh_from_db()
    refund.refresh_from_db()
    assert groceries_part.category_id == dining.pk
    assert refund.category_id == dining.pk
    totals = income_and_spending_totals(owner)
    assert totals.spending_by_category_id[dining.pk] == 3000
    assert totals.spending_by_category_id[housing.pk] == 5000
