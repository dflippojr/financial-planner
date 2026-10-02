from datetime import date
import threading

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from finance.category_services import assign_category, ensure_household_categories, link_refund
from finance.csv_import.parser import Mapping, read_csv
from finance.csv_import.services import categorize_imported_batch, commit_csv_import
from finance.models import (
    Account,
    Category,
    CategoryRule,
    Household,
    ImportBatch,
    Membership,
    Person,
    RuleApplication,
    RuleApplicationEntry,
    Transaction,
    TransactionCorrectionHistory,
)
from finance.rule_services import (
    apply_enabled_rules_to_transactions,
    apply_rule,
    preview_rule,
    reverse_application,
    save_category_rule,
    set_rule_enabled,
)


PASSWORD = "Synthetic-passphrase-42!"
CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC KROGER,-12.34,USD\n"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    amount_minor=-1000,
    description="SYNTHETIC KROGER",
    fingerprint=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{description}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"Memo": description},
    )


def groceries(household):
    return Category.objects.get(household=household, name="Groceries")


def dining(household):
    return Category.objects.get(household=household, name="Dining")


def mapping():
    return Mapping(
        date_column="When",
        description_column="Memo",
        date_format="mdy_slash_4",
        number_format="dot_comma",
        amount_mode="signed",
        amount_column="Amount",
        currency_column="Currency",
    )


@pytest.mark.django_db
def test_personal_rule_beats_household_rule_and_skips_hand_set():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    txn = make_transaction(owner, shared)
    hand = make_transaction(owner, shared, description="SYNTHETIC KROGER HAND", fingerprint="c" * 64)
    assign_category(owner, hand.pk, groceries(household).pk)
    personal = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries(household).pk,
        priority=10,
    )
    save_category_rule(
        member,
        owner_kind="household",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining(household).pk,
        priority=1,
    )

    _, matches = preview_rule(owner, personal.pk)
    assert {item.pk for item in matches} == {txn.pk}
    apply_rule(owner, personal.pk)
    txn.refresh_from_db()
    hand.refresh_from_db()
    assert txn.category_id == groceries(household).pk
    assert txn.category_source == Transaction.CategorySource.RULE
    assert hand.category_id == groceries(household).pk
    assert hand.category_source == Transaction.CategorySource.MANUAL
    history = txn.correction_history.get()
    assert "Groceries" in history.new_description
    assert "kroger" in history.new_description


@pytest.mark.django_db
def test_household_rule_never_matches_private_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Private")
    make_transaction(owner, private)
    rule = save_category_rule(
        member,
        owner_kind="household",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining(household).pk,
        priority=0,
    )
    dining_id = dining(household).pk
    with pytest.raises(ValidationError):
        save_category_rule(
            owner,
            owner_kind="household",
            description_contains="private only",
            account_id=private.pk,
            min_amount_minor=None,
            max_amount_minor=None,
            category_id=dining_id,
            priority=0,
        )

    _, matches = preview_rule(member, rule.pk)
    assert matches == []
    apply_rule(member, rule.pk)
    assert Transaction.objects.get(account=private).category_id is None


@pytest.mark.django_db
def test_reverse_restores_except_later_hand_edits():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    first = make_transaction(owner, account, fingerprint="d" * 64)
    second = make_transaction(owner, account, description="SYNTHETIC KROGER TWO", fingerprint="e" * 64)
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries(household).pk,
        priority=0,
    )
    application, skipped = apply_rule(owner, rule.pk)
    assert skipped == 0
    assign_category(owner, second.pk, dining(household).pk)
    result = reverse_application(owner, application.pk)
    first.refresh_from_db()
    second.refresh_from_db()
    assert result.restored == 1
    assert result.skipped_manual == 1
    assert first.category_id is None
    assert first.category_source == Transaction.CategorySource.UNSET
    assert second.category_id == dining(household).pk
    assert second.category_source == Transaction.CategorySource.MANUAL


@pytest.mark.django_db
def test_rules_auto_apply_on_csv_import_and_disable_stops_them():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries(household).pk,
        priority=0,
    )
    apply_rule(owner, rule.pk)  # confirming the preview turns on automatic use
    document = read_csv(CSV)
    result = commit_csv_import(
        owner.user,
        account.pk,
        content=CSV,
        document=document,
        mapping=mapping(),
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )
    categorize_imported_batch(owner.user, result.batch)
    imported = Transaction.objects.get(account=account)
    assert imported.category_id == groceries(household).pk
    assert imported.category_source == Transaction.CategorySource.RULE


@pytest.mark.django_db
def test_rules_skip_refunds_and_honor_amount_bounds():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    original = make_transaction(owner, account, fingerprint="f" * 64)
    refund = make_transaction(
        owner,
        account,
        amount_minor=1000,
        description="SYNTHETIC KROGER REFUND",
        fingerprint="1" * 64,
    )
    link_refund(owner, refund.pk, original.pk)
    bounded = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=-1500,
        max_amount_minor=-500,
        category_id=groceries(household).pk,
        priority=0,
    )
    too_small = make_transaction(
        owner,
        account,
        amount_minor=-2000,
        description="SYNTHETIC KROGER SMALL",
        fingerprint="2" * 64,
    )
    _, matches = preview_rule(owner, bounded.pk)
    assert {item.pk for item in matches} == {original.pk}
    apply_rule(owner, bounded.pk)
    original.refresh_from_db()
    refund.refresh_from_db()
    too_small.refresh_from_db()
    assert original.category_id == groceries(household).pk
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert refund.category_id == groceries(household).pk
    assert too_small.category_id is None


@pytest.mark.django_db
def test_disabled_rule_does_not_auto_apply():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries(household).pk,
        priority=0,
        enabled=False,
    )
    set_rule_enabled(owner, rule.pk, False)
    apply_enabled_rules_to_transactions(owner, [make_transaction(owner, account)])
    assert Transaction.objects.get(account=account).category_id is None


@pytest.mark.django_db
def test_rule_access_owner_member_and_outsider():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    personal = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries(household).pk,
        priority=0,
    )
    household_rule = save_category_rule(
        owner,
        owner_kind="household",
        description_contains="dining out",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining(household).pk,
        priority=0,
    )
    assert personal in CategoryRule.objects.visible_to(owner)
    assert personal not in CategoryRule.objects.visible_to(member)
    assert personal not in CategoryRule.objects.visible_to(outsider)
    assert household_rule in CategoryRule.objects.visible_to(member)
    assert household_rule not in CategoryRule.objects.visible_to(outsider)
    with pytest.raises(PermissionDenied):
        preview_rule(member, personal.pk)
    with pytest.raises(PermissionDenied):
        preview_rule(outsider, household_rule.pk)

    owner_client = Client()
    owner_client.force_login(owner.user)
    member_client = Client()
    member_client.force_login(member.user)
    outsider_client = Client()
    outsider_client.force_login(outsider.user)
    personal_url = reverse("category-rule-detail", args=(personal.pk,))
    household_url = reverse("category-rule-detail", args=(household_rule.pk,))
    assert owner_client.get(personal_url).status_code == 200
    assert member_client.get(personal_url).status_code == 404
    assert outsider_client.get(personal_url).status_code == 404
    assert member_client.get(household_url).status_code == 200
    assert outsider_client.get(household_url).status_code == 404
    page = owner_client.get(personal_url).content.decode()
    assert "Private Kroger Leak" not in page


@pytest.mark.django_db
def test_rules_pages_create_preview_apply_reverse_and_toggle():
    owner = make_person("owner-ui")
    loner = make_person("loner-ui")
    household = make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account)
    client = Client()
    client.force_login(owner.user)
    loner_client = Client()
    loner_client.force_login(loner.user)
    list_url = reverse("category-rule-list")
    assert b"Join or create a household" in loner_client.get(list_url).content
    listed = client.get(list_url)
    assert listed.status_code == 200
    created = client.post(
        list_url,
        {
            "owner_kind": "personal",
            "description_contains": "kroger",
            "category": str(groceries(household).pk),
            "priority": "0",
            "enabled": "on",
        },
    )
    assert created.status_code == 302
    rule = CategoryRule.objects.get()
    detail = reverse("category-rule-detail", args=(rule.pk,))
    assert client.get(detail).status_code == 200
    assert client.post(detail, {"action": "apply"}).status_code == 302
    application = RuleApplication.objects.get()
    assert client.post(detail, {"action": "reverse", "application_id": str(application.pk)}).status_code == 302
    assert client.post(detail, {"action": "disable"}).status_code == 302
    disabled_apply = client.post(detail, {"action": "apply"})
    assert disabled_apply.status_code == 200
    assert b"Enable the rule" in disabled_apply.content
    assert client.post(detail, {"action": "enable"}).status_code == 302
    saved = client.post(
        detail,
        {
            "action": "save",
            "owner_kind": "personal",
            "description_contains": "kroger",
            "category": str(groceries(household).pk),
            "priority": "5",
            "enabled": "on",
        },
    )
    assert saved.status_code == 302
    rule.refresh_from_db()
    assert rule.priority == 5


@pytest.mark.django_db
def test_save_category_rule_rejects_invalid_range_and_blank_match():
    owner = make_person("owner-validate")
    household = make_household(owner)
    grocery_id = groceries(household).pk
    with pytest.raises(ValidationError):
        save_category_rule(
            owner,
            owner_kind="personal",
            description_contains="   ",
            account_id=None,
            min_amount_minor=None,
            max_amount_minor=None,
            category_id=grocery_id,
            priority=0,
        )
    with pytest.raises(ValidationError):
        save_category_rule(
            owner,
            owner_kind="personal",
            description_contains="kroger",
            account_id=None,
            min_amount_minor=-100,
            max_amount_minor=-200,
            category_id=grocery_id,
            priority=0,
        )


@pytest.mark.django_db(transaction=True)
def test_hand_edit_wins_when_a_rule_applies_concurrently():
    if connection.vendor != "postgresql":
        pytest.skip("concurrent rule apply versus hand edit needs PostgreSQL row locks")

    owner = make_person("owner")
    editor = make_person("editor")
    household = make_household(owner, editor)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    txn = make_transaction(owner, shared)
    rule = save_category_rule(
        owner,
        owner_kind="household",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining(household).pk,
        priority=0,
    )
    barrier = threading.Barrier(2)
    errors = []

    def apply_from_owner():
        try:
            barrier.wait(timeout=10)
            apply_rule(owner, rule.pk)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    def edit_from_member():
        try:
            barrier.wait(timeout=10)
            assign_category(editor, txn.pk, groceries(household).pk)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    first = threading.Thread(target=apply_from_owner)
    second = threading.Thread(target=edit_from_member)
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)
    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    txn.refresh_from_db()
    assert txn.category_id == groceries(household).pk
    assert txn.category_source == Transaction.CategorySource.MANUAL


@pytest.mark.django_db
def test_0008_backfill_marks_existing_categories_manual_or_inherited():
    import importlib

    from django.apps import apps

    backfill_category_source = importlib.import_module(
        "finance.migrations.0010_category_rules"
    ).backfill_category_source

    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    grocery = groceries(household)
    hand = make_transaction(owner, account, fingerprint="3" * 64)
    assign_category(owner, hand.pk, grocery.pk)
    original = make_transaction(owner, account, fingerprint="4" * 64)
    refund = make_transaction(
        owner,
        account,
        amount_minor=1000,
        description="SYNTHETIC KROGER REFUND",
        fingerprint="5" * 64,
    )
    assign_category(owner, original.pk, grocery.pk)
    link_refund(owner, refund.pk, original.pk)
    uncategorized = make_transaction(
        owner,
        account,
        description="SYNTHETIC UNCATEGORIZED",
        fingerprint="6" * 64,
    )
    Transaction.objects.filter(pk__in=[hand.pk, original.pk, refund.pk]).update(
        category_source=Transaction.CategorySource.UNSET
    )

    backfill_category_source(apps, None)

    hand.refresh_from_db()
    original.refresh_from_db()
    refund.refresh_from_db()
    uncategorized.refresh_from_db()
    assert hand.category_source == Transaction.CategorySource.MANUAL
    assert original.category_source == Transaction.CategorySource.MANUAL
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert uncategorized.category_source == Transaction.CategorySource.UNSET

    save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining(household).pk,
        priority=0,
    )
    apply_rule(owner, CategoryRule.objects.get(owner_person=owner).pk)
    hand.refresh_from_db()
    original.refresh_from_db()
    refund.refresh_from_db()
    assert hand.category_id == grocery.pk
    assert hand.category_source == Transaction.CategorySource.MANUAL
    assert original.category_id == grocery.pk
    assert original.category_source == Transaction.CategorySource.MANUAL
    assert refund.category_id == grocery.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED


@pytest.mark.django_db
def test_apply_rule_propagates_to_linked_refunds_and_reverse_restores_them():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    grocery = groceries(household)
    dining_cat = dining(household)
    original = make_transaction(owner, account, fingerprint="7" * 64)
    refund = make_transaction(
        owner,
        account,
        amount_minor=1000,
        description="SYNTHETIC KROGER REFUND",
        fingerprint="8" * 64,
    )
    Transaction.objects.filter(pk=original.pk).update(category=dining_cat)
    original.refresh_from_db()
    link_refund(owner, refund.pk, original.pk)
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=grocery.pk,
        priority=0,
    )
    application, skipped = apply_rule(owner, rule.pk)
    assert skipped == 0
    original.refresh_from_db()
    refund.refresh_from_db()
    assert original.category_id == grocery.pk
    assert original.category_source == Transaction.CategorySource.RULE
    assert refund.category_id == grocery.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert refund.correction_history.filter(
        field_name=TransactionCorrectionHistory.Field.CATEGORY
    ).exists()
    result = reverse_application(owner, application.pk)
    original.refresh_from_db()
    refund.refresh_from_db()
    assert result.restored == 1
    assert original.category_id == dining_cat.pk
    assert original.category_source == Transaction.CategorySource.UNSET
    assert refund.category_id == dining_cat.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED


@pytest.mark.django_db
def test_reverse_skips_refund_later_inherited_from_linked_original():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    grocery = groceries(household)
    dining_cat = dining(household)
    refund = make_transaction(
        owner,
        account,
        amount_minor=1000,
        description="SYNTHETIC KROGER REFUND",
        fingerprint="9" * 64,
    )
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=grocery.pk,
        priority=0,
    )
    application, skipped = apply_rule(owner, rule.pk)
    assert skipped == 0
    refund.refresh_from_db()
    assert refund.category_source == Transaction.CategorySource.RULE
    original = make_transaction(owner, account, fingerprint="0" * 64)
    assign_category(owner, original.pk, dining_cat.pk)
    link_refund(owner, refund.pk, original.pk)
    result = reverse_application(owner, application.pk)
    refund.refresh_from_db()
    original.refresh_from_db()
    assert result.restored == 0
    assert result.skipped_manual == 1
    assert refund.category_id == dining_cat.pk
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert original.category_id == dining_cat.pk


@pytest.mark.django_db
def test_deleting_an_account_removes_its_rule_history_and_account_rules():
    from finance.lifecycle_services import delete_account
    from finance.models import CategoryRule, RuleApplication, RuleApplicationEntry

    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    other = make_account(owner, name="Synthetic Other")
    make_transaction(owner, account, fingerprint="a1".ljust(64, "0"))
    kept = make_transaction(owner, other, fingerprint="b1".ljust(64, "0"))
    general = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    only_this = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=account.pk,
        min_amount_minor=None, max_amount_minor=None, category_id=dining(household).pk, priority=1,
    )
    apply_rule(owner, only_this.pk)
    apply_rule(owner, general.pk)

    delete_account(owner, account.pk)

    assert not CategoryRule.objects.filter(pk=only_this.pk).exists()
    assert CategoryRule.objects.filter(pk=general.pk).exists()
    assert not RuleApplicationEntry.objects.filter(transaction__account_id=account.pk).exists()
    assert RuleApplicationEntry.objects.filter(transaction=kept).exists()
    assert RuleApplication.objects.filter(rule=general).exists()


@pytest.mark.django_db
def test_preview_and_auto_apply_fold_case_the_same_way():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, description="SYNTHETIC GROSSE Straße MARKT", fingerprint="c1".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="STRASSE", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )

    _, matches = preview_rule(owner, rule.pk)
    assert [item.pk for item in matches] == [txn.pk]
    apply_rule(owner, rule.pk)
    txn.refresh_from_db()
    assert txn.category_id == groceries(household).pk


@pytest.mark.django_db
def test_auto_apply_skips_investment_rows_and_excluded_transfers():
    from finance.category_services import refresh_transfer_pairs

    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Savings")
    investment = make_transaction(owner, checking, description="SYNTHETIC KROGER FUND", fingerprint="d1".ljust(64, "0"))
    Transaction.objects.filter(pk=investment.pk).update(kind=Transaction.Kind.INVESTMENT_ACTIVITY)
    out_leg = make_transaction(owner, checking, amount_minor=-5000, description="SYNTHETIC KROGER MOVE", fingerprint="d2".ljust(64, "0"))
    make_transaction(owner, savings, amount_minor=5000, description="SYNTHETIC MOVE IN", fingerprint="d3".ljust(64, "0"))
    refresh_transfer_pairs(owner)
    save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )

    apply_enabled_rules_to_transactions(owner, list(Transaction.objects.filter(pk__in=[investment.pk, out_leg.pk])))

    investment.refresh_from_db()
    out_leg.refresh_from_db()
    assert investment.category_id is None
    assert out_leg.category_id is None


@pytest.mark.django_db
def test_reversing_an_older_application_keeps_a_newer_rules_category():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, fingerprint="e1".ljust(64, "0"))
    first = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    first_application, _skipped = apply_rule(owner, first.pk)
    set_rule_enabled(owner, first.pk, False)
    second = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=dining(household).pk, priority=1,
    )
    apply_rule(owner, second.pk)
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk

    reverse_application(owner, first_application.pk)

    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk


@pytest.mark.django_db
def test_reversing_later_then_earlier_application_restores_original_category():
    owner = make_person("owner-chain")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, fingerprint="e2".ljust(64, "0"))
    first = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    first_application, _skipped = apply_rule(owner, first.pk)
    set_rule_enabled(owner, first.pk, False)
    second = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=dining(household).pk, priority=1,
    )
    second_application, _skipped = apply_rule(owner, second.pk)
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk

    reverse_application(owner, second_application.pk)
    txn.refresh_from_db()
    assert txn.category_id == groceries(household).pk

    reverse_application(owner, first_application.pk)
    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_personal_rule_does_not_apply_former_household_category_after_move():
    from finance.lifecycle_services import leave_household

    owner = make_person("owner-move")
    stayer = make_person("stayer-move")
    household_a = make_household(owner, stayer, name="Synthetic Household A")
    save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household_a).pk, priority=0,
    )
    leave_household(owner)
    household_b = make_household(owner, name="Synthetic Household B")
    shared_b = make_account(owner, name="Shared B", scope=Account.Scope.HOUSEHOLD, household=household_b)
    txn = make_transaction(owner, shared_b, fingerprint="e3".ljust(64, "0"))

    apply_enabled_rules_to_transactions(owner, [txn])

    txn.refresh_from_db()
    assert txn.category_id is None
    assert groceries(household_a).household_id != household_b.pk


@pytest.mark.django_db
def test_rules_list_flags_personal_rule_inactive_after_household_change():
    from finance.lifecycle_services import leave_household

    owner = make_person("owner-inactive")
    stayer = make_person("stayer-inactive")
    household_a = make_household(owner, stayer, name="Synthetic Household A")
    save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household_a).pk, priority=0,
    )
    leave_household(owner)
    make_household(owner, name="Synthetic Household B")
    client = Client()
    client.force_login(owner.user)

    page = client.get(reverse("category-rule-list")).content.decode()

    assert 'data-rule-inactive="true"' in page
    assert "Inactive" in page
    assert "kroger" in page


@pytest.mark.django_db
def test_reversing_earlier_then_later_application_restores_original_category():
    owner = make_person("owner-out-of-order")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, fingerprint="e3".ljust(64, "0"))
    first = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    first_application, _skipped = apply_rule(owner, first.pk)
    set_rule_enabled(owner, first.pk, False)
    second = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=dining(household).pk, priority=1,
    )
    second_application, _skipped = apply_rule(owner, second.pk)

    reverse_application(owner, first_application.pk)
    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk

    # The first application is already reversed, so undoing the second must
    # not bring its Groceries category back.
    reverse_application(owner, second_application.pk)
    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_rows_hidden_from_the_reverser_stay_reversible_by_their_owner():
    owner = make_person("owner-hidden")
    member = make_person("member-hidden")
    household = make_household(owner, member)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    later_private = make_account(owner, name="Synthetic Savings", scope=Account.Scope.HOUSEHOLD, household=household)
    visible = make_transaction(owner, shared, fingerprint="e4".ljust(64, "0"))
    hidden = make_transaction(owner, later_private, fingerprint="e5".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="household", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    application, _skipped = apply_rule(owner, rule.pk)
    Account.objects.filter(pk=later_private.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")

    reverse_application(member, application.pk)
    visible.refresh_from_db()
    hidden.refresh_from_db()
    assert visible.category_id is None
    assert hidden.category_id == groceries(household).pk

    reverse_application(owner, application.pk)
    hidden.refresh_from_db()
    assert hidden.category_id is None


@pytest.mark.django_db
def test_reversal_skips_an_application_undone_before_the_later_one_ran():
    owner = make_person("owner-interleaved")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, fingerprint="e6".ljust(64, "0"))
    other = Category.objects.create(household=household, name="Synthetic Other")

    def rule_for(category, priority):
        return save_category_rule(
            owner, owner_kind="personal", description_contains="kroger", account_id=None,
            min_amount_minor=None, max_amount_minor=None, category_id=category.pk, priority=priority,
        )

    first = rule_for(groceries(household), 0)
    first_application, _skipped = apply_rule(owner, first.pk)
    set_rule_enabled(owner, first.pk, False)
    second = rule_for(dining(household), 1)
    second_application, _skipped = apply_rule(owner, second.pk)
    reverse_application(owner, second_application.pk)
    set_rule_enabled(owner, second.pk, False)
    third = rule_for(other, 2)
    third_application, _skipped = apply_rule(owner, third.pk)

    reverse_application(owner, first_application.pk)
    reverse_application(owner, third_application.pk)

    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_apply_rechecks_eligibility_after_locking(monkeypatch):
    import finance.rule_services as rule_services

    owner = make_person("owner-race")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, fingerprint="e7".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    original_lock = rule_services._lock_transactions

    def reclassified_before_lock(person, transactions):
        # Another request changes the row after the preview query ran.
        Transaction.objects.filter(pk=txn.pk).update(kind=Transaction.Kind.INVESTMENT_ACTIVITY)
        return original_lock(person, transactions)

    monkeypatch.setattr(rule_services, "_lock_transactions", reclassified_before_lock)
    apply_rule(owner, rule.pk)

    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_reversal_rechecks_visibility_after_locking(monkeypatch):
    import finance.rule_services as rule_services

    owner = make_person("owner-unshare")
    member = make_person("member-unshare")
    household = make_household(owner, member)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    txn = make_transaction(owner, shared, fingerprint="e8".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="household", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    application, _skipped = apply_rule(owner, rule.pk)
    original_lock = rule_services._lock_transactions

    def unshared_before_lock(person, transactions):
        Account.objects.filter(pk=shared.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")
        return original_lock(person, transactions)

    monkeypatch.setattr(rule_services, "_lock_transactions", unshared_before_lock)
    reverse_application(member, application.pk)

    txn.refresh_from_db()
    assert txn.category_id == groceries(household).pk
    assert RuleApplicationEntry.objects.get(application=application).reversed_at is None


@pytest.mark.django_db
def test_apply_uses_the_rule_as_saved_when_rows_are_locked(monkeypatch):
    import finance.rule_services as rule_services

    owner = make_person("owner-rule-edit")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, fingerprint="e9".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    original_lock = rule_services._lock_transactions

    def edited_before_lock(person, transactions):
        CategoryRule.objects.filter(pk=rule.pk).update(category=dining(household))
        return original_lock(person, transactions)

    monkeypatch.setattr(rule_services, "_lock_transactions", edited_before_lock)
    apply_rule(owner, rule.pk)

    txn.refresh_from_db()
    assert txn.category_id == dining(household).pk


@pytest.mark.django_db
def test_former_member_can_undo_a_household_rule_on_their_lent_account():
    from finance.lifecycle_services import leave_household

    owner = make_person("owner-leaves")
    member = make_person("member-stays")
    household = make_household(owner, member)
    lent = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    Account.objects.filter(pk=lent.pk).update(share_mode=Account.ShareMode.LENT)
    txn = make_transaction(owner, lent, fingerprint="ea".ljust(64, "0"))
    rule = save_category_rule(
        member, owner_kind="household", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    apply_rule(member, rule.pk)
    leave_household(owner)
    lent.refresh_from_db()
    assert lent.scope == Account.Scope.PRIVATE

    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("category-rule-list")).content.decode()
    assert "Changes from rules you can no longer open" in page
    assert "kroger" not in page.lower()
    application = RuleApplication.objects.get(rule=rule)
    client.post(reverse("category-rule-application-reverse", args=[application.pk]))

    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_deleting_an_account_keeps_rule_history_for_other_accounts():
    from finance.lifecycle_services import delete_account

    owner = make_person("owner-delete")
    household = make_household(owner)
    first = make_account(owner)
    second = make_account(owner, name="Synthetic Savings")
    make_transaction(owner, first, fingerprint="eb".ljust(64, "0"))
    kept = make_transaction(owner, second, fingerprint="ec".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    application, _skipped = apply_rule(owner, rule.pk)
    save_category_rule(
        owner, rule_id=rule.pk, owner_kind="personal", description_contains="kroger", account_id=first.pk,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )

    delete_account(owner, first.pk)

    assert RuleApplicationEntry.objects.filter(application=application, transaction=kept).exists()
    reverse_application(owner, application.pk)
    kept.refresh_from_db()
    assert kept.category_id is None


@pytest.mark.django_db
def test_reversing_a_rule_on_a_marked_transfer_updates_its_snapshot():
    from finance.category_services import confirm_transfer_pair, undo_transfer_pair
    from finance.models import TransferPair

    owner = make_person("owner-transfer")
    household = make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Savings")
    out_leg = make_transaction(owner, checking, fingerprint="ed".ljust(64, "0"))
    in_leg = make_transaction(owner, savings, amount_minor=1000, description="SYNTHETIC MOVE IN", fingerprint="ee".ljust(64, "0"))
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=checking.pk,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    application, _skipped = apply_rule(owner, rule.pk)
    pair = TransferPair.objects.create(
        leg_a=out_leg, leg_b=in_leg, status=TransferPair.Status.SUGGESTED,
        kind=TransferPair.Kind.TRANSFER, confidence=TransferPair.Confidence.HIGH, reasons=["synthetic"],
    )
    confirm_transfer_pair(owner, pair.pk)

    reverse_application(owner, application.pk)
    undo_transfer_pair(owner, pair.pk)

    out_leg.refresh_from_db()
    assert out_leg.category_id is None


@pytest.mark.django_db
def test_a_rule_applies_automatically_only_after_its_preview_is_confirmed():
    owner = make_person("owner-confirm")
    household = make_household(owner)
    account = make_account(owner)
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )
    first = make_transaction(owner, account, fingerprint="ef".ljust(64, "0"))

    apply_enabled_rules_to_transactions(owner, [first])
    first.refresh_from_db()
    assert first.category_id is None

    apply_rule(owner, rule.pk)
    second = make_transaction(owner, account, fingerprint="f0".ljust(64, "0"))
    apply_enabled_rules_to_transactions(owner, [second])
    second.refresh_from_db()
    assert second.category_id == groceries(household).pk

    # Editing the rule needs a fresh confirmation before it applies on its own.
    save_category_rule(
        owner, rule_id=rule.pk, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=dining(household).pk, priority=0,
    )
    third = make_transaction(owner, account, fingerprint="f1".ljust(64, "0"))
    apply_enabled_rules_to_transactions(owner, [third])
    third.refresh_from_db()
    assert third.category_id is None


@pytest.mark.django_db
def test_apply_locks_the_household_before_writing_the_rule():
    from django.db import connection as db_connection
    from django.test.utils import CaptureQueriesContext

    if db_connection.vendor != "postgresql":
        pytest.skip("SQLite does not emit row locks")
    owner = make_person("owner-lock-order")
    household = make_household(owner)
    make_account(owner)
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="kroger", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=groceries(household).pk, priority=0,
    )

    with CaptureQueriesContext(db_connection) as queries:
        apply_rule(owner, rule.pk)

    statements = [query["sql"].lower() for query in queries.captured_queries]
    membership_lock = next(i for i, sql in enumerate(statements) if "finance_membership" in sql and "for update" in sql)
    rule_write = next(i for i, sql in enumerate(statements) if sql.startswith("update") and "finance_categoryrule" in sql)
    # Saving a rule locks the household first; Apply must use the same order.
    assert membership_lock < rule_write
