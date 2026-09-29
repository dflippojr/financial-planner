import threading
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from finance.forms import TransactionCorrectionForm
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_transaction(owner, *, account=None, transaction_date=date(2026, 1, 2), amount_minor=-1234, description="Synthetic groceries", fingerprint="a" * 64):
    account = account or Account.objects.create(name="Synthetic Checking", account_type=Account.Type.CHECKING, owner=owner)
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        source_row_number=2,
        fingerprint=fingerprint,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


@pytest.mark.django_db
def test_transaction_pages_and_home_are_not_browser_cached():
    owner = make_person("owner")
    financial_transaction = make_transaction(owner)
    client = Client()
    client.force_login(owner.user)

    list_response = client.get(reverse("transaction-list"))
    edit_response = client.get(reverse("transaction-edit", args=(financial_transaction.pk,)))
    home_response = client.get(reverse("home"))

    assert "no-store" in list_response["Cache-Control"]
    assert "no-store" in edit_response["Cache-Control"]
    assert "no-store" in home_response["Cache-Control"]


@pytest.mark.django_db
def test_transaction_list_requires_sign_in():
    response = Client().get(reverse("transaction-list"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_transaction_list_shows_columns_provenance_and_newest_first():
    owner = make_person("owner")
    older = make_transaction(owner, description="Synthetic older", transaction_date=date(2026, 1, 2), fingerprint="c" * 64)
    newer = make_transaction(owner, description="Synthetic newer", transaction_date=date(2026, 1, 3), fingerprint="d" * 64)
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("transaction-list"))

    assert response.status_code == 200
    assert list(response.context["transactions"]) == [newer, older]
    content = response.content.decode()
    for heading in ("Date", "Account", "Description", "Amount", "Category", "Source", "Scope"):
        assert f"<th>{heading}</th>" in content
    assert "-12.34 USD" in content
    assert "Uncategorized" in content
    assert "Huntington Bank" in content
    assert "Imported" in content
    assert "Private" in content


@pytest.mark.django_db
def test_transaction_list_never_exposes_another_persons_private_data_or_filter_choice():
    viewer = make_person("viewer")
    other = make_person("other")
    secret = make_transaction(other, description="PRIVATE SECRET TRANSACTION")
    client = Client()
    client.force_login(viewer.user)

    response = client.get(reverse("transaction-list"), {"account": secret.account_id, "q": "SECRET"})

    content = response.content.decode()
    assert response.status_code == 200
    assert "PRIVATE SECRET TRANSACTION" not in content
    assert secret.account.name not in content
    assert "Select a valid choice" in content
    assert not response.context["transactions"].exists()


@pytest.mark.django_db
def test_transaction_list_includes_shared_data_for_current_member_but_not_former_member():
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    membership = Membership.objects.create(person=member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    make_transaction(owner, account=account, description="Synthetic shared transaction")
    client = Client()
    client.force_login(member.user)

    current_response = client.get(reverse("transaction-list"))
    membership.ended_at = membership.joined_at
    membership.save(update_fields=("ended_at",))
    former_response = client.get(reverse("transaction-list"))

    assert "Synthetic shared transaction" in current_response.content.decode()
    assert "Synthetic shared transaction" not in former_response.content.decode()


@pytest.mark.django_db
def test_transaction_filters_are_inclusive_and_can_combine():
    owner = make_person("owner")
    account = Account.objects.create(name="Filter Account", account_type=Account.Type.CHECKING, owner=owner)
    matching = make_transaction(owner, account=account, transaction_date=date(2026, 1, 10), description="Synthetic Coffee", fingerprint="e" * 64)
    make_transaction(owner, account=account, transaction_date=date(2026, 1, 11), description="Synthetic Coffee", fingerprint="f" * 64)
    make_transaction(owner, transaction_date=date(2026, 1, 10), description="Synthetic Tea", fingerprint="1" * 64)
    client = Client()
    client.force_login(owner.user)

    response = client.get(
        reverse("transaction-list"),
        {"date_from": "2026-01-10", "date_to": "2026-01-10", "account": account.pk, "category": "uncategorized", "q": "coffee"},
    )

    assert list(response.context["transactions"]) == [matching]


@pytest.mark.django_db
def test_invalid_date_range_is_reported_without_exposing_transactions():
    owner = make_person("owner")
    make_transaction(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("transaction-list"), {"date_from": "2026-02-01", "date_to": "2026-01-01"})

    assert "End date must be on or after start date." in response.content.decode()
    assert not response.context["transactions"].exists()


@pytest.mark.django_db
def test_authorized_owner_can_correct_user_controlled_fields_and_preserve_provenance():
    owner = make_person("owner")
    financial_transaction = make_transaction(owner)
    original_fields = financial_transaction.original_fields.copy()
    account_id = financial_transaction.account_id
    import_batch_id = financial_transaction.import_batch_id
    source_row_number = financial_transaction.source_row_number
    fingerprint = financial_transaction.fingerprint
    client = Client()
    client.force_login(owner.user)

    response = client.post(
        reverse("transaction-edit", args=(financial_transaction.pk,)),
        {"transaction_date": "2026-01-05", "description": "Corrected synthetic groceries", "amount": "-98.76"},
    )

    list_url = reverse("transaction-list")
    corrected_date = date(2026, 1, 5)

    assert response.status_code == 302
    assert response.url == list_url
    financial_transaction.refresh_from_db()
    assert financial_transaction.transaction_date == corrected_date
    assert financial_transaction.description == "Corrected synthetic groceries"
    assert financial_transaction.amount_minor == -9876
    assert financial_transaction.original_fields == original_fields
    assert financial_transaction.account_id == account_id
    assert financial_transaction.import_batch_id == import_batch_id
    assert financial_transaction.source_row_number == source_row_number
    assert financial_transaction.fingerprint == fingerprint


@pytest.mark.django_db
def test_current_household_member_can_correct_shared_transaction():
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    financial_transaction = make_transaction(owner, account=account)
    client = Client()
    client.force_login(member.user)

    response = client.post(
        reverse("transaction-edit", args=(financial_transaction.pk,)),
        {"transaction_date": "2026-01-02", "description": "Shared correction", "amount": "-12.34"},
    )

    assert response.status_code == 302
    financial_transaction.refresh_from_db()
    assert financial_transaction.description == "Shared correction"


@pytest.mark.django_db
def test_former_household_member_cannot_correct_shared_transaction():
    owner = make_person("owner")
    former_member = make_person("former-member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    membership = Membership.objects.create(person=former_member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    financial_transaction = make_transaction(owner, account=account)
    membership.ended_at = membership.joined_at
    membership.save(update_fields=("ended_at",))
    client = Client()
    client.force_login(former_member.user)

    response = client.post(
        reverse("transaction-edit", args=(financial_transaction.pk,)),
        {"transaction_date": "2026-01-02", "description": "Revoked correction", "amount": "-12.34"},
    )

    assert response.status_code == 404
    financial_transaction.refresh_from_db()
    assert financial_transaction.description == "Synthetic groceries"


@pytest.mark.django_db
@pytest.mark.parametrize("method", ("get", "post"))
def test_private_transaction_cannot_be_read_or_edited_by_another_person(method):
    owner = make_person("owner")
    viewer = make_person("viewer")
    financial_transaction = make_transaction(owner)
    client = Client()
    client.force_login(viewer.user)
    request = getattr(client, method)

    response = request(
        reverse("transaction-edit", args=(financial_transaction.pk,)),
        {"transaction_date": "2026-01-02", "description": "Unauthorized correction", "amount": "1.00"},
    )

    assert response.status_code == 404
    financial_transaction.refresh_from_db()
    assert financial_transaction.description == "Synthetic groceries"


@pytest.mark.django_db
def test_archived_transaction_is_not_listed_or_editable():
    owner = make_person("owner")
    financial_transaction = make_transaction(owner)
    Transaction.objects.filter(pk=financial_transaction.pk).update(status=Transaction.Status.ARCHIVED, archived_at="2026-01-04T12:00:00Z")
    client = Client()
    client.force_login(owner.user)

    list_response = client.get(reverse("transaction-list"))
    edit_response = client.get(reverse("transaction-edit", args=(financial_transaction.pk,)))

    assert financial_transaction.description not in list_response.content.decode()
    assert edit_response.status_code == 404


def test_correction_form_converts_money_exactly_and_rejects_bigint_overflow():
    exact = TransactionCorrectionForm({"transaction_date": "2026-01-02", "description": "Synthetic", "amount": "123456789012.34"})
    overflow = TransactionCorrectionForm({"transaction_date": "2026-01-02", "description": "Synthetic", "amount": "99999999999999999.99"})

    assert exact.is_valid()
    assert int(exact.cleaned_data["amount"] * 100) == 12_345_678_901_234
    assert not overflow.is_valid()
    assert "supported range" in overflow.errors["amount"][0]


@pytest.mark.django_db
def test_correction_form_initial_amount_does_not_round_through_float():
    owner = make_person("owner")
    financial_transaction = make_transaction(owner, amount_minor=9_007_199_254_740_993)

    form = TransactionCorrectionForm.for_transaction(financial_transaction)

    assert form.initial["amount"] == Decimal("90071992547409.93")


@pytest.mark.django_db
def test_correction_edit_posts_large_amount_as_decimal_text():
    owner = make_person("owner")
    financial_transaction = make_transaction(owner, amount_minor=9_007_199_254_740_993)
    client = Client()
    client.force_login(owner.user)
    url = reverse("transaction-edit", args=(financial_transaction.pk,))

    get_response = client.get(url)
    html = get_response.content.decode()
    assert 'name="amount"' in html
    assert 'type="text"' in html
    assert 'type="number"' not in html
    assert 'inputmode="decimal"' in html
    assert 'value="90071992547409.93"' in html

    post_response = client.post(
        url,
        {
            "transaction_date": "2026-01-02",
            "description": financial_transaction.description,
            "amount": "90071992547409.93",
        },
    )

    assert post_response.status_code == 302
    financial_transaction.refresh_from_db()
    assert financial_transaction.amount_minor == 9_007_199_254_740_993


@pytest.mark.django_db(transaction=True)
def test_correction_cannot_be_saved_after_the_editor_is_evicted_from_the_household():
    # A correction must lock the membership that authorizes it, in the same
    # order as the lifecycle services (memberships, then accounts, then
    # transactions). Otherwise a member being evicted can pass the visibility
    # check, have the eviction commit, and still save their edit afterward.
    # SQLite ignores row locks, so this runs under scripts/test_postgres.sh.
    if connection.vendor != "postgresql":
        pytest.skip("row-lock ordering can only be exercised on PostgreSQL")

    owner = make_person("owner")
    editor = make_person("editor")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=editor, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    financial_transaction = make_transaction(owner, account=account)

    editing_paused = threading.Event()
    removal_committed = threading.Event()
    observed = {}
    errors = []
    original_apply = TransactionCorrectionForm.apply

    def pause_before_saving(form, target):
        editing_paused.set()
        # If the edit holds the membership lock, the removal cannot finish
        # while it waits here, so this wait simply times out.
        removal_committed.wait(timeout=3)
        observed["removal_committed_before_save"] = removal_committed.is_set()
        return original_apply(form, target)

    def edit():
        try:
            client = Client()
            client.force_login(editor.user)
            client.post(
                reverse("transaction-edit", args=(financial_transaction.pk,)),
                {"transaction_date": "2026-01-05", "description": "Late correction", "amount": "-1.00"},
            )
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    def evict():
        try:
            call_command("evict_household_member", username="editor")
            removal_committed.set()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    with patch.object(TransactionCorrectionForm, "apply", pause_before_saving):
        editing = threading.Thread(target=edit)
        editing.start()
        assert editing_paused.wait(timeout=10)
        evicting = threading.Thread(target=evict)
        evicting.start()
        editing.join(timeout=30)
        evicting.join(timeout=30)

    assert errors == []
    assert observed["removal_committed_before_save"] is False
