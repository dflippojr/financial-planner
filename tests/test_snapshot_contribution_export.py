import csv
import io
import json
import zipfile
from datetime import date

import pytest
from django.contrib.auth import get_user_model

from finance.category_services import ensure_household_categories
from finance.export import money_decimal, write_export_zip
from finance.models import Account, BalanceSnapshot, Household, Membership, Person


PASSWORD = "Synthetic-passphrase-42!"

SNAPSHOT_HEADER = [
    "id",
    "account_id",
    "snapshot_date",
    "amount_minor",
    "amount_decimal",
    "currency",
    "source",
    "note",
    "net_contribution_minor",
    "net_contribution_decimal",
]


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.INVESTMENT,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_shared_account(owner, household, name="Shared Synthetic Brokerage"):
    return make_account(owner, name=name, scope=Account.Scope.HOUSEHOLD, household=household)


def _zip_from_bytes(payload):
    return zipfile.ZipFile(io.BytesIO(payload))


def _json_rows(archive, name):
    return json.loads(archive.read(f"{name}.json").decode())


def _csv_rows(archive, name):
    text = archive.read(f"{name}.csv").decode()
    return list(csv.DictReader(io.StringIO(text)))


def _csv_header(archive, name):
    text = archive.read(f"{name}.csv").decode()
    return next(csv.reader(io.StringIO(text)))


def _by_id(rows, pk):
    return next(row for row in rows if int(row["id"]) == pk)


@pytest.mark.django_db
@pytest.mark.parametrize("contribution", [12345, -2500, 0])
def test_snapshot_csv_carries_the_same_contribution_as_json(contribution):
    owner = make_person("owner")
    household = make_household(owner)
    shared = make_shared_account(owner, household)
    snapshot = BalanceSnapshot.objects.create(
        account=shared,
        snapshot_date=date(2026, 3, 31),
        amount_minor=500000,
        source=BalanceSnapshot.Source.MANUAL,
        net_contribution_minor=contribution,
    )

    archive = _zip_from_bytes(write_export_zip(owner))
    csv_row = _by_id(_csv_rows(archive, "balance_snapshots"), snapshot.pk)
    json_row = _by_id(_json_rows(archive, "balance_snapshots"), snapshot.pk)

    assert csv_row["net_contribution_minor"] == str(contribution)
    assert csv_row["net_contribution_decimal"] == money_decimal(contribution)
    assert not csv_row["net_contribution_decimal"].startswith("'")
    assert json_row["net_contribution_minor"] == contribution
    assert json_row["net_contribution_decimal"] == money_decimal(contribution)
    assert int(csv_row["net_contribution_minor"]) == json_row["net_contribution_minor"]
    assert csv_row["net_contribution_decimal"] == json_row["net_contribution_decimal"]


@pytest.mark.django_db
def test_snapshots_without_contribution_export_blank_csv_and_null_json():
    owner = make_person("owner")
    household = make_household(owner)
    shared = make_shared_account(owner, household)
    manual = BalanceSnapshot.objects.create(
        account=shared,
        snapshot_date=date(2026, 3, 31),
        amount_minor=400000,
        source=BalanceSnapshot.Source.MANUAL,
    )
    simplefin = BalanceSnapshot.objects.create(
        account=shared,
        snapshot_date=date(2026, 3, 31),
        amount_minor=410000,
        source=BalanceSnapshot.Source.SIMPLEFIN,
    )

    archive = _zip_from_bytes(write_export_zip(owner))
    csv_rows = _csv_rows(archive, "balance_snapshots")
    json_rows = _json_rows(archive, "balance_snapshots")

    for snapshot in (manual, simplefin):
        csv_row = _by_id(csv_rows, snapshot.pk)
        json_row = _by_id(json_rows, snapshot.pk)
        assert csv_row["net_contribution_minor"] == ""
        assert csv_row["net_contribution_decimal"] == ""
        assert json_row["net_contribution_minor"] is None
        assert json_row["net_contribution_decimal"] is None


@pytest.mark.django_db
def test_private_snapshot_contribution_of_another_member_is_not_exported():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_shared_account(owner, household)
    member_private = make_account(member, name="Member Private Brokerage")
    visible = BalanceSnapshot.objects.create(
        account=shared,
        snapshot_date=date(2026, 3, 31),
        amount_minor=250000,
        source=BalanceSnapshot.Source.MANUAL,
        net_contribution_minor=4200,
    )
    hidden = BalanceSnapshot.objects.create(
        account=member_private,
        snapshot_date=date(2026, 3, 31),
        amount_minor=880000,
        source=BalanceSnapshot.Source.MANUAL,
        net_contribution_minor=777001,
    )

    archive = _zip_from_bytes(write_export_zip(owner))
    csv_text = archive.read("balance_snapshots.csv").decode()
    json_text = archive.read("balance_snapshots.json").decode()

    for text in (csv_text, json_text):
        assert "777001" not in text
        assert "7770.01" not in text
    assert [int(row["id"]) for row in _csv_rows(archive, "balance_snapshots")] == [visible.pk]
    assert [row["id"] for row in _json_rows(archive, "balance_snapshots")] == [visible.pk]
    assert _csv_rows(archive, "balance_snapshots")[0]["net_contribution_minor"] == "4200"
    assert hidden.pk not in {row["id"] for row in _json_rows(archive, "balance_snapshots")}


@pytest.mark.django_db
def test_empty_snapshot_csv_keeps_the_full_header():
    owner = make_person("owner")

    archive = _zip_from_bytes(write_export_zip(owner))

    assert _csv_header(archive, "balance_snapshots") == SNAPSHOT_HEADER
    assert _csv_rows(archive, "balance_snapshots") == []
    assert _json_rows(archive, "balance_snapshots") == []
