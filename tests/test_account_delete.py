import threading
import time
from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.db.models import Q
from django.test import Client
from django.urls import reverse

from finance import lifecycle_services
from finance.category_services import assign_category, ensure_household_categories, link_refund, refresh_transfer_pairs
from finance.lifecycle_services import delete_account
from finance.models import (
    Account,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringSeries,
    RecurringSeriesMember,
    RefundLink,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransferPair,
)
from tests.helpers import stamp_recent_auth
from finance.recurring_services import confirm_recurring_series, refresh_recurring_series


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


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 2),
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
        date_range_end=date(2026, 12, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64])
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


def add_monthly_charges(owner, account, *, description="Synthetic Stream", amount_minor=-1599, count=3, start=date(2026, 1, 15)):
    rows = []
    for index in range(count):
        month = start.month + index
        year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        rows.append(
            make_transaction(
                owner,
                account,
                transaction_date=date(year, month, start.day),
                amount_minor=amount_minor,
                description=description,
            )
        )
    return rows


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def recent(person):
    return stamp_recent_auth(signed_in(person))


def missing_id(account):
    return account.pk + 999


def assert_same_404(response, missing_response):
    assert response.status_code == missing_response.status_code == 404
    assert response.content == missing_response.content


def related_counts(account_id):
    return {
        "Account": Account.objects.filter(pk=account_id).count(),
        "ImportBatch": ImportBatch.objects.filter(account_id=account_id).count(),
        "Transaction": Transaction.objects.filter(account_id=account_id).count(),
        "TransactionCorrectionHistory": TransactionCorrectionHistory.objects.filter(
            transaction__account_id=account_id
        ).count(),
        "TransferPair": TransferPair.objects.filter(
            Q(leg_a__account_id=account_id) | Q(leg_b__account_id=account_id)
        ).count(),
        "RefundLink": RefundLink.objects.filter(
            Q(refund__account_id=account_id) | Q(original__account_id=account_id)
        ).count(),
        "TransactionSplit": TransactionSplit.objects.filter(transaction__account_id=account_id).count(),
        "RecurringSeriesMember": RecurringSeriesMember.objects.filter(transaction__account_id=account_id).count(),
    }


def assert_no_rows_reference(account_id):
    assert related_counts(account_id) == {name: 0 for name in related_counts(account_id)}


@pytest.mark.django_db
def test_owner_deletes_private_and_household_accounts():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Synthetic Private")
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private_tx = make_transaction(owner, private, description="Synthetic private grocery")
    shared_tx = make_transaction(owner, shared, description="Synthetic shared grocery")
    TransactionCorrectionHistory.objects.create(
        transaction=private_tx,
        actor=owner,
        field_name=TransactionCorrectionHistory.Field.DESCRIPTION,
        previous_description="old",
        new_description="Synthetic private grocery",
    )
    TransactionCorrectionHistory.objects.create(
        transaction=shared_tx,
        actor=owner,
        field_name=TransactionCorrectionHistory.Field.DESCRIPTION,
        previous_description="old",
        new_description="Synthetic shared grocery",
    )
    private_id, shared_id = private.pk, shared.pk

    delete_account(owner, private_id)
    delete_account(owner, shared_id)

    assert_no_rows_reference(private_id)
    assert_no_rows_reference(shared_id)


@pytest.mark.django_db
def test_non_owner_and_outsider_cannot_delete_account():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    make_household(outsider, name="Other Household")
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(owner, shared)

    with pytest.raises(PermissionDenied):
        delete_account(member, shared.pk)
    with pytest.raises(PermissionDenied):
        delete_account(outsider, shared.pk)

    assert related_counts(shared.pk)["Account"] == 1
    assert related_counts(shared.pk)["Transaction"] == 1


@pytest.mark.django_db
def test_delete_repairs_transfer_partner_refunds_and_recurring_series():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    shared = make_account(owner, name="Synthetic Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Member Savings", account_type=Account.Type.SAVINGS)
    kept = make_account(owner, name="Synthetic Kept Card", account_type=Account.Type.CREDIT_CARD)
    shared_leg = make_transaction(owner, shared, amount_minor=-2800, description="Synthetic shared out")
    private_leg = make_transaction(member, private, amount_minor=2800, description="Synthetic private in")
    assign_category(member, private_leg.pk, groceries.pk)
    refresh_transfer_pairs(member)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.AUTO_MARKED
    assign_category(member, private_leg.pk, dining.pk)

    original_on_shared = make_transaction(owner, shared, amount_minor=-4000, description="Synthetic store")
    refund_on_kept = make_transaction(owner, kept, amount_minor=1500, description="Synthetic store refund")
    assign_category(owner, original_on_shared.pk, groceries.pk)
    link_refund(owner, refund_on_kept.pk, original_on_shared.pk)
    refund_on_kept.refresh_from_db()
    refund_category_id = refund_on_kept.category_id

    original_on_kept = make_transaction(owner, kept, amount_minor=-2200, description="Synthetic kept purchase")
    refund_on_shared = make_transaction(owner, shared, amount_minor=800, description="Synthetic kept refund")
    assign_category(owner, original_on_kept.pk, dining.pk)
    link_refund(owner, refund_on_shared.pk, original_on_kept.pk)
    original_on_kept.refresh_from_db()
    kept_purchase_category_id = original_on_kept.category_id

    add_monthly_charges(owner, shared, description="Synthetic Doomed Sub")
    add_monthly_charges(owner, shared, description="Synthetic Mixed Sub", count=2)
    add_monthly_charges(owner, kept, description="Synthetic Mixed Sub", count=1, start=date(2026, 3, 15))
    refresh_recurring_series(owner)
    doomed_series = RecurringSeries.objects.get(person=owner, display_name="Synthetic Doomed Sub")
    mixed_series = RecurringSeries.objects.get(person=owner, display_name="Synthetic Mixed Sub")
    confirm_recurring_series(owner, doomed_series.pk)
    confirm_recurring_series(owner, mixed_series.pk)
    shared_id = shared.pk

    delete_account(owner, shared_id)

    assert_no_rows_reference(shared_id)
    private_leg.refresh_from_db()
    assert private_leg.category_id == groceries.pk
    assert TransactionCorrectionHistory.objects.filter(
        transaction=private_leg,
        field_name=TransactionCorrectionHistory.Field.EXCLUSION,
        new_description="included",
        actor=owner,
    ).exists()
    refund_on_kept.refresh_from_db()
    original_on_kept.refresh_from_db()
    assert refund_on_kept.category_id == refund_category_id
    assert original_on_kept.category_id == kept_purchase_category_id
    assert not RefundLink.objects.filter(Q(refund=refund_on_kept) | Q(original=original_on_kept)).exists()
    doomed_series.refresh_from_db()
    mixed_series.refresh_from_db()
    assert doomed_series.is_active is False
    assert mixed_series.is_active is True
    assert mixed_series.members.count() == 1
    assert mixed_series.members.get().transaction.account_id == kept.pk
    assert not TransferPair.objects.filter(pk=pair.pk).exists()
    assert shared_leg.pk not in Transaction.objects.values_list("pk", flat=True)


@pytest.mark.django_db
def test_wrong_typed_name_deletes_nothing():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Checking")
    make_transaction(owner, account)
    client = recent(owner)

    response = client.post(reverse("account-delete", args=(account.pk,)), {"confirm_name": "Wrong Name"})

    assert response.status_code == 200
    assert Account.objects.filter(pk=account.pk).exists()
    assert related_counts(account.pk)["Transaction"] == 1
    content = response.content.decode()
    assert "Type the exact account name to confirm." in content
    assert "Synthetic row" not in content


@pytest.mark.django_db
def test_owner_delete_button_and_success_message():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Synthetic Private")
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(owner, private, description="Synthetic secret grocery")
    make_transaction(owner, shared)

    owner_page = signed_in(owner).get(reverse("account-list")).content.decode()
    member_page = signed_in(member).get(reverse("account-list")).content.decode()
    confirm = signed_in(owner).get(reverse("account-delete", args=(shared.pk,)))
    confirm_page = confirm.content.decode()

    assert reverse("account-delete", args=(private.pk,)) in owner_page
    assert reverse("account-delete", args=(shared.pk,)) in owner_page
    assert reverse("account-delete", args=(shared.pk,)) not in member_page
    assert reverse("account-delete", args=(private.pk,)) not in member_page
    assert confirm.status_code == 200
    assert "1 transaction" in confirm_page
    assert "1 import" in confirm_page
    assert "permanent" in confirm_page.casefold() or "permanently" in confirm_page.casefold()
    assert "8 weeks" in confirm_page
    assert "csrfmiddlewaretoken" in confirm_page
    assert "Synthetic secret grocery" not in confirm_page

    deleted = recent(owner).post(reverse("account-delete", args=(shared.pk,)), {"confirm_name": "Synthetic Shared"}, follow=True)
    assert deleted.redirect_chain[0][0] == reverse("account-list")
    assert "Deleted Synthetic Shared." in deleted.content.decode()
    assert_no_rows_reference(shared.pk)
    assert "Synthetic secret grocery" not in deleted.content.decode()


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.django_db
def test_non_owner_delete_views_return_identical_404(method):
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    make_household(outsider, name="Other Household")
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    payload = {"confirm_name": "Shared"}
    signed = recent if method == "post" else signed_in
    caller = getattr(signed(member), method)
    outsider_client = getattr(signed(outsider), method)
    owner_missing = getattr(signed(owner), method)

    member_private = caller(reverse("account-delete", args=(private.pk,)), data=payload)
    outsider_private = outsider_client(reverse("account-delete", args=(private.pk,)), data=payload)
    outsider_shared = outsider_client(reverse("account-delete", args=(shared.pk,)), data=payload)
    member_shared = caller(reverse("account-delete", args=(shared.pk,)), data=payload)
    missing = owner_missing(reverse("account-delete", args=(missing_id(private),)), data=payload)

    assert_same_404(member_private, missing)
    assert_same_404(outsider_private, missing)
    assert_same_404(outsider_shared, missing)
    assert_same_404(member_shared, missing)
    assert Account.objects.filter(pk=private.pk).exists()
    assert Account.objects.filter(pk=shared.pk).exists()


@pytest.mark.django_db
def test_delete_enforces_csrf():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Checking")
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner.user)
    stamp_recent_auth(client)

    denied = client.post(reverse("account-delete", args=(account.pk,)), {"confirm_name": "Synthetic Checking"})
    page = client.get(reverse("account-delete", args=(account.pk,)))
    token = client.cookies["csrftoken"].value
    allowed = client.post(
        reverse("account-delete", args=(account.pk,)),
        {"confirm_name": "Synthetic Checking", "csrfmiddlewaretoken": token},
    )

    assert denied.status_code == 403
    assert page.status_code == 200
    assert allowed.status_code == 302
    assert not Account.objects.filter(pk=account.pk).exists()


def _run_delete_race(other_action, owner, account):
    first_lock_held = threading.Event()
    other_is_waiting = threading.Event()
    errors = []
    original_lock = lifecycle_services._visible_account_for_update

    def lock_then_pause(principal, account_id):
        locked = original_lock(principal, account_id)
        first_lock_held.set()
        other_is_waiting.wait(timeout=10)
        return locked

    def run(action):
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    with patch.object(lifecycle_services, "_visible_account_for_update", lock_then_pause):
        deleting = threading.Thread(target=run, args=(lambda: delete_account(owner, account.pk),))
        deleting.start()
        assert first_lock_held.wait(timeout=10)
        other = threading.Thread(target=run, args=(other_action,))
        other.start()
        time.sleep(1.5)
        other_is_waiting.set()
        deleting.join(timeout=30)
        other.join(timeout=30)

    assert not deleting.is_alive()
    assert not other.is_alive()
    assert errors == []
    assert_no_rows_reference(account.pk)


@pytest.mark.django_db(transaction=True)
def test_delete_account_races_edit_and_transfer_refresh_without_deadlock():
    if connection.vendor != "postgresql":
        pytest.skip("row-lock ordering can only be exercised on PostgreSQL")

    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Member Savings", account_type=Account.Type.SAVINGS)
    financial_transaction = make_transaction(owner, shared, amount_minor=-3100, description="Synthetic shared out")
    make_transaction(member, private, amount_minor=3100, description="Synthetic private in")
    refresh_transfer_pairs(member)

    def edit():
        client = Client()
        client.force_login(member.user)
        client.post(
            reverse("transaction-edit", args=(financial_transaction.pk,)),
            {"transaction_date": "2026-01-05", "description": "Late correction", "amount": "-31.00"},
        )

    _run_delete_race(edit, owner, shared)

    owner = make_person("owner-two")
    member = make_person("member-two")
    household = make_household(owner, member, name="Second Household")
    shared = make_account(owner, name="Synthetic Shared Two", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Member Savings Two", account_type=Account.Type.SAVINGS)
    make_transaction(owner, shared, amount_minor=-3200, description="Synthetic shared out two")
    make_transaction(member, private, amount_minor=3200, description="Synthetic private in two")
    refresh_transfer_pairs(member)

    _run_delete_race(lambda: refresh_transfer_pairs(member), owner, shared)


@pytest.mark.django_db
def test_deleting_a_shared_account_keeps_another_members_series_with_private_charges():
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    shared = make_account(alice, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    bob_card = make_account(bob, name="Synthetic Bob Card")
    add_monthly_charges(alice, shared, description="Synthetic Gym", count=2, start=date(2026, 1, 15))
    add_monthly_charges(bob, bob_card, description="Synthetic Gym", count=2, start=date(2026, 3, 15))
    refresh_recurring_series(bob)
    series = RecurringSeries.objects.get(person=bob, status=RecurringSeries.Status.SUGGESTED)
    assert series.members.count() == 4
    confirm_recurring_series(bob, series.pk)

    delete_account(alice, shared.pk)
    series.refresh_from_db()

    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.is_active
    assert set(series.members.values_list("transaction__account_id", flat=True)) == {bob_card.pk}
