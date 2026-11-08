"""The privacy sweeps' matcher: ids never trip it, every amount form does (#709)."""
import pytest

from tests.privacy_world import CANARY, CANARY_MINORS, FORBIDDEN, GOAL_MINOR, UNCATEGORIZED_MINOR, leaks, money_forms


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
