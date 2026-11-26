"""Refusal branches of the member-scoped services (#769 coverage on the renamed helpers).

Each test drives one public service into a refusal it must give: a row the member
cannot see, a category outside the member's household, a member with no household,
or a row that leaves the member's view between the first read and the lock. The
last kind is simulated by wrapping the module's lock helper, the only place the
race can be observed.
"""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile

from finance import splits
from finance.bulk_edit_services import (
    ACTION_ADD_TAGS,
    ACTION_APPEND_NOTE,
    ACTION_CATEGORY,
    apply_bulk_edit,
    preview_bulk_edit,
    undo_bulk_edit,
)
from finance.category_services import assign_category, ensure_household_categories
from finance.fund_services import account_fund_summary, fund_history, resolve_shortfall, undo_draw_down
from finance.models import (
    Account,
    FundEntry,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringSeries,
    SavingsGoal,
    Transaction,
    TransactionSplit,
)
from finance.receipt_services import attach_receipt, remove_receipt, stored_receipt_path
from finance.recurring_services import (
    confirm_recurring_series,
    create_manual_series,
    keep_cancelled_recurring_series,
    remove_recurring_member,
)
from finance.splits import assign_split_part_category, split_transaction, unsplit_transaction
from finance.tag_services import add_tag, archive_tag, rename_tag, set_transaction_note_and_tags
from tests.test_recurring_manual_create import charges

PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 1, 15)
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, household=None, name="Synthetic Checking"):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD if household else Account.Scope.PRIVATE,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if household else "",
    )


def make_transaction(owner, account, *, amount_minor=-10000, fingerprint=None):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="c" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or f"{account.pk}-{amount_minor}-denial".encode().hex().ljust(64, "b")[:64]
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=amount_minor,
        currency="USD",
        description="Synthetic warehouse",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"Description": "Synthetic warehouse"},
    )


def move_out_of_view_after_lock(monkeypatch, module, txn, account):
    """Wrap the module's lock helper so the row moves to another member's private account after locking."""
    original = module.lock_owned_transactions

    def lock_then_move(rows):
        locked = original(rows)
        Transaction.objects.filter(pk=txn.pk).update(account=account)
        return locked

    monkeypatch.setattr(module, "lock_owned_transactions", lock_then_move)


@pytest.fixture
def household_pair(db):
    owner = make_person("owner")
    outsider = make_person("outsider")
    household = make_household(owner)
    other_household = make_household(outsider, name="Other Household")
    return {
        "owner": owner,
        "outsider": outsider,
        "groceries": household.categories.get(name="Groceries"),
        "housing": household.categories.get(name="Housing"),
        "foreign_groceries": other_household.categories.get(name="Groceries"),
        "outsider_account": make_account(outsider, name="Outsider Private"),
        "owner_account": make_account(owner, name="Owner Private"),
    }


@pytest.fixture
def split_txn(household_pair):
    owner = household_pair["owner"]
    txn = make_transaction(owner, household_pair["owner_account"])
    split_transaction(
        owner,
        txn.pk,
        (
            {"category_id": household_pair["groceries"].pk, "amount_minor": -6000},
            {"category_id": household_pair["housing"].pk, "amount_minor": -4000},
        ),
    )
    txn.refresh_from_db()
    return txn


# Splits --------------------------------------------------------------------


@pytest.mark.django_db
def test_split_refuses_a_transaction_the_member_cannot_see(household_pair):
    txn = make_transaction(household_pair["outsider"], household_pair["outsider_account"])
    with pytest.raises(PermissionDenied):
        split_transaction(
            household_pair["owner"],
            txn.pk,
            ({"category_id": household_pair["groceries"].pk, "amount_minor": -10000},),
        )


@pytest.mark.django_db
def test_split_refuses_a_category_outside_the_members_household(household_pair):
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    with pytest.raises(PermissionDenied):
        split_transaction(
            household_pair["owner"],
            txn.pk,
            (
                {"category_id": household_pair["groceries"].pk, "amount_minor": -6000},
                {"category_id": household_pair["foreign_groceries"].pk, "amount_minor": -4000},
            ),
        )


@pytest.mark.django_db
def test_unsplit_refuses_a_transaction_the_member_cannot_see(household_pair):
    txn = make_transaction(household_pair["outsider"], household_pair["outsider_account"])
    with pytest.raises(PermissionDenied):
        unsplit_transaction(household_pair["owner"], txn.pk, None)


@pytest.mark.django_db
def test_unsplit_refuses_a_transaction_that_is_not_split(household_pair):
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    assign_category(household_pair["owner"], txn.pk, household_pair["groceries"].pk)
    with pytest.raises(PermissionDenied):
        unsplit_transaction(household_pair["owner"], txn.pk, None)


@pytest.mark.django_db
def test_unsplit_refuses_a_category_outside_the_members_household(household_pair, split_txn):
    with pytest.raises(PermissionDenied):
        unsplit_transaction(household_pair["owner"], split_txn.pk, household_pair["foreign_groceries"].pk)


@pytest.mark.django_db
def test_unsplit_refuses_when_the_row_leaves_the_members_view_before_the_lock(
    monkeypatch, household_pair, split_txn
):
    move_out_of_view_after_lock(monkeypatch, splits, split_txn, household_pair["outsider_account"])
    with pytest.raises(PermissionDenied):
        unsplit_transaction(household_pair["owner"], split_txn.pk, None)


@pytest.mark.django_db
def test_part_category_refuses_an_unknown_part(household_pair):
    with pytest.raises(PermissionDenied):
        assign_split_part_category(household_pair["owner"], 999_999, household_pair["housing"].pk)


@pytest.mark.django_db
def test_part_category_refuses_a_part_of_a_transaction_the_member_cannot_see(household_pair):
    outsider = household_pair["outsider"]
    outsider_household = Membership.objects.get(person=outsider).household
    outsider_txn = make_transaction(outsider, household_pair["outsider_account"])
    split_transaction(
        outsider,
        outsider_txn.pk,
        (
            {"category_id": outsider_household.categories.get(name="Groceries").pk, "amount_minor": -6000},
            {"category_id": outsider_household.categories.get(name="Housing").pk, "amount_minor": -4000},
        ),
    )
    part = TransactionSplit.objects.filter(transaction=outsider_txn).first()
    with pytest.raises(PermissionDenied):
        assign_split_part_category(household_pair["owner"], part.pk, household_pair["housing"].pk)


@pytest.mark.django_db
def test_part_category_refuses_a_transaction_that_is_not_split(household_pair, split_txn):
    Transaction.objects.filter(pk=split_txn.pk).update(category_source=Transaction.CategorySource.MANUAL)
    part = TransactionSplit.objects.filter(transaction=split_txn).first()
    with pytest.raises(PermissionDenied):
        assign_split_part_category(household_pair["owner"], part.pk, household_pair["housing"].pk)


@pytest.mark.django_db
def test_part_category_refuses_a_category_outside_the_members_household(household_pair, split_txn):
    part = TransactionSplit.objects.filter(transaction=split_txn).first()
    with pytest.raises(PermissionDenied):
        assign_split_part_category(household_pair["owner"], part.pk, household_pair["foreign_groceries"].pk)


@pytest.mark.django_db
def test_part_category_refuses_when_the_parent_leaves_the_view_before_the_lock(
    monkeypatch, household_pair, split_txn
):
    part = TransactionSplit.objects.filter(transaction=split_txn).first()
    move_out_of_view_after_lock(monkeypatch, splits, split_txn, household_pair["outsider_account"])
    with pytest.raises(PermissionDenied):
        assign_split_part_category(household_pair["owner"], part.pk, household_pair["housing"].pk)


# Tags ----------------------------------------------------------------------


@pytest.mark.django_db
def test_tag_writes_need_a_household():
    loner = make_person("loner")
    with pytest.raises(PermissionDenied):
        add_tag(loner, "Trip")
    with pytest.raises(PermissionDenied):
        rename_tag(loner, 1, "Trip")
    with pytest.raises(PermissionDenied):
        archive_tag(loner, 1)


@pytest.mark.django_db
def test_note_and_tags_refuse_a_transaction_the_member_cannot_see(household_pair):
    txn = make_transaction(household_pair["outsider"], household_pair["outsider_account"])
    with pytest.raises(PermissionDenied):
        set_transaction_note_and_tags(household_pair["owner"], txn.pk, note="", tag_ids=[])


@pytest.mark.django_db
def test_new_tag_or_existing_tags_need_a_household_for_a_member_without_one():
    loner = make_person("loner")
    account = make_account(loner, name="Loner Private")
    txn = make_transaction(loner, account)
    with pytest.raises(PermissionDenied):
        set_transaction_note_and_tags(loner, txn.pk, note="", tag_ids=[], new_tag_name="Trip")
    with pytest.raises(PermissionDenied):
        set_transaction_note_and_tags(loner, txn.pk, note="", tag_ids=[1], new_tag_name="")


# Funds ---------------------------------------------------------------------


def make_goal(owner, account, *, household=None, name="Synthetic Insurance"):
    return SavingsGoal.objects.create(
        owner=owner,
        name=name,
        target_amount_minor=120_000,
        target_date=date(2027, 4, 30),
        scope=SavingsGoal.Scope.HOUSEHOLD if household else SavingsGoal.Scope.PRIVATE,
        household=household,
        linked_account=account,
        share_of_account=True,
    )


@pytest.mark.django_db
def test_fund_summary_refuses_an_account_the_member_cannot_see(household_pair):
    with pytest.raises(PermissionDenied):
        account_fund_summary(household_pair["owner"], household_pair["outsider_account"], today=TODAY)


@pytest.mark.django_db
def test_shortfall_refuses_an_account_the_member_cannot_see(household_pair):
    with pytest.raises(PermissionDenied):
        resolve_shortfall(household_pair["owner"], household_pair["outsider_account"], [], today=TODAY)


@pytest.mark.django_db
def test_fund_history_refuses_a_goal_the_member_cannot_see(household_pair):
    goal = make_goal(household_pair["outsider"], household_pair["outsider_account"])
    with pytest.raises(PermissionDenied):
        fund_history(household_pair["owner"], goal)


@pytest.mark.django_db
def test_undo_draw_down_refuses_a_fund_the_member_cannot_see(household_pair):
    outsider = household_pair["outsider"]
    goal = make_goal(outsider, household_pair["outsider_account"])
    entry = FundEntry.objects.create(
        goal=goal,
        kind=FundEntry.Kind.DRAW_DOWN,
        reason="planned",
        amount_minor=-5000,
        entry_date=TODAY,
        occurrence_date=TODAY,
        actor=outsider,
    )
    with pytest.raises(PermissionDenied):
        undo_draw_down(household_pair["owner"], entry)


# Receipts ------------------------------------------------------------------


@pytest.mark.django_db
def test_stored_receipt_path_refuses_a_name_that_is_not_ours():
    with pytest.raises(PermissionDenied):
        stored_receipt_path("../escape.jpg")


@pytest.mark.django_db
def test_receipt_removal_refuses_a_receipt_that_is_not_on_the_transaction(household_pair):
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    with pytest.raises(PermissionDenied):
        remove_receipt(household_pair["owner"], txn.pk, 999_999)


@pytest.mark.django_db
def test_receipt_actions_need_a_member_record(household_pair):
    user = get_user_model().objects.create_user(username="no-person", password=PASSWORD)
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    with pytest.raises(PermissionDenied):
        remove_receipt(user, txn.pk, 1)


@pytest.mark.django_db
def test_attach_refuses_a_transaction_the_member_cannot_see(household_pair):
    txn = make_transaction(household_pair["outsider"], household_pair["outsider_account"])
    upload = SimpleUploadedFile("synthetic.jpg", JPEG)
    with pytest.raises(PermissionDenied):
        attach_receipt(household_pair["owner"], txn.pk, upload)


# Bulk edits ----------------------------------------------------------------


@pytest.mark.django_db
def test_bulk_category_refuses_a_category_outside_the_members_household(household_pair):
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    with pytest.raises(PermissionDenied):
        preview_bulk_edit(
            household_pair["owner"],
            matching=Transaction.objects.none(),
            transaction_ids=[txn.pk],
            select_matching=False,
            action=ACTION_CATEGORY,
            category_id=household_pair["foreign_groceries"].pk,
        )


@pytest.mark.django_db
def test_bulk_tags_refuse_a_tag_outside_the_members_household(household_pair):
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    foreign_tag = add_tag(household_pair["outsider"], "Trip")
    with pytest.raises(PermissionDenied):
        preview_bulk_edit(
            household_pair["owner"],
            matching=Transaction.objects.none(),
            transaction_ids=[txn.pk],
            select_matching=False,
            action=ACTION_ADD_TAGS,
            tag_ids=[foreign_tag.pk],
        )


@pytest.mark.django_db
def test_bulk_actions_need_a_member_record(household_pair):
    user = get_user_model().objects.create_user(username="bulk-no-person", password=PASSWORD)
    txn = make_transaction(household_pair["owner"], household_pair["owner_account"])
    with pytest.raises(PermissionDenied):
        preview_bulk_edit(
            user,
            matching=Transaction.objects.none(),
            transaction_ids=[txn.pk],
            select_matching=False,
            action=ACTION_APPEND_NOTE,
            note_line="Synthetic note",
        )
    with pytest.raises(PermissionDenied):
        apply_bulk_edit(
            user,
            matching=Transaction.objects.none(),
            transaction_ids=[txn.pk],
            action=ACTION_APPEND_NOTE,
            note_line="Synthetic note",
        )
    with pytest.raises(PermissionDenied):
        undo_bulk_edit(user, 1)


# Recurring series ----------------------------------------------------------


@pytest.mark.django_db
def test_a_confirmed_series_cannot_be_confirmed_again(household_pair):
    owner = household_pair["owner"]
    rows = charges(owner, household_pair["owner_account"])
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    with pytest.raises(PermissionDenied):
        confirm_recurring_series(owner, series.pk)


@pytest.mark.django_db
def test_keep_cancelled_needs_a_cancelled_series(household_pair):
    owner = household_pair["owner"]
    rows = charges(owner, household_pair["owner_account"])
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    with pytest.raises(PermissionDenied):
        keep_cancelled_recurring_series(owner, series.pk)


@pytest.mark.django_db
def test_keep_cancelled_refuses_another_members_series(household_pair):
    owner = household_pair["owner"]
    outsider = household_pair["outsider"]
    outsider_rows = charges(outsider, household_pair["outsider_account"], description="Synthetic Water")
    series = create_manual_series(outsider, "Water", "monthly", [row.pk for row in outsider_rows])
    with pytest.raises(PermissionDenied):
        keep_cancelled_recurring_series(owner, series.pk)


@pytest.mark.django_db
def test_keep_cancelled_refuses_a_series_that_is_no_longer_confirmed(household_pair):
    owner = household_pair["owner"]
    rows = charges(owner, household_pair["owner_account"])
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    RecurringSeries.objects.filter(pk=series.pk).update(status=RecurringSeries.Status.DISMISSED)
    with pytest.raises(PermissionDenied):
        keep_cancelled_recurring_series(owner, series.pk)


@pytest.mark.django_db
def test_removing_a_member_refuses_an_unknown_series(household_pair):
    owner = household_pair["owner"]
    txn = make_transaction(owner, household_pair["owner_account"])
    with pytest.raises(PermissionDenied):
        remove_recurring_member(owner, 999_999, txn.pk)


@pytest.mark.django_db
def test_removing_a_member_refuses_a_series_the_member_cannot_see(household_pair):
    owner = household_pair["owner"]
    outsider = household_pair["outsider"]
    rows = charges(owner, household_pair["owner_account"])
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    with pytest.raises(PermissionDenied):
        remove_recurring_member(outsider, series.pk, rows[0].pk)


@pytest.mark.django_db
def test_removing_a_member_refuses_a_transaction_outside_the_series(household_pair):
    owner = household_pair["owner"]
    rows = charges(owner, household_pair["owner_account"])
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    stray = make_transaction(owner, household_pair["owner_account"], fingerprint="d" * 64)
    with pytest.raises(PermissionDenied):
        remove_recurring_member(owner, series.pk, stray.pk)
