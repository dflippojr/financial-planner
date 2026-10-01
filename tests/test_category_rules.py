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
from finance.csv_import.services import commit_csv_import
from finance.models import (
    Account,
    Category,
    CategoryRule,
    Household,
    ImportBatch,
    Membership,
    Person,
    RuleApplication,
    Transaction,
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
    document = read_csv(CSV)
    commit_csv_import(
        owner.user,
        account.pk,
        content=CSV,
        document=document,
        mapping=mapping(),
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )
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
    assert refund.category_id is None
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
