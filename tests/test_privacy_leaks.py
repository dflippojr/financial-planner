"""The privacy sweeps' matcher: ids never trip it, every amount form does (#709)."""
import io
import zipfile

import pytest
from django.http import HttpResponse
from django.urls import reverse

from finance.export import write_export_zip
from tests.privacy_world import (
    CANARY,
    CANARY_MINORS,
    FORBIDDEN,
    GOAL_MINOR,
    UNCATEGORIZED_MINOR,
    build_report_world,
    ids_starting_at,
    leaks,
    csv_without_ids,
    money_forms,
)
from tests.test_route_access_sweep import PRIVATE_FORBIDDEN, _client as fresh_client, _leaks as route_leaks


MINOR = str(UNCATEGORIZED_MINOR)

# Positions where the app renders an object id, filled with a canary's value.
ID_POSITIONS = [
    '<option value="{n}">Synthetic Shared</option>',
    "<option value='{n}' selected>Synthetic Shared</option>",
    '<a href="/accounts/{n}/imports/">Import</a>',
    '<form method="post" action="/accounts/3/imports/{n}/undo/">',
    '<a href="/transactions/{n}/edit/">Edit</a>',
    '<input type="hidden" name="transaction_id" value="{n}">',
    '<input type="checkbox" name="accounts" value="{n}" checked>',
    '<label for="rename-{n}">Name</label><input id="rename-{n}">',
    '<dialog id="archive-{n}"></dialog><a data-open-dialog="archive-{n}">Archive</a>',
    '<a href="/?horizon=3&amp;sc=m%7C{n}%7C1">Scenario</a>',
    '{{"id": {n}, "name": "Synthetic Shared"}}',
    '{{"account_id": {n}, "import_batch_id": 3}}',
    '{{"member_transaction_ids": [\n      1,\n      {n}\n    ]}}',
    '{{"member_transaction_ids": [{n}, 2]}}',
]

# Digits that only contain a canary's value.
LOOKALIKES = [
    "<td>1{n}</td>",
    "<td>{n}0</td>",
    "<td>{n}.5</td>",
    "<td>3.{n}</td>",
    '<meta name="token" content="a{n}b">',
]

# Ways the app renders an amount, filled with a canary's value.
AMOUNT_RENDERS = [
    '<script type="application/json">{{"spending_minor": {n}, "count": 1}}</script>',
    '<script type="application/json">{{"net_minor": -{n}}}</script>',
    '<script type="application/json">[1,{n},2]</script>',
    "<td>{n},</td>",
    '<progress class="progress" value="{n}" max="100"></progress>',
    "<td>{n}</td>",
    '<option value="2">{n}</option>',
    "date,amount\n2026-10-15,-{n},USD\n",
    'data-amount="{n}"',
    '{{"id": 1, "amount_minor": -{n}}}',
    '{{"account_id": 1, "typical_amount_minor": {n}}}',
    "Spent {n} this month.",
]


@pytest.mark.parametrize("html", ID_POSITIONS + LOOKALIKES)
def test_an_id_or_longer_number_holding_a_canary_value_is_not_a_leak(html):
    for minor in CANARY_MINORS:
        assert leaks(html.format(n=minor), FORBIDDEN) == []


@pytest.mark.parametrize("html", AMOUNT_RENDERS)
def test_a_rendered_minor_amount_is_a_leak(html):
    for minor in CANARY_MINORS:
        assert leaks(html.format(n=minor), FORBIDDEN) == [minor]


@pytest.mark.parametrize("html", AMOUNT_RENDERS)
def test_a_rendered_amount_is_a_leak_next_to_an_id_of_the_same_value(html):
    page = ID_POSITIONS[0].format(n=MINOR) + html.format(n=MINOR) + ID_POSITIONS[2].format(n=MINOR)
    assert leaks(page, FORBIDDEN) == [MINOR]


def test_csv_id_columns_are_emptied_and_amount_columns_kept():
    for minor in CANARY_MINORS:
        text = (
            "id,account_id,amount_minor,member_transaction_ids,description\r\n"
            f'{minor},{minor},5,"[1,{minor}]",Synthetic\r\n'
            f"2,3,-{minor},[],Synthetic {minor}\r\n"
        )
        cleaned = csv_without_ids(text)
        assert cleaned.splitlines()[1] == ",,5,,Synthetic"
        assert leaks(cleaned, FORBIDDEN) == [minor]
        assert leaks(csv_without_ids(text.splitlines(keepends=True)[0] + text.splitlines(keepends=True)[1]), FORBIDDEN) == []


def test_every_decimal_form_and_the_name_still_match_anywhere():
    comma, plain, minor = money_forms(GOAL_MINOR)
    assert (comma, plain) == ("98,765.43", "98765.43")
    assert leaks(f"<td>${comma}</td>", FORBIDDEN) == [comma]
    assert leaks(f'{{"amount": "{plain}"}}', FORBIDDEN) == [plain]
    assert leaks(f"<td>-{plain} USD</td>", FORBIDDEN) == [plain]
    assert leaks(f"<td>{CANARY}-PRIVATE</td>", FORBIDDEN) == [CANARY]
    assert minor in CANARY_MINORS


def test_every_canary_has_all_three_forms_in_forbidden():
    for minor in CANARY_MINORS:
        assert set(money_forms(int(minor))) <= set(FORBIDDEN)


def _exports(person):
    """The member's data export zip and transaction CSV, each with its raw text."""
    archive = write_export_zip(person)
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        archive_raw = "\n".join(files.read(name).decode("utf-8") for name in files.namelist())
    streamed = fresh_client(person).post(reverse("transaction-export"))
    assert streamed.status_code == 200
    csv_raw = b"".join(streamed.streaming_content)
    return [
        (HttpResponse(archive, content_type="application/zip"), archive_raw),
        (HttpResponse(csv_raw, content_type=streamed["Content-Type"]), csv_raw.decode("utf-8")),
    ]


@pytest.mark.parametrize("minor", CANARY_MINORS)
def test_the_members_exports_name_ids_equal_to_a_canary_amount_without_a_leak(db, minor):
    """The data export's JSON and CSV and the transaction CSV carry ids in their own keys and columns."""
    with ids_starting_at(int(minor)):
        world = build_report_world()
        exports = _exports(world.member)
    for response, raw in exports:
        assert minor in raw, f"the export holds no id {minor}, so this proves nothing"
        assert route_leaks(response, PRIVATE_FORBIDDEN) == []


def test_the_owners_exports_still_trip_the_sweep_when_ids_share_the_amount(db):
    with ids_starting_at(UNCATEGORIZED_MINOR):
        world = build_report_world()
        exports = _exports(world.owner)
    for response, _raw in exports:
        assert str(UNCATEGORIZED_MINOR) in route_leaks(response, PRIVATE_FORBIDDEN)
