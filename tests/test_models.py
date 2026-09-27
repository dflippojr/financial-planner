from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


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


@pytest.mark.django_db
def test_private_and_household_account_shapes_are_persisted(person, household):
    private = Account.objects.create(
        name="Private Savings",
        account_type=Account.Type.SAVINGS,
        owner=person,
        scope=Account.Scope.PRIVATE,
    )
    shared = Account.objects.create(
        name="Shared Card",
        account_type=Account.Type.CREDIT_CARD,
        owner=person,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )

    assert private.household is None
    assert shared.household == household
    assert shared.currency == "USD"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scope", "with_household"),
    [(Account.Scope.PRIVATE, True), (Account.Scope.HOUSEHOLD, False)],
)
def test_database_rejects_scope_household_mismatches(person, household, scope, with_household):
    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.create(
            name="Invalid Scope",
            account_type=Account.Type.CHECKING,
            owner=person,
            scope=scope,
            household=household if with_household else None,
        )


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
def test_membership_end_cannot_precede_join(person):
    household = Household.objects.create(name="Example Household")

    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(
            person=person,
            household=household,
            joined_at=timezone.now(),
            ended_at=timezone.now() - timedelta(days=1),
        )


@pytest.mark.django_db
def test_transaction_preserves_exact_signed_minor_units_and_provenance(private_account, import_batch):
    original_fields = {
        "Date": "01/15/2026",
        "Description": "SYNTHETIC PAYROLL",
        "Amount": "123456789012.34",
    }
    imported = Transaction.objects.create(
        account=private_account,
        import_batch=import_batch,
        transaction_date=date(2026, 1, 15),
        amount_minor=12_345_678_901_234,
        currency="USD",
        description="Synthetic payroll",
        source_row_number=2,
        source_transaction_id="synthetic-transaction-1",
        fingerprint="b" * 64,
        original_fields=original_fields,
    )
    spent = Transaction.objects.create(
        account=private_account,
        import_batch=import_batch,
        transaction_date=date(2026, 1, 16),
        amount_minor=-10_99,
        currency="USD",
        description="Synthetic groceries",
        source_row_number=3,
        fingerprint="c" * 64,
        original_fields={"Amount": "-10.99"},
    )

    imported.refresh_from_db()
    assert imported.amount_minor == 12_345_678_901_234
    assert imported.original_fields == original_fields
    assert spent.amount_minor == -1099


@pytest.mark.django_db
def test_database_rejects_non_usd_transaction(private_account, import_batch):
    with pytest.raises(IntegrityError), transaction.atomic():
        Transaction.objects.create(
            account=private_account,
            import_batch=import_batch,
            transaction_date=date(2026, 1, 15),
            amount_minor=100,
            currency="CAD",
            description="Invalid currency",
            source_row_number=2,
            fingerprint="d" * 64,
            original_fields={},
        )


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
def test_investment_activity_is_stored_as_neutral_kind(private_account, import_batch):
    imported = Transaction.objects.create(
        account=private_account,
        import_batch=import_batch,
        transaction_date=date(2026, 1, 15),
        amount_minor=0,
        description="Synthetic investment activity",
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
        source_row_number=2,
        fingerprint="0" * 64,
        original_fields={"Type": "SYNTHETIC UNKNOWN ACTIVITY"},
    )

    assert imported.kind == Transaction.Kind.INVESTMENT_ACTIVITY


@pytest.mark.django_db
def test_archived_records_require_archive_timestamp(private_account):
    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.filter(pk=private_account.pk).update(status=Account.Status.ARCHIVED)

    archived_at = timezone.now()
    Account.objects.filter(pk=private_account.pk).update(
        status=Account.Status.ARCHIVED,
        archived_at=archived_at,
    )
    private_account.refresh_from_db()
    assert private_account.archived_at == archived_at
