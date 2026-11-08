"""Synthetic households and canaries shared by the privacy sweeps (#481, #551).

The owner's private rows carry CANARY in their names and distinctive amounts;
FORBIDDEN lists every form a leaked amount can take on a page, and leaks()
finds them in a response body.
"""
import re
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.test import Client
from django.utils import timezone

from finance.category_services import ensure_household_categories
from finance.lifecycle_services import update_debt_terms
from finance.models import (
    Account,
    BalanceSnapshot,
    Household,
    ImportBatch,
    Membership,
    PlannedItem,
    Person,
    RecurringSeries,
    RecurringSeriesMember,
    SavingsGoal,
    Transaction,
)


PASSWORD = "Synthetic-passphrase-42!"
CANARY = "SYN-CANARY"
PRIVATE_CHARGE_MINOR = 888_888
SHARED_CHARGE_MINOR = 1_100
UNCATEGORIZED_MINOR = 77_777
CARD_BALANCE_MINOR = 432_109
PLANNED_MINOR = 654_321
GOAL_MINOR = 9_876_543


def money_forms(minor):
    value = Decimal(minor) / 100
    return (f"{value:,.2f}", f"{value:.2f}", str(minor))


FORBIDDEN = (
    CANARY,
    *money_forms(PRIVATE_CHARGE_MINOR),
    # The owner's groceries total, shared and private charges together.
    *money_forms(PRIVATE_CHARGE_MINOR + SHARED_CHARGE_MINOR),
    *money_forms(UNCATEGORIZED_MINOR),
    *money_forms(CARD_BALANCE_MINOR),
    *money_forms(PLANNED_MINOR),
    *money_forms(GOAL_MINOR),
)
# Each canary's bare minor string, the form an object id can take too.
CANARY_MINORS = tuple(needle for needle in FORBIDDEN if needle.isdigit())

# Where an id renders and an amount never does: a URL path segment, the value of an
# <option> or an <input>, or the end of an element id such as rename-17.
_ID_TAG = re.compile(r"<(?:option|input)\b[^<>]*\bvalue=[\"']?\Z", re.IGNORECASE)


def _whole_numbers(needle, body):
    # Not part of a longer number, word, decimal or slug; a leading minus still counts.
    return re.finditer(rf"(?<![\w.])(?<!\w-){needle}(?!\w)(?!\.\d)", body)


def _in_id_position(body, start):
    if body[start - 1 : start] == "/":
        return True
    tag = body[max(body.rfind("<", 0, start), 0) : start]
    return bool(_ID_TAG.search(tag))


def leaks(body, forbidden):
    """The forbidden needles in body.

    Names and decimal amounts match anywhere. A bare minor amount such as 77777
    matches only as a whole number outside an id position: on PostgreSQL ids run
    on across tests, so a shared row the requester may see can land on a canary's
    value (#709), and a UUID, token or timestamp can contain the digits.
    """
    return [
        needle
        for needle in forbidden
        if (
            any(not _in_id_position(body, match.start()) for match in _whole_numbers(needle, body))
            if needle.isdigit()
            else needle in body
        )
    ]


@contextmanager
def ids_starting_at(value):
    """Give the next row of every table the id `value`, as a long PostgreSQL run eventually can."""
    tables = [model._meta.db_table for model in apps.get_models() if isinstance(model._meta.pk, models.AutoField)]
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            # Sequences ignore the test transaction, so put them back afterwards.
            saved = {}
            for table in tables:
                cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
                (sequence,) = cursor.fetchone()
                cursor.execute(f"SELECT last_value, is_called FROM {sequence}")  # noqa: S608 - name from the catalog
                saved[sequence] = cursor.fetchone()
                cursor.execute("SELECT setval(%s, %s, false)", [sequence, value])
        else:
            for table in tables:
                cursor.execute("DELETE FROM sqlite_sequence WHERE name = %s", [table])
                cursor.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (%s, %s)", [table, value - 1])
    try:
        yield
    finally:
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                for sequence, (last_value, is_called) in saved.items():
                    cursor.execute("SELECT setval(%s, %s, %s)", [sequence, last_value, is_called])


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def seed_transaction(account, owner, *, amount_minor, when, description, category=None, index):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=when,
        date_range_end=when,
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=when,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=f"{index:064x}",
        original_fields={},
        category=category,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def body_text(response):
    if response.streaming:
        return b"".join(response.streaming_content).decode("utf-8")
    return response.content.decode("utf-8")


def build_report_world():
    """Seed the report sweep's household: an owner's private canaries and one shared account."""
    today = timezone.localdate()
    # The last full month is closed, so monthly review and year-end include it.
    when = (today.replace(day=1) - timedelta(days=1)).replace(day=15)
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    for person in (owner, member):
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    groceries = household.categories.get(name="Groceries")

    shared = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    private = Account.objects.create(name=f"{CANARY}-PRIVATE", account_type=Account.Type.CHECKING, owner=owner)
    card = Account.objects.create(name=f"{CANARY}-CARD", account_type=Account.Type.CREDIT_CARD, owner=owner)
    seed_transaction(shared, owner, amount_minor=-SHARED_CHARGE_MINOR, when=when, description="Synthetic shared spend", category=groceries, index=1)
    secret_charge = seed_transaction(
        private, owner, amount_minor=-PRIVATE_CHARGE_MINOR, when=when, description=f"{CANARY} charge", category=groceries, index=2
    )
    seed_transaction(private, owner, amount_minor=-UNCATEGORIZED_MINOR, when=when, description=f"{CANARY} loose", index=3)

    series = RecurringSeries.objects.create(
        person=owner,
        merchant_key="syn canary stream",
        display_name=f"{CANARY} stream",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-PRIVATE_CHARGE_MINOR,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="d" * 64,
    )
    RecurringSeriesMember.objects.create(series=series, transaction=secret_charge)
    planned = PlannedItem.objects.create(
        owner=owner,
        name=f"{CANARY} planned",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=PLANNED_MINOR,
        start_date=today + timedelta(days=20),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    goal = SavingsGoal.objects.create(
        owner=owner,
        name=f"{CANARY} goal",
        target_amount_minor=GOAL_MINOR,
        target_date=today + timedelta(days=400),
    )
    BalanceSnapshot.objects.create(
        account=card,
        snapshot_date=today,
        amount_minor=CARD_BALANCE_MINOR,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    update_debt_terms(owner.user, card.pk, apr_percent=Decimal("19.990"), minimum_payment_minor=3_500, payment_day=None)
    return SimpleNamespace(
        owner=owner,
        member=member,
        household=household,
        groceries=groceries,
        when=when,
        planned=planned,
        goal=goal,
        series=series,
        shared=shared,
        private=private,
        card=card,
        secret_charge=secret_charge,
    )
