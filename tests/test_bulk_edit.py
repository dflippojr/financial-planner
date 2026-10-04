from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.bulk_edit_services import (
    ACTION_ADD_TAGS,
    ACTION_APPEND_NOTE,
    ACTION_CATEGORY,
    ACTION_REMOVE_TAGS,
    CHANGED_SINCE,
    SKIP_INVESTMENT,
    SKIP_NOTE_TOO_LONG,
    SKIP_SPLIT,
    SKIP_TRANSFER,
    UNDO_UNAVAILABLE,
    apply_bulk_edit,
    preview_bulk_edit,
    undo_bulk_edit,
)
from finance.category_services import (
    assign_category,
    ensure_household_categories,
    link_refund,
    split_transaction,
)
from finance.category_suggestion_services import accept_suggestion, snapshot_hash
from finance.models import (
    Account,
    BulkEditUndo,
    CategorySuggestion,
    Household,
    ImportBatch,
    Membership,
    Person,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)
from finance.rule_services import apply_enabled_rules_to_transactions, save_category_rule
from finance.tag_services import add_tag, set_transaction_note_and_tags


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
    transaction_date=date(2026, 1, 10),
    amount_minor=-1000,
    description="Synthetic row",
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
        description=description,
        kind=kind,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"Description": description},
    )


def matching_qs(person):
    return Transaction.objects.visible_to(person).filter(status=Transaction.Status.ACTIVE)


def make_pair(left, right, kind):
    first, second = (left, right) if left.pk < right.pk else (right, left)
    return TransferPair.objects.create(
        leg_a=first,
        leg_b=second,
        status=TransferPair.Status.CONFIRMED,
        kind=kind,
        confidence=TransferPair.Confidence.HIGH,
        reasons=["synthetic"],
    )


def groceries(household):
    return household.categories.get(name="Groceries")


def dining(household):
    return household.categories.get(name="Dining")


@pytest.mark.django_db
def test_list_has_row_checkboxes_and_select_matching():
    owner = make_person("owner")
    account = make_account(owner)
    txn = make_transaction(owner, account)
    client = Client()
    client.force_login(owner.user)
    response = client.get(reverse("transaction-list"))
    body = response.content.decode()
    assert f'name="transaction_id" value="{txn.pk}"' in body
    assert "Select all on this page" in body
    assert "Select all 1 matching" in body
    assert "Preview bulk edit" in body


@pytest.mark.django_db
def test_preview_counts_match_applied_category_changes():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    first = make_transaction(owner, account, description="Synthetic one", fingerprint="1" * 64)
    second = make_transaction(owner, account, description="Synthetic two", fingerprint="2" * 64)
    category = groceries(household)
    ids = [first.pk, second.pk]
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=ids,
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=category.pk,
    )
    applied, _undo = apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=ids,
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=category.pk,
    )
    first.refresh_from_db()
    second.refresh_from_db()
    assert preview.eligible_ids == applied.eligible_ids
    assert set(preview.eligible_ids) == {first.pk, second.pk}
    assert first.category_id == category.pk
    assert first.category_source == Transaction.CategorySource.MANUAL
    assert second.category_source == Transaction.CategorySource.MANUAL
    assert TransactionCorrectionHistory.objects.filter(field_name="category").count() == 2


@pytest.mark.django_db
def test_skip_reasons_for_category_and_investment():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    cash = make_transaction(owner, checking, description="Synthetic cash", fingerprint="a" * 64)
    split = make_transaction(owner, checking, amount_minor=-5000, description="Synthetic split", fingerprint="b" * 64)
    split_transaction(
        owner,
        split.pk,
        (
            {"category_id": groceries(household).pk, "amount_minor": -3000},
            {"category_id": dining(household).pk, "amount_minor": -2000},
        ),
    )
    out_leg = make_transaction(owner, checking, amount_minor=-2200, description="Synthetic transfer out", fingerprint="c" * 64)
    in_leg = make_transaction(owner, savings, amount_minor=2200, description="Synthetic transfer in", fingerprint="d" * 64)
    make_pair(out_leg, in_leg, TransferPair.Kind.TRANSFER)
    card_out = make_transaction(owner, checking, amount_minor=-3300, description="Synthetic card out", fingerprint="e" * 64)
    card_in = make_transaction(owner, savings, amount_minor=3300, description="Synthetic card in", fingerprint="f" * 64)
    make_pair(card_out, card_in, TransferPair.Kind.CARD_PAYMENT)
    investment = make_transaction(
        owner,
        checking,
        description="Synthetic dividend",
        fingerprint="g" * 64,
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
    )
    ids = [cash.pk, split.pk, out_leg.pk, card_out.pk, investment.pk]
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=ids,
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=groceries(household).pk,
    )
    assert preview.skip_counts[SKIP_SPLIT] == 1
    assert preview.skip_counts[SKIP_TRANSFER] == 2
    assert preview.skip_counts[SKIP_INVESTMENT] == 1
    assert preview.eligible_ids == [cash.pk]
    apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=ids,
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=groceries(household).pk,
    )
    cash.refresh_from_db()
    split.refresh_from_db()
    out_leg.refresh_from_db()
    investment.refresh_from_db()
    assert cash.category_id == groceries(household).pk
    assert split.category_source == Transaction.CategorySource.SPLIT
    assert out_leg.category_id is None
    assert investment.category_id is None


@pytest.mark.django_db
def test_investment_is_skipped_for_tags_and_notes():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    cash = make_transaction(owner, account, description="Synthetic cash", fingerprint="h" * 64)
    investment = make_transaction(
        owner,
        account,
        description="Synthetic buy",
        fingerprint="i" * 64,
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
    )
    tag = add_tag(owner, "Synthetic tag")
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[cash.pk, investment.pk],
        select_matching=False,
        action=ACTION_ADD_TAGS,
        tag_ids=[tag.pk],
    )
    assert preview.skip_counts[SKIP_INVESTMENT] == 1
    assert preview.eligible_ids == [cash.pk]
    apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[cash.pk, investment.pk],
        select_matching=False,
        action=ACTION_APPEND_NOTE,
        note_line="Synthetic note line",
    )
    cash.refresh_from_db()
    investment.refresh_from_db()
    assert cash.note == "Synthetic note line"
    assert investment.note == ""


@pytest.mark.django_db
def test_forged_private_id_is_omitted_from_preview_and_apply():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Owner private")
    shared_txn = make_transaction(owner, shared, description="Synthetic shared", fingerprint="j" * 64)
    private_txn = make_transaction(owner, private, description="Synthetic secret", fingerprint="k" * 64)
    client = Client()
    client.force_login(member.user)
    preview = client.post(
        reverse("transaction-bulk-preview"),
        {
            "action": ACTION_CATEGORY,
            "transaction_id": [str(shared_txn.pk), str(private_txn.pk)],
            "category": str(groceries(household).pk),
        },
    )
    body = preview.content.decode()
    assert "Synthetic secret" not in body
    assert f'name="transaction_id" value="{private_txn.pk}"' not in body
    assert "1 selected" in body
    assert "1 will be updated" in body
    apply = client.post(
        reverse("transaction-bulk-apply"),
        {
            "action": ACTION_CATEGORY,
            "transaction_id": [str(shared_txn.pk), str(private_txn.pk)],
            "category": str(groceries(household).pk),
        },
    )
    assert apply.status_code == 302
    shared_txn.refresh_from_db()
    private_txn.refresh_from_db()
    assert shared_txn.category_id == groceries(household).pk
    assert private_txn.category_id is None
    member_page = client.get(reverse("transaction-list"))
    assert "Synthetic secret" not in member_page.content.decode()


@pytest.mark.django_db
def test_undo_restores_and_refuses_when_changed():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account)
    tag = add_tag(owner, "Keep")
    set_transaction_note_and_tags(owner, txn.pk, note="First line", tag_ids=[tag.pk])
    _preview, undo = apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_APPEND_NOTE,
        note_line="Second line",
    )
    txn.refresh_from_db()
    assert txn.note == "First line\nSecond line"
    undo_bulk_edit(owner, undo.pk)
    txn.refresh_from_db()
    assert txn.note == "First line"
    _preview, undo2 = apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=groceries(household).pk,
    )
    assign_category(owner, txn.pk, dining(household).pk)
    with pytest.raises(ValidationError, match=CHANGED_SINCE):
        undo_bulk_edit(owner, undo2.pk)
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk


@pytest.mark.django_db
def test_undo_expires_and_other_member_cannot_see_it():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    account = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    txn = make_transaction(owner, account)
    _preview, undo = apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=groceries(household).pk,
    )
    BulkEditUndo.objects.filter(pk=undo.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    with pytest.raises(ValidationError, match=UNDO_UNAVAILABLE):
        undo_bulk_edit(owner, undo.pk)
    with pytest.raises(PermissionDenied):
        undo_bulk_edit(member, undo.pk)


@pytest.mark.django_db
def test_rules_and_ai_do_not_override_bulk_set_category():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, description="SYNTHETIC KROGER")
    apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=dining(household).pk,
    )
    save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries(household).pk,
        priority=0,
    )
    apply_enabled_rules_to_transactions(owner, [txn])
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk
    assert txn.category_source == Transaction.CategorySource.MANUAL
    suggestion = CategorySuggestion.objects.create(
        member=owner,
        transaction=txn,
        category=groceries(household),
        provider="agent_harness",
        backend="local",
        snapshot_hash=snapshot_hash(txn),
    )
    accept_suggestion(owner, suggestion.pk)
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk
    assert txn.category_source == Transaction.CategorySource.MANUAL


@pytest.mark.django_db
def test_add_and_remove_tags_and_note_too_long_skip():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account)
    keep = add_tag(owner, "Keep")
    drop = add_tag(owner, "Drop")
    apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_ADD_TAGS,
        tag_ids=[keep.pk, drop.pk],
    )
    txn.refresh_from_db()
    assert set(txn.tags.values_list("name", flat=True)) == {"Keep", "Drop"}
    apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_REMOVE_TAGS,
        tag_ids=[drop.pk],
    )
    txn.refresh_from_db()
    assert set(txn.tags.values_list("name", flat=True)) == {"Keep"}
    txn.note = "x" * 1995
    txn.save(update_fields=("note", "updated_at"))
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=False,
        action=ACTION_APPEND_NOTE,
        note_line="too-long-line",
    )
    assert preview.skip_counts[SKIP_NOTE_TOO_LONG] == 1
    assert preview.eligible_ids == []


@pytest.mark.django_db
def test_select_matching_respects_cap(monkeypatch):
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, description="Synthetic a", fingerprint="m" * 64)
    make_transaction(owner, account, description="Synthetic b", fingerprint="n" * 64)
    make_transaction(owner, account, description="Synthetic c", fingerprint="o" * 64)
    monkeypatch.setattr("finance.bulk_edit_services.BULK_EDIT_CAP", 2)
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[],
        select_matching=True,
        action=ACTION_CATEGORY,
        category_id=groceries(household).pk,
    )
    assert preview.over_cap is True
    assert preview.eligible_ids == []
    with pytest.raises(ValidationError):
        apply_bulk_edit(
            owner,
            matching=matching_qs(owner),
            transaction_ids=[],
            select_matching=True,
            action=ACTION_CATEGORY,
            category_id=groceries(household).pk,
        )


@pytest.mark.django_db
def test_uncategorized_and_select_matching_under_cap():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account)
    assign_category(owner, txn.pk, groceries(household).pk)
    apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[txn.pk],
        select_matching=True,
        action=ACTION_CATEGORY,
        category_id=None,
    )
    txn.refresh_from_db()
    assert txn.category_id is None
    assert txn.category_source == Transaction.CategorySource.MANUAL


@pytest.mark.django_db
def test_http_preview_apply_undo_flow():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account)
    client = Client()
    client.force_login(owner.user)
    preview = client.post(
        reverse("transaction-bulk-preview"),
        {"action": ACTION_CATEGORY, "transaction_id": str(txn.pk), "category": str(dining(household).pk)},
    )
    assert preview.status_code == 200
    assert b"1 will be updated" in preview.content
    apply = client.post(
        reverse("transaction-bulk-apply"),
        {"action": ACTION_CATEGORY, "transaction_id": str(txn.pk), "category": str(dining(household).pk)},
        follow=True,
    )
    assert b"Updated 1 transaction" in apply.content
    assert b"Undo" in apply.content
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk
    undo_id = BulkEditUndo.objects.get().pk
    undo = client.post(reverse("transaction-bulk-undo", args=(undo_id,)), follow=True)
    assert b"Bulk edit undone" in undo.content
    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_bulk_category_propagates_to_linked_refunds_and_skips_refund_legs():
    from finance.bulk_edit_services import SKIP_LINKED_REFUND

    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    purchase = make_transaction(
        owner, account, amount_minor=-4000, description="Synthetic store", fingerprint="p" * 64
    )
    refund = make_transaction(
        owner, account, amount_minor=1500, description="Synthetic store refund", fingerprint="r" * 64
    )
    grocery = groceries(household)
    dine = dining(household)
    assign_category(owner, purchase.pk, grocery.pk)
    link_refund(owner, refund.pk, purchase.pk)
    refund.refresh_from_db()
    assert refund.category_id == grocery.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[purchase.pk, refund.pk],
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=dine.pk,
    )
    assert preview.eligible_ids == [purchase.pk]
    assert preview.skip_counts[SKIP_LINKED_REFUND] == 1
    _applied, undo = apply_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[purchase.pk, refund.pk],
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=dine.pk,
    )
    purchase.refresh_from_db()
    refund.refresh_from_db()
    assert purchase.category_id == dine.pk
    assert purchase.category_source == Transaction.CategorySource.MANUAL
    assert refund.category_id == dine.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED
    undo_bulk_edit(owner, undo.pk)
    purchase.refresh_from_db()
    refund.refresh_from_db()
    assert purchase.category_id == grocery.pk
    assert purchase.category_source == Transaction.CategorySource.MANUAL
    assert refund.category_id == grocery.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED


@pytest.mark.django_db
def test_apply_writes_previewed_ids_not_rows_imported_later():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    first = make_transaction(owner, account, description="Synthetic first", fingerprint="1" * 64)
    client = Client()
    client.force_login(owner.user)
    preview = client.post(
        reverse("transaction-bulk-preview"),
        {
            "action": ACTION_CATEGORY,
            "select_matching": "on",
            "category": str(dining(household).pk),
        },
    )
    body = preview.content.decode()
    assert f'name="transaction_id" value="{first.pk}"' in body
    assert 'name="select_matching"' not in body
    second = make_transaction(owner, account, description="Synthetic later", fingerprint="2" * 64)
    apply = client.post(
        reverse("transaction-bulk-apply"),
        {
            "action": ACTION_CATEGORY,
            "select_matching": "on",
            "transaction_id": str(first.pk),
            "eligible_id": str(first.pk),
            "category": str(dining(household).pk),
        },
        follow=True,
    )
    assert b"Updated 1 transaction" in apply.content
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.category_id == dining(household).pk
    assert second.category_id is None


@pytest.mark.django_db
def test_apply_refuses_when_a_previewed_row_is_no_longer_eligible():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    first = make_transaction(owner, account, amount_minor=-5000, description="Synthetic one", fingerprint="s" * 64)
    second = make_transaction(owner, account, description="Synthetic two", fingerprint="t" * 64)
    dine = dining(household)
    preview = preview_bulk_edit(
        owner,
        matching=matching_qs(owner),
        transaction_ids=[first.pk, second.pk],
        select_matching=False,
        action=ACTION_CATEGORY,
        category_id=dine.pk,
    )
    assert preview.eligible_ids == sorted([first.pk, second.pk])
    split_transaction(
        owner,
        first.pk,
        (
            {"category_id": groceries(household).pk, "amount_minor": -3000},
            {"category_id": dine.pk, "amount_minor": -2000},
        ),
    )
    with pytest.raises(ValidationError, match="No eligible transactions to update"):
        apply_bulk_edit(
            owner,
            matching=matching_qs(owner),
            transaction_ids=[first.pk, second.pk],
            select_matching=False,
            action=ACTION_CATEGORY,
            category_id=dine.pk,
            expected_eligible_ids=preview.eligible_ids,
        )
    second.refresh_from_db()
    assert second.category_id is None
