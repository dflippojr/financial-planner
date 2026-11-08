"""The privacy sweeps' matcher: ids never trip it, every amount form does (#709)."""
import csv
import io
import zipfile

import pytest
from django.http import HttpResponse
from django.urls import reverse

from finance.export import write_export_zip
from finance.models import Receipt, RecurringSeries, RecurringSeriesMember, Transaction
from tests.privacy_world import (
    CANARY,
    CANARY_MINORS,
    FORBIDDEN,
    GOAL_MINOR,
    SHARED_CHARGE_MINOR,
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
    '{{"member_sources": {{"{n}": "detected", "2": "manual"}}}}',
    '{{"source_transaction_id": "{n}", "account_id": 3}}',
    '{{"source_transaction_id": "SYN-{n}", "account_id": 3}}',
    '{{"drilldown_url": "/transactions/{n}/edit/"}}',
    '<a href="https://planner.example/accounts/{n}/">Account</a>',
    '{{"export_path": "receipts/{n}/Synthetic receipt.png"}}',
    '<a href="#txn-{n}">Jump</a><button aria-controls="panel-{n}">Open</button>',
    '<a href="/planning/goals/?fund_item={n}#add-goal">Fund</a>',
    '<a href="/transactions/?date_from=2026-10-01&amp;tag={n}">Tagged</a>',
    '{{"drilldown_url": "/transactions/?date_from=2026-10-01\\u0026tag={n}"}}',
    '<input type="hidden" name="page_query" value="c={n}">',
    '<div data-page-route="/settings/audit/" data-page-query="actor={n}"></div>',
    '<p class="errorlist" role="alert">Select a valid choice. {n} is not one of the available choices.</p>',
    '<p class="errorlist" role="alert">Select a valid choice. saved:{n} is not one of the available choices.</p>',
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
    '<input value="2" data-value="{n}">',
    '<option value="2" data-value="{n}">Two</option>',
    '<p>Spent x={n}</p>',
    "<td>Spent/{n}</td>",
    '<p>Spent "amount_minor={n}"</p>',
    "<p>Spent ?x={n}</p>",
    "<p>Spent &amp;x={n}</p>",
    '<span data-amount="USD-{n}"></span>',
    '<span aria-label="USD-{n}"></span>',
    "<td>Spent/{n}/month</td>",
    '{{"note": "receipts/{n}"}}',
    '<input name="note" value="x={n}">',
    "<td>Spent-{n}</td>",
    '{{"note": "Spent/{n}"}}',
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
        assert leaks(csv_without_ids(text), FORBIDDEN) == [minor]
        assert leaks(csv_without_ids("".join(text.splitlines(keepends=True)[:2])), FORBIDDEN) == []


def test_csv_id_columns_still_show_names_decimals_and_ids_used_as_json_keys():
    minor = str(UNCATEGORIZED_MINOR)
    comma, plain, _minor = money_forms(GOAL_MINOR)
    header = "id,source_transaction_id,member_sources\r\n"
    assert leaks(csv_without_ids(header + f'1,{CANARY}-7,"{{""{minor}"":""detected""}}"\r\n'), FORBIDDEN) == [CANARY]
    assert leaks(csv_without_ids(header + f"1,{plain},{{}}\r\n"), FORBIDDEN) == [plain]
    assert leaks(csv_without_ids(header + f'1,x,"{comma}"\r\n'), FORBIDDEN) == [comma]


def test_a_long_id_list_holds_ids_however_far_from_its_key():
    for minor in CANARY_MINORS:
        items = ",\n".join(f"      {index}" for index in [*range(1, 200), minor])
        assert leaks(f'{{\n  "member_transaction_ids": [\n{items}\n  ]\n}}', FORBIDDEN) == []


def _csv(*rows):
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


def test_a_neighbouring_csv_cell_cannot_excuse_an_amount():
    for minor in CANARY_MINORS:
        for description in ('{"account_id":', '{"member_sources": {"', "/", '<input value="', "rename-"):
            text = _csv(("description", "amount_minor"), (description, minor))
            assert leaks(csv_without_ids(text), FORBIDDEN) == [minor], description
        assert leaks(csv_without_ids(_csv(("description",), (f'Spent "amount_minor={minor}"',))), FORBIDDEN) == [minor]
        text = _csv(("description", "amount_minor", "note"), ('{"account_id": "', minor, '"}'))
        assert leaks(csv_without_ids(text), FORBIDDEN) == [minor]


def test_a_receipt_path_in_an_export_csv_is_an_id():
    for minor in CANARY_MINORS:
        text = _csv(("id", "export_path"), ("1", f"receipts/{minor}/Synthetic receipt.png"))
        assert leaks(csv_without_ids(text), FORBIDDEN) == []


def test_every_decimal_form_and_the_name_still_match_anywhere():
    comma, plain, minor = money_forms(GOAL_MINOR)
    assert (comma, plain) == ("98,765.43", "98765.43")
    assert leaks(f"<td>${comma}</td>", FORBIDDEN) == [comma]
    assert leaks(f'{{"amount": "{plain}"}}', FORBIDDEN) == [plain]
    assert leaks(f"<td>-{plain} USD</td>", FORBIDDEN) == [plain]
    assert leaks(f"<td>{CANARY}-PRIVATE</td>", FORBIDDEN) == [CANARY]
    # An id value excuses only the bare minor form.
    assert set(leaks(f'{{"source_transaction_id": "{CANARY} {plain} {minor}"}}', FORBIDDEN)) == {CANARY, plain}
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
        # The member's own series on the shared charge exports its transaction id as a JSON key.
        series = RecurringSeries.objects.create(
            person=world.member,
            merchant_key="synthetic shared stream",
            display_name="Synthetic shared stream",
            cadence=RecurringSeries.Cadence.MONTHLY,
            typical_amount_minor=-SHARED_CHARGE_MINOR,
            status=RecurringSeries.Status.CONFIRMED,
            confidence=RecurringSeries.Confidence.HIGH,
            reasons=["synthetic"],
            fingerprint="e" * 64,
        )
        shared_charge = Transaction.objects.get(account=world.shared)
        RecurringSeriesMember.objects.create(series=series, transaction=shared_charge)
        # The member's receipt on the shared charge exports under receipts/<its id>/.
        Receipt.objects.create(
            transaction=shared_charge,
            original_name="Synthetic receipt.png",
            stored_name="f" * 32,
            content_type=Receipt.ContentType.PNG,
            size_bytes=8,
            uploaded_by=world.member,
        )
        exports = _exports(world.member)
    for response, raw in exports:
        assert minor in raw, f"the export holds no id {minor}, so this proves nothing"
        if response["Content-Type"].startswith("application/zip"):
            assert f'"{minor}": "detected"' in raw, "the member's series is not in the export"
            assert f"receipts/{minor}/" in raw, "the member's receipt is not in the export"
        assert route_leaks(response, PRIVATE_FORBIDDEN) == []


def test_the_owners_exports_still_trip_the_sweep_when_ids_share_the_amount(db):
    with ids_starting_at(UNCATEGORIZED_MINOR):
        world = build_report_world()
        exports = _exports(world.owner)
    for response, _raw in exports:
        assert str(UNCATEGORIZED_MINOR) in route_leaks(response, PRIVATE_FORBIDDEN)
