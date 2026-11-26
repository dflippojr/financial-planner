"""Clicking one spot on the Transfers page changes one pair at most (#635, AC1 and AC2).

Hypothesis builds a starting state for the page: card payments that are
auto-marked, confirmed or undone before the visit, some with a second credit
that an Undo turns into a competing suggestion. It then picks spots in the
Excluded list and clicks each one up to ten times, posting whatever form sits
at that spot on the current page and following the redirect. The expected
outcome comes from the clicks, not from the code: every click at a spot names
the same pair, the rows above Recently undone never move, and only clicked
pairs change, each ending undone or confirmed.
"""

from datetime import date, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from hypothesis import example, given
from hypothesis import strategies as st

from finance.models import Account, TransferPair
from finance.transfer_pairing import refresh_transfer_pairs, undo_transfer_pair
from tests.test_categorization import make_account, make_household, make_person, make_transaction
from tests.test_transfer_restore import EXCLUDED_START, UNDONE_START, _forms

from . import harness as ledger

pytestmark = [pytest.mark.property, pytest.mark.django_db]

KINDS = ("auto", "confirmed", "undone before the visit")
MARKED = (TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED)


@st.composite
def page_states(draw):
    count = draw(st.integers(1, 5), label="pairs")
    kinds = draw(st.lists(st.sampled_from(KINDS), min_size=count, max_size=count), label="kinds")
    rivals = draw(st.lists(st.booleans(), min_size=count, max_size=count), label="second credit")
    spots = draw(st.lists(st.integers(0, 4), min_size=1, max_size=2), label="spots")
    clicks = draw(st.integers(1, 10), label="clicks per spot")
    return tuple(kinds), tuple(rivals), tuple(spots), clicks


def _build(kinds, rivals):
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Joint Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    card = make_account(
        owner, name="Synthetic Joint Card", account_type=Account.Type.CREDIT_CARD,
        scope=Account.Scope.HOUSEHOLD, household=household,
    )
    for index in range(len(rivals)):
        amount = 10_000 + 100 * index
        day = date(2026, 3, 1) + timedelta(days=3 * index)
        make_transaction(owner, checking, amount_minor=-amount, transaction_date=day, description=f"SYN CARD PAYMENT {index}")
        make_transaction(owner, card, amount_minor=amount, transaction_date=day, description=f"SYN THANK YOU {index}")
    refresh_transfer_pairs(owner)
    pairs = list(TransferPair.objects.filter(status__in=MARKED).order_by("pk"))
    assert len(pairs) == len(rivals)
    # A second credit added after pairing matches the debit again once Undo frees it.
    for index, rival in enumerate(rivals):
        if rival:
            make_transaction(
                owner, card, amount_minor=10_000 + 100 * index, transaction_date=date(2026, 3, 1) + timedelta(days=3 * index),
                description=f"SYN SECOND CREDIT {index}", fingerprint=f"{index:x}".rjust(64, "e"),
            )
    refresh_transfer_pairs(owner)
    base = timezone.now() - timedelta(days=1)
    for index, pair in enumerate(pairs):
        # Distinct creation times, with updated_at in the opposite order.
        TransferPair.objects.filter(pk=pair.pk).update(
            created_at=base + timedelta(minutes=index), updated_at=base + timedelta(hours=1, minutes=-index),
        )
    for pair, kind in zip(pairs, kinds, strict=False):
        if kind == "confirmed":
            TransferPair.objects.filter(pk=pair.pk).update(status=TransferPair.Status.CONFIRMED)
        elif kind == "undone before the visit":
            undo_transfer_pair(owner, pair.pk)
            refresh_transfer_pairs(owner, transaction_ids=[pair.leg_a_id, pair.leg_b_id])  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
            TransferPair.objects.filter(pk=pair.pk).update(undone_at=timezone.now() - timedelta(hours=1))
    return owner


def _rows(html):
    """Forms above Recently undone, and where the Excluded ones start."""
    top = html[: html.index(UNDONE_START)]
    return _forms(top), len(_forms(top[: top.index(EXCLUDED_START)]))


@given(page_states())
@example((("auto", "auto", "auto"), (False, False, True), (0,), 10))
@example((("undone before the visit", "auto", "confirmed"), (True, True, False), (1, 0), 10))
def test_clicks_on_one_spot_change_only_the_pair_that_was_there(state):
    kinds, rivals, spots, clicks = state
    with ledger.isolated():
        owner = _build(kinds, rivals)
        start = dict(TransferPair.objects.values_list("pk", "status"))
        client = Client()
        client.force_login(owner.user)
        url = reverse("transfer-review")
        page = client.get(url)
        rows, first_excluded = _rows(page.content.decode())
        excluded = len(rows) - first_excluded
        if excluded == 0:
            return
        layout = [pk for pk, _action in rows]
        clicked = set()
        for spot in spots:
            spot = first_excluded + spot % excluded
            target = None
            for _click in range(clicks):
                pair_id, action = _rows(page.content.decode())[0][spot]
                target = target or pair_id
                assert pair_id == target, "a different pair moved under the cursor"
                page = client.post(url, {"action": action, "pair_id": pair_id}, follow=True)
                assert page.status_code == 200
                url = page.redirect_chain[-1][0]
                assert [pk for pk, _action in _rows(page.content.decode())[0]] == layout, "a row moved"
            clicked.add(target)

        end = dict(TransferPair.objects.values_list("pk", "status"))
        changed = {pk for pk in start if end.get(pk) != start[pk]}
        assert changed <= clicked
        assert all(end[pk] in (TransferPair.Status.UNDONE, TransferPair.Status.CONFIRMED) for pk in changed)
        assert not TransferPair.objects.filter(status__in=MARKED).exclude(pk__in=start).exists()
