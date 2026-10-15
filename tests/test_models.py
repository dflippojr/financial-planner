from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction, TransactionCorrectionHistory


@pytest.fixture
def person(db):
    user = get_user_model().objects.create_user(username="alex")
    return Person.objects.create(user=user, display_name="Alex Example")


@pytest.fixture
def household(person):
    household = Household.objects.create(name="Example Household")
    Membership.objects.create(person=person, household=household)
    return household


@pytest.fixture
def private_account(person):
    return Account.objects.create(
        name="Synthetic Checking",
        account_type=Account.Type.CHECKING,
        owner=person,
        scope=Account.Scope.PRIVATE,
        currency="USD",
    )


@pytest.fixture
def import_batch(person, private_account):
    return ImportBatch.objects.create(
        account=private_account,
        imported_by=person,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="a" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )


def _history_with_mismatched_columns(person, import_batch):
    financial_transaction = Transaction.objects.create(
        account=import_batch.account,
        import_batch=import_batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-100,
        description="Synthetic groceries",
        source_row_number=2,
        fingerprint="c" * 64,
        original_fields={"synthetic": "value"},
    )
    TransactionCorrectionHistory.objects.create(
        transaction=financial_transaction,
        actor=person,
        field_name=TransactionCorrectionHistory.Field.AMOUNT_MINOR,
        previous_date=date(2026, 1, 1),
        new_date=date(2026, 1, 2),
    )


def _account_with(person, **overrides):
    fields = {"name": "Invalid", "account_type": Account.Type.CHECKING, "owner": person}
    fields.update(overrides)
    Account.objects.create(**fields)


def _constraint_cases():
    private, household_scope = Account.Scope.PRIVATE, Account.Scope.HOUSEHOLD
    shared = Account.ShareMode.CO_OWNED
    return {
        "private-account-with-household": lambda p, h, a, b: _account_with(p, scope=private, household=h),
        "household-account-without-household": lambda p, h, a, b: _account_with(
            p, scope=household_scope, household=None, share_mode=shared
        ),
        "private-account-with-share-mode": lambda p, h, a, b: _account_with(
            p, scope=private, household=None, share_mode=shared
        ),
        "household-account-without-share-mode": lambda p, h, a, b: _account_with(
            p, scope=household_scope, household=h, share_mode=None
        ),
        "household-account-with-unknown-share-mode": lambda p, h, a, b: _account_with(
            p, scope=household_scope, household=h, share_mode="secret"
        ),
        "membership-ends-before-it-starts": lambda p, h, a, b: Membership.objects.create(
            person=p,
            household=Household.objects.create(name="Backwards Household"),
            joined_at=timezone.now(),
            ended_at=timezone.now() - timedelta(days=1),
        ),
        "non-usd-transaction": lambda p, h, a, b: Transaction.objects.create(
            account=a,
            import_batch=b,
            transaction_date=date(2026, 1, 15),
            amount_minor=100,
            currency="CAD",
            description="Invalid currency",
            source_row_number=2,
            fingerprint="d" * 64,
            original_fields={},
        ),
        "correction-history-with-mismatched-value-columns": lambda p, h, a, b: _history_with_mismatched_columns(p, b),
    }


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(_constraint_cases()))
def test_database_rejects_invalid_rows(person, household, private_account, import_batch, case):
    with pytest.raises(IntegrityError), transaction.atomic():
        _constraint_cases()[case](person, household, private_account, import_batch)


@pytest.mark.django_db
def test_person_can_have_only_one_current_household(person, household):
    another = Household.objects.create(name="Another Household")

    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(person=person, household=another)

    membership = Membership.objects.get(person=person, household=household)
    membership.ended_at = timezone.now()
    membership.save(update_fields=("ended_at",))
    Membership.objects.create(person=person, household=another)

    assert Membership.objects.filter(person=person).count() == 2


@pytest.mark.django_db
def test_original_fields_must_be_json_object(private_account, import_batch):
    imported = Transaction(
        account=private_account,
        import_batch=import_batch,
        transaction_date=date(2026, 1, 15),
        amount_minor=100,
        description="Invalid original fields",
        source_row_number=2,
        fingerprint="e" * 64,
        original_fields=["not", "an", "object"],
    )

    with pytest.raises(ValidationError, match="JSON object"):
        imported.full_clean()


@pytest.mark.django_db
def test_transaction_batch_must_match_account(person, private_account, import_batch):
    other_account = Account.objects.create(
        name="Other Account",
        account_type=Account.Type.INVESTMENT,
        owner=person,
    )
    imported = Transaction(
        account=other_account,
        import_batch=import_batch,
        transaction_date=date(2026, 1, 15),
        amount_minor=100,
        description="Mismatched provenance",
        source_row_number=2,
        fingerprint="f" * 64,
        original_fields={},
    )

    with pytest.raises(ValidationError, match="transaction account"):
        imported.full_clean()


@pytest.mark.django_db
def test_archived_records_require_archive_timestamp(private_account):
    unarchived = Account.objects.filter(pk=private_account.pk)

    with pytest.raises(IntegrityError), transaction.atomic():
        unarchived.update(status=Account.Status.ARCHIVED)

    archived_at = timezone.now()
    Account.objects.filter(pk=private_account.pk).update(
        status=Account.Status.ARCHIVED,
        archived_at=archived_at,
    )
    private_account.refresh_from_db()
    assert private_account.archived_at == archived_at
