"""Synthetic households and canaries shared by the privacy sweeps (#481, #551).

The owner's private rows carry CANARY in their names and distinctive amounts;
FORBIDDEN lists every form a leaked amount can take on a page, and leaks()
finds them in a response body.
"""
import csv
import io
import re
import zipfile
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

# Where an id renders and an amount never does. Each rule needs its context right
# next to the number, so text further away (or in another CSV cell) never excuses one.
# HTML: a path segment of a URL that opens a quoted string or a CSV cell
# ("/accounts/17/", "https://host/accounts/17/"), the value attribute of an <option>
# or an <input>, or the end of an element id in an attribute that names an element
# (id="rename-17", for=, aria-controls= and the other id-reference aria attributes,
# data-open-dialog=, href="#txn-17"). aria-label is text, so it still counts.
_URL_PATH = re.compile(r"[\"'\x1f](?:https?://[^\"'\s<>/\x1f]+)?/[^\"'\s<>\x1f]*/\Z")
# A relative path such as the export's receipts/17/receipt.png counts only as a middle
# segment, with a slash after the number too, so "Spent/17" in text stays an amount.
_RELATIVE_PATH = re.compile(r"[\"'\x1f][\w.~-]+(?:/[\w.~-]+)*/\Z")
_ID_TAG = re.compile(r"<(?:option|input)\b[^<>]*\svalue=[\"']?\Z", re.IGNORECASE)
_ELEMENT_ID = re.compile(
    r"\s(?:id|for|aria-(?:controls|labelledby|describedby|owns)|data-open-dialog|data-[\w-]*-(?:id|target))=[\"'][A-Za-z_][\w-]*-\Z"
    r"|\shref=[\"']#[A-Za-z_][\w-]*-\Z"
)
# A query value: after "?" that follows a URL character or a quote, after "&", "&amp;"
# or "\u0026" that follows the previous value, or first in a query string held in
# data-page-query or a page_query/filter_query input. And Django's invalid-choice
# message, which echoes the id a request named. No URL in the app carries an amount.
_ECHOED_ID = re.compile(
    r"(?:[\"'/\w.-]\?|[\w%.:+-](?:&|&amp;|\\u0026))[\w-]+=\Z"
    r"|(?:\sdata-page-query=|\sname=[\"'](?:page|filter)_query[\"'][^<>]*\svalue=)[\"'][\w-]+=\Z"
    r"|Select a valid choice\. [\w:-]*\Z"
)
# JSON: the value of a key that names an id ("id": 17, "source_transaction_id": "TX-17",
# "pk"), a whole list under an "_ids" key however long, or an object key
# ({"17": "detected"}). Names and decimal amounts inside a quoted id still count.
_JSON_IDS = re.compile(
    r'"(?:\w*_)?(?:id|pk)"[ \t\r\n]*:[ \t\r\n]*(\d+|"[^"\\]*")'
    r'|"\w*_ids"[ \t\r\n]*:[ \t\r\n]*(\[[\d, \t\r\n]*\])'
    r'|"(\d+)"[ \t\r\n]*:'
)
# CSV cells are joined by the unit separator, and leaks() reads each cell on its own.
CELL_SEPARATOR = "\x1f"


def _whole_numbers(needle, body):
    # Not part of a longer number, word or decimal; a leading minus still counts.
    return re.finditer(rf"(?<![\w.]){needle}(?!\w)(?!\.\d)", body)


def _json_id_spans(body):
    return [
        match.span(group)
        for match in _JSON_IDS.finditer(body)
        for group in (1, 2, 3)
        if match.group(group) is not None
    ]


def _in_id_position(body, match, spans):
    start = match.start()
    before = body[max(start - 512, 0) : start]
    if _URL_PATH.search(before) or _ELEMENT_ID.search(before) or _ECHOED_ID.search(before):
        return True
    if _RELATIVE_PATH.search(before) and body[match.end() : match.end() + 1] == "/":
        return True
    if _ID_TAG.search(body[max(body.rfind("<", 0, start), 0) : start]):
        return True
    return any(low <= start < high for low, high in spans)


def _is_id_column(name):
    return name in ("id", "pk") or name.endswith(("_id", "_ids"))


def csv_without_ids(text):
    """The CSV's cells, unquoted and joined by CELL_SEPARATOR, without the bare ids in its id columns.

    An id column (id, account_id, member_transaction_ids) loses a cell that holds
    only numbers, such as 17 or [17,18]; any other text in it stays, so a name or
    decimal amount there still counts. Unquoted, a cell such as {"17":"detected"}
    reads as the JSON it is, and the separator keeps one cell from excusing the next.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    ids = {index for index, name in enumerate(rows[0]) if _is_id_column(name.strip())}
    cells = list(rows[0])
    for row in rows[1:]:
        cells += [
            "" if index in ids and re.fullmatch(r"\[?[\d,\s]*\]?", cell) else cell for index, cell in enumerate(row)
        ]
    return CELL_SEPARATOR.join(cells)


def leaks(body, forbidden):
    """The forbidden needles in body.

    Names and decimal amounts match anywhere. A bare minor amount such as 77777
    matches only as a whole number outside an id position: on PostgreSQL ids run
    on across tests, so a shared row the requester may see can land on a canary's
    value (#709), and a UUID, token or timestamp can contain the digits.
    """
    # Each CSV cell is read on its own, so no rule can reach into a neighbouring cell.
    # The leading separator marks where the cell starts.
    cells = [CELL_SEPARATOR + cell for cell in body.split(CELL_SEPARATOR)]
    return [needle for needle in forbidden if any(_cell_leaks(cell, needle) for cell in cells)]


def _cell_leaks(cell, needle):
    if not needle.isdigit():
        return needle in cell
    spans = _json_id_spans(cell)
    return any(not _in_id_position(cell, match, spans) for match in _whole_numbers(needle, cell))


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
    """What a sweep reads: the page, every file of a zip, and a CSV without its id columns."""
    content_type = response.get("Content-Type", "")
    if content_type.startswith("application/zip"):
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        return "\n".join(_file_text(name, archive.read(name)) for name in archive.namelist())
    raw = b"".join(response.streaming_content) if response.streaming else response.content
    return _file_text("response.csv" if content_type.startswith("text/csv") else "", raw)


def _file_text(name, raw):
    text = raw.decode("utf-8", "replace")
    return csv_without_ids(text) if name.endswith(".csv") else text


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
