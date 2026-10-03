from datetime import date

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import ForeignKey, OneToOneField
from django.test import Client
from django.urls import reverse

from finance.auth_services import create_recovery_codes
from finance.category_services import ensure_household_categories
from finance.lifecycle_services import LENT_DELETE, LENT_HANDOVER, delete_member_data
from finance.models import (
    FORMER_MEMBER_LABEL,
    Account,
    Household,
    ImportBatch,
    Membership,
    Person,
    PlannedItem,
    Transaction,
    TransactionCorrectionHistory,
)
from tests.helpers import stamp_recent_auth


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
    scope=Account.Scope.PRIVATE,
    household=None,
    share_mode=None,
):
    if share_mode is None:
        share_mode = Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else ""
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
    )


def make_transaction(owner, account, *, description="Synthetic row"):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="c" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 12, 31),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-1000,
        description=description,
        source_row_number=2,
        fingerprint="d" * 64,
        original_fields={"synthetic": "value"},
    )


def leftover_identity_rows(person_id, user_id):
    leftovers = []
    person_model = Person
    user_model = get_user_model()
    if person_model.objects.filter(pk=person_id).exists():
        leftovers.append("finance.Person")
    if user_model.objects.filter(pk=user_id).exists():
        leftovers.append(user_model._meta.label)
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for field in model._meta.fields:
            if not isinstance(field, (ForeignKey, OneToOneField)):
                continue
            related = field.remote_field.model
            lookup = {field.attname: person_id if related is person_model else user_id}
            if related is person_model or related is user_model:
                if model.objects.filter(**lookup).exists():
                    leftovers.append(f"{model._meta.label}.{field.name}")
    return leftovers


@pytest.mark.django_db
def test_delete_member_data_removes_owned_rows_and_anonymizes_shared_history():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Synthetic Private")
    make_transaction(owner, private)
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    shared_txn = make_transaction(owner, shared, description="Synthetic shared")
    history = TransactionCorrectionHistory.objects.create(
        transaction=shared_txn,
        actor=owner,
        field_name=TransactionCorrectionHistory.Field.DESCRIPTION,
        previous_description="old",
        new_description="Synthetic shared",
    )
    create_recovery_codes(owner.user)
    person_id = owner.pk
    user_id = owner.user_id
    member_private = make_account(member, name="Member Private")
    member_txn = make_transaction(member, member_private)

    delete_member_data(owner, {})

    assert leftover_identity_rows(person_id, user_id) == []
    shared.refresh_from_db()
    assert shared.owner_id == member.pk
    assert shared.scope == Account.Scope.HOUSEHOLD
    history.refresh_from_db()
    assert history.actor_id is None
    assert history.actor_label == FORMER_MEMBER_LABEL
    assert ImportBatch.objects.get(pk=shared_txn.import_batch_id).imported_by_id is None
    assert Transaction.objects.filter(pk=shared_txn.pk).exists()
    assert Account.objects.filter(pk=member_private.pk).exists()
    assert Transaction.objects.filter(pk=member_txn.pk).exists()
    assert Membership.objects.filter(person=member, ended_at__isnull=True).exists()


@pytest.mark.django_db
def test_last_member_deletion_removes_the_household():
    owner = make_person("solo")
    household = make_household(owner)
    household_id = household.pk
    shared = make_account(
        owner,
        name="Synthetic Only Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    make_transaction(owner, shared)
    PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.HOUSEHOLD,
        household=household,
        name="Synthetic bill",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=500,
        start_date=date(2026, 2, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )

    delete_member_data(owner, {})

    assert not Household.objects.filter(pk=household_id).exists()
    assert not Account.objects.filter(pk=shared.pk).exists()


@pytest.mark.django_db
def test_lent_handover_keeps_account_and_delete_removes_the_other():
    owner = make_person("lender")
    member = make_person("borrower")
    household = make_household(owner, member)
    keep = make_account(
        owner,
        name="Synthetic Lent Keep",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )
    keep_txn = make_transaction(owner, keep, description="Keep history")
    drop = make_account(
        owner,
        name="Synthetic Lent Drop",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )
    make_transaction(owner, drop, description="Drop history")

    delete_member_data(owner, {keep.pk: LENT_HANDOVER, drop.pk: LENT_DELETE})

    keep.refresh_from_db()
    assert keep.scope == Account.Scope.HOUSEHOLD
    assert keep.share_mode == Account.ShareMode.CO_OWNED
    assert keep.owner_id == member.pk
    assert Transaction.objects.filter(pk=keep_txn.pk).exists()
    assert not Account.objects.filter(pk=drop.pk).exists()


@pytest.mark.django_db
def test_lent_choices_are_required():
    owner = make_person("lender")
    member = make_person("borrower")
    household = make_household(owner, member)
    lent = make_account(
        owner,
        name="Synthetic Lent",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )

    with pytest.raises(ValidationError, match="Every lent account needs a choice"):
        delete_member_data(owner, {})
    lent.refresh_from_db()
    assert lent.share_mode == Account.ShareMode.LENT
    assert Person.objects.filter(pk=owner.pk).exists()


@pytest.mark.django_db
def test_delete_my_data_page_requires_reauth_username_and_lent_choice():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    lent = make_account(
        owner,
        name="Synthetic Lent",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )
    client = Client()
    client.force_login(owner.user)

    refused = client.post(reverse("delete-my-data"), {"confirm_username": "owner", f"lent_{lent.pk}": LENT_DELETE})
    assert refused.status_code == 302
    assert refused.url.startswith(reverse("reauth"))

    stamp_recent_auth(client)
    page = client.get(reverse("delete-my-data"))
    assert page.status_code == 200
    assert b"Export data" in page.content
    assert b"14 nightly" in page.content
    assert b"Synthetic Lent" in page.content
    assert b"private account" in page.content

    incomplete = client.post(reverse("delete-my-data"), {"confirm_username": "owner"})
    assert incomplete.status_code == 200
    assert Person.objects.filter(pk=owner.pk).exists()

    wrong_name = client.post(
        reverse("delete-my-data"),
        {"confirm_username": "someone-else", f"lent_{lent.pk}": LENT_DELETE},
    )
    assert wrong_name.status_code == 200
    assert Person.objects.filter(pk=owner.pk).exists()

    finished = client.post(
        reverse("delete-my-data"),
        {"confirm_username": "owner", f"lent_{lent.pk}": LENT_DELETE},
    )
    assert finished.status_code == 302
    assert finished.url == reverse("member-data-deleted")
    success = client.get(finished.url)
    assert success.status_code == 200
    assert b"Your data has been deleted." in success.content
    assert b"owner" not in success.content
    assert not Person.objects.filter(pk=owner.pk).exists()


@pytest.mark.django_db
def test_settings_links_to_delete_my_data():
    owner = make_person("owner")
    make_household(owner)
    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("settings-data"))
    assert reverse("delete-my-data") in page.content.decode()


@pytest.mark.django_db
def test_delete_member_data_command_requires_typed_username(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "wrong")
    with pytest.raises(CommandError, match="Confirmation did not match"):
        call_command("delete_member_data", username="owner")
    assert Person.objects.filter(pk=owner.pk).exists()

    monkeypatch.setattr("builtins.input", lambda _prompt="": "owner")
    call_command("delete_member_data", username="owner")
    assert not Person.objects.filter(pk=owner.pk).exists()


@pytest.mark.django_db
def test_remaining_member_sees_former_member_on_shared_history():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    shared_txn = make_transaction(owner, shared, description="Synthetic shared")
    TransactionCorrectionHistory.objects.create(
        transaction=shared_txn,
        actor=owner,
        field_name=TransactionCorrectionHistory.Field.DESCRIPTION,
        previous_description="old",
        new_description="Synthetic shared",
    )
    delete_member_data(owner, {})

    client = Client()
    client.force_login(member.user)
    page = client.get(reverse("transaction-edit", args=(shared_txn.pk,)))
    assert FORMER_MEMBER_LABEL.encode() in page.content
    assert b"Owner Example" not in page.content
