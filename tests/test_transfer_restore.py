"""Undo keeps a transfer pair in place, and an undone pair can be restored (#635)."""

import re
from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.audit_models import AuditEvent
from finance.cash_flow import cash_flow_report, selected_accounts
from finance.ledger_counting import income_and_spending_totals
from finance.models import Account, Transaction, TransactionCorrectionHistory, TransferPair
from finance.transfer_pairing import refresh_transfer_pairs, undo_transfer_pair
from tests.test_categorization import make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db

FORM = re.compile(r"<form method=\"post\"[^>]*>(.*?)</form>", re.DOTALL)
PAIR_ID = re.compile(r'name="pair_id" value="(\d+)"')
ACTION = re.compile(r'name="action" value="([a-z]+)"')
EXCLUDED_START = 'id="transfer-excluded"'
UNDONE_START = 'id="transfer-recently-undone"'


def _restore(principal, pair_id):
    # Imported late so the red run on main fails per test, not at collection.
    from finance.transfer_pairing import restore_transfer_pair

    return restore_transfer_pair(principal, pair_id)


def _client(person):
    client = Client()
    client.force_login(person.user)
    return client


def _world(pair_count=3):
    """A household checking account and card with `pair_count` auto-marked card payments."""
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    checking = make_account(owner, name="Synthetic Joint Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    card = make_account(
        owner, name="Synthetic Joint Card", account_type=Account.Type.CREDIT_CARD,
        scope=Account.Scope.HOUSEHOLD, household=household,
    )
    private = make_account(owner, name="Synthetic Owner Savings", account_type=Account.Type.SAVINGS)
    for index in range(pair_count):
        amount = 10_000 + 100 * index
        day = date(2026, 3, 1) + timedelta(days=3 * index)
        make_transaction(owner, checking, amount_minor=-amount, transaction_date=day, description=f"SYN CARD PAYMENT {index}")
        make_transaction(owner, card, amount_minor=amount, transaction_date=day, description=f"SYN PAYMENT THANK YOU {index}")
    # Ordinary activity, so totals have something besides the pairs.
    make_transaction(owner, checking, amount_minor=250_000, transaction_date=date(2026, 3, 1), description="SYN PAYROLL")
    make_transaction(owner, card, amount_minor=-4_321, transaction_date=date(2026, 3, 2), description="SYN GROCER")
    refresh_transfer_pairs(owner)
    pairs = list(TransferPair.objects.order_by("pk"))
    assert len(pairs) == pair_count
    assert all(pair.status == TransferPair.Status.AUTO_MARKED for pair in pairs)
    return owner, member, household, checking, card, private, pairs


def _spread_timestamps(pairs):
    """Distinct created_at, newest pk newest, and updated_at in the opposite order.

    Under the old `-updated_at` key the list would be reversed, so a test that
    passes cannot be relying on updated_at.
    """
    base = timezone.now() - timedelta(days=1)
    for index, pair in enumerate(pairs):
        TransferPair.objects.filter(pk=pair.pk).update(
            created_at=base + timedelta(minutes=index),
            updated_at=base + timedelta(hours=1, minutes=-index),
        )


def _forms(html):
    """Every pair form on the page, top to bottom, as (pair id, action)."""
    found = []
    for body in FORM.findall(html):
        pair_id, action = PAIR_ID.search(body), ACTION.search(body)
        if pair_id and action:
            found.append((int(pair_id.group(1)), action.group(1)))
    return found


def _excluded_section(html):
    start = html.index(EXCLUDED_START)
    end = html.find(UNDONE_START, start)
    return html[start:] if end == -1 else html[start:end]


def _statuses():
    return dict(TransferPair.objects.values_list("pk", "status"))


def test_excluded_list_orders_by_creation_not_by_last_update():
    owner, *_rest, pairs = _world()
    _spread_timestamps(pairs)
    response = _client(owner).get(reverse("transfer-review"))
    rows = [pair_id for pair_id, _action in _forms(_excluded_section(response.content.decode()))]
    assert rows == [pair.pk for pair in reversed(pairs)]


def test_undo_keeps_every_row_in_place_and_offers_restore_where_undo_was():
    owner, *_rest, pairs = _world()
    _spread_timestamps(pairs)
    client = _client(owner)
    before = _forms(_excluded_section(client.get(reverse("transfer-review")).content.decode()))
    assert [action for _pk, action in before] == ["undo", "undo", "undo"]
    first = before[0][0]

    response = client.post(reverse("transfer-review"), {"action": "undo", "pair_id": first}, follow=True)

    assert response.status_code == 200
    after = _forms(_excluded_section(response.content.decode()))
    assert [pk for pk, _action in after] == [pk for pk, _action in before]
    assert after[0] == (first, "restore")
    assert after[1:] == before[1:]
    statuses = _statuses()
    assert statuses[first] == TransferPair.Status.UNDONE
    assert all(statuses[pk] == TransferPair.Status.AUTO_MARKED for pk, _action in before[1:])


def test_ten_fast_clicks_on_one_spot_change_at_most_one_pair():
    owner, *_rest, pairs = _world(pair_count=5)
    _spread_timestamps(pairs)
    client = _client(owner)
    start = _statuses()
    url = reverse("transfer-review")
    page = client.get(url)
    # The cursor rests on the first Undo button on the page.
    spot = next(i for i, (_pk, action) in enumerate(_forms(page.content.decode())) if action == "undo")
    for _click in range(10):
        pair_id, action = _forms(page.content.decode())[spot]
        page = client.post(url, {"action": action, "pair_id": pair_id}, follow=True)
        assert page.status_code == 200
        url = page.redirect_chain[-1][0]

    changed = {pk for pk, status in _statuses().items() if status != start[pk]}
    assert len(changed) <= 1
    for pk in changed:
        assert _statuses()[pk] in (TransferPair.Status.UNDONE, TransferPair.Status.CONFIRMED)


def test_undo_records_who_undid_the_pair_and_when():
    owner, *_rest, pairs = _world(pair_count=1)
    before = timezone.now()
    undo_transfer_pair(owner, pairs[0].pk)
    pair = TransferPair.objects.get(pk=pairs[0].pk)
    assert pair.status == TransferPair.Status.UNDONE
    assert pair.undone_by == owner
    assert pair.undone_at is not None and pair.undone_at >= before


def _totals_by_scope(people):
    """Cash flow, income and spending for every scope each person can choose."""
    found = {}
    for person in people:
        for scope in ("", Account.Scope.HOUSEHOLD, Account.Scope.PRIVATE):
            accounts = selected_accounts(person, scope=scope, cash_flow_only=True)
            report = cash_flow_report(person, date_from=date(2026, 3, 1), date_to=date(2026, 3, 31), scope=scope)
            found[(person.pk, scope)] = (
                income_and_spending_totals(person, accounts=accounts),
                [(period.income_minor, period.spending_minor, period.net_minor) for period in report.periods],
            )
    return found


def test_restore_from_the_page_confirms_the_pair_and_brings_totals_back():
    owner, member, household, checking, card, _private, pairs = _world()
    groceries = household.categories.get(name="Groceries")
    totals_before = _totals_by_scope((owner, member))
    target = pairs[1]
    client = _client(owner)
    client.post(reverse("transfer-review"), {"action": "undo", "pair_id": target.pk})
    assert _totals_by_scope((owner, member)) != totals_before
    # Categories change while undone; restore snapshots the current ones, as confirm does.
    Transaction.objects.filter(pk=target.leg_a_id).update(category=groceries)
    history_before = TransactionCorrectionHistory.objects.count()

    response = client.post(reverse("transfer-review"), {"action": "restore", "pair_id": target.pk})

    assert response.status_code == 302
    pair = TransferPair.objects.get(pk=target.pk)
    assert pair.status == TransferPair.Status.CONFIRMED
    assert pair.undone_at is None and pair.undone_by is None
    assert pair.leg_a_category_id_at_mark == groceries.pk
    assert pair.leg_b_category_id_at_mark is None
    assert _totals_by_scope((owner, member)) == totals_before
    event = AuditEvent.objects.filter(action=AuditEvent.Action.RECORD_ENABLED).latest("pk")
    assert event.target_type == AuditEvent.TargetType.TRANSACTION
    assert {event.target_id, event.metadata["transaction_id"]} == {target.leg_a_id, target.leg_b_id}
    exclusions = TransactionCorrectionHistory.objects.filter(
        field_name=TransactionCorrectionHistory.Field.EXCLUSION
    ).order_by("-pk")[:2]
    assert TransactionCorrectionHistory.objects.count() > history_before
    assert {row.transaction_id for row in exclusions} == {target.leg_a_id, target.leg_b_id}
    assert {(row.previous_description, row.new_description) for row in exclusions} == {
        ("included", "excluded:card payment")
    }


def test_a_pair_restored_then_undone_again_can_be_restored_again():
    owner, *_rest, pairs = _world(pair_count=1)
    pair_id = pairs[0].pk
    for _round in range(2):
        undo_transfer_pair(owner, pair_id)
        _restore(owner, pair_id)
    assert TransferPair.objects.get(pk=pair_id).status == TransferPair.Status.CONFIRMED


def test_restore_refuses_a_pair_that_is_not_undone():
    owner, *_rest, pairs = _world(pair_count=1)
    with pytest.raises(PermissionDenied):
        _restore(owner, pairs[0].pk)
    assert TransferPair.objects.get(pk=pairs[0].pk).status == TransferPair.Status.AUTO_MARKED


def test_restore_refuses_an_invalidated_suggestion_nobody_undid():
    owner, *_rest, pairs = _world(pair_count=1)
    TransferPair.objects.filter(pk=pairs[0].pk).update(status=TransferPair.Status.UNDONE)
    with pytest.raises(PermissionDenied):
        _restore(owner, pairs[0].pk)
    response = _client(owner).post(reverse("transfer-review"), {"action": "restore", "pair_id": pairs[0].pk})
    assert response.status_code == 404
    assert TransferPair.objects.get(pk=pairs[0].pk).status == TransferPair.Status.UNDONE


def _private_leg_pair():
    owner, member, household, checking, _card, private, _pairs = _world(pair_count=0)
    make_transaction(owner, private, amount_minor=-7_700, transaction_date=date(2026, 4, 1), description="SYN TRANSFER TO JOINT")
    make_transaction(owner, checking, amount_minor=7_700, transaction_date=date(2026, 4, 1), description="SYN TRANSFER FROM SAVINGS")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    undo_transfer_pair(owner, pair.pk)
    return owner, member, pair


def test_restore_refuses_a_member_who_cannot_see_a_leg():
    _owner, member, pair = _private_leg_pair()
    with pytest.raises(PermissionDenied):
        _restore(member, pair.pk)
    response = _client(member).post(reverse("transfer-review"), {"action": "restore", "pair_id": pair.pk})
    assert response.status_code == 404
    assert TransferPair.objects.get(pk=pair.pk).status == TransferPair.Status.UNDONE


def test_recently_undone_never_shows_a_pair_with_a_leg_the_viewer_cannot_see():
    owner, member, pair = _private_leg_pair()
    owner_page = _client(owner).get(reverse("transfer-review")).content.decode()
    assert (pair.pk, "restore") in _forms(owner_page[owner_page.index(UNDONE_START):])
    member_client = _client(member)
    for url in (reverse("transfer-review"), f"{reverse('transfer-review')}?undone={pair.pk}"):
        page = member_client.get(url).content.decode()
        assert pair.pk not in {pk for pk, _action in _forms(page)}
        assert "SYN TRANSFER TO JOINT" not in page


@pytest.mark.parametrize(
    "breaks",
    [
        pytest.param(lambda leg: {"category_source": Transaction.CategorySource.SPLIT}, id="split leg"),
        pytest.param(lambda leg: {"transaction_date": leg.transaction_date + timedelta(days=40)}, id="outside window"),
        pytest.param(lambda leg: {"amount_minor": leg.amount_minor + 1}, id="amounts differ"),
        pytest.param(lambda leg: {"status": Transaction.Status.ARCHIVED, "archived_at": timezone.now()}, id="inactive leg"),
        pytest.param(lambda leg: {"untracked_transfer": True}, id="untracked mark"),
    ],
)
def test_restore_refuses_when_a_leg_no_longer_qualifies(breaks):
    owner, *_rest, pairs = _world(pair_count=1)
    pair = pairs[0]
    undo_transfer_pair(owner, pair.pk)
    leg = Transaction.objects.get(pk=pair.leg_a_id)
    Transaction.objects.filter(pk=leg.pk).update(**breaks(leg))
    with pytest.raises(PermissionDenied):
        _restore(owner, pair.pk)
    assert TransferPair.objects.get(pk=pair.pk).status == TransferPair.Status.UNDONE


@pytest.mark.parametrize("other_status", [TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED])
def test_restore_refuses_when_a_leg_now_belongs_to_another_excluding_pair(other_status):
    owner, _member, _household, _checking, card, _private, pairs = _world(pair_count=1)
    pair = pairs[0]
    undo_transfer_pair(owner, pair.pk)
    checking_leg = Transaction.objects.get(pk=pair.leg_a_id)
    partner = make_transaction(
        owner, card, amount_minor=-checking_leg.amount_minor, transaction_date=checking_leg.transaction_date,
        description="SYN SECOND PAYMENT", fingerprint="c" * 64,
    )
    TransferPair.objects.create(
        leg_a=checking_leg, leg_b=partner, status=other_status, kind=TransferPair.Kind.CARD_PAYMENT,
        confidence=TransferPair.Confidence.HIGH, reasons=["synthetic"],
    )
    totals_before = _totals_by_scope((owner,))
    with pytest.raises(PermissionDenied):
        _restore(owner, pair.pk)
    assert TransferPair.objects.get(pk=pair.pk).status == TransferPair.Status.UNDONE
    assert _totals_by_scope((owner,)) == totals_before


def test_restore_retires_a_competing_suggestion_so_no_leg_is_excluded_twice():
    owner, _member, _household, _checking, card, _private, pairs = _world(pair_count=1)
    pair = pairs[0]
    client = _client(owner)
    checking_leg = Transaction.objects.get(pk=pair.leg_a_id)
    client.post(reverse("transfer-review"), {"action": "undo", "pair_id": pair.pk})
    # A second card credit of the same amount arrives while the pair is undone.
    make_transaction(
        owner, card, amount_minor=-checking_leg.amount_minor, transaction_date=checking_leg.transaction_date,
        description="SYN REFUND", fingerprint="d" * 64,
    )
    refresh_transfer_pairs(owner)
    competing = TransferPair.objects.exclude(pk=pair.pk).get()
    assert competing.status == TransferPair.Status.SUGGESTED

    client.post(reverse("transfer-review"), {"action": "restore", "pair_id": pair.pk})

    assert TransferPair.objects.get(pk=pair.pk).status == TransferPair.Status.CONFIRMED
    assert not TransferPair.objects.filter(pk=competing.pk).exists()
    response = client.post(reverse("transfer-review"), {"action": "confirm", "pair_id": competing.pk})
    assert response.status_code == 404
    assert TransferPair.objects.excluding_income_and_spending().filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as in the baseline
        leg_a=checking_leg
    ).count() + TransferPair.objects.excluding_income_and_spending().filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as in the baseline
        leg_b=checking_leg
    ).count() == 1


def test_recently_undone_lists_member_undos_newest_first_and_only_the_last_30_days():
    owner, *_rest, pairs = _world(pair_count=3)
    for pair in pairs:
        undo_transfer_pair(owner, pair.pk)
    now = timezone.now()
    TransferPair.objects.filter(pk=pairs[0].pk).update(undone_at=now - timedelta(days=31))
    TransferPair.objects.filter(pk=pairs[1].pk).update(undone_at=now - timedelta(days=2))
    TransferPair.objects.filter(pk=pairs[2].pk).update(undone_at=now - timedelta(days=1))
    page = _client(owner).get(reverse("transfer-review")).content.decode()
    listed = _forms(page[page.index(UNDONE_START):])
    assert listed == [(pairs[2].pk, "restore"), (pairs[1].pk, "restore")]


def test_suggestions_order_by_creation_not_by_last_update():
    owner, *_rest, pairs = _world()
    TransferPair.objects.update(status=TransferPair.Status.SUGGESTED)
    _spread_timestamps(pairs)
    response = _client(owner).get(reverse("transfer-review"))
    suggestions = [pk for pk, action in _forms(response.content.decode()) if action == "confirm"]
    assert suggestions == [pair.pk for pair in reversed(pairs)]


def test_a_suggestion_restore_removed_comes_back_when_the_pair_is_undone_again():
    owner, _member, _household, _checking, card, _private, pairs = _world(pair_count=1)
    pair = pairs[0]
    client = _client(owner)
    checking_leg = Transaction.objects.get(pk=pair.leg_a_id)
    client.post(reverse("transfer-review"), {"action": "undo", "pair_id": pair.pk})
    second = make_transaction(
        owner, card, amount_minor=-checking_leg.amount_minor, transaction_date=checking_leg.transaction_date,
        description="SYN REFUND", fingerprint="d" * 64,
    )
    refresh_transfer_pairs(owner)
    client.post(reverse("transfer-review"), {"action": "restore", "pair_id": pair.pk})
    assert not TransferPair.objects.filter(leg_b=second).exists()

    client.post(reverse("transfer-review"), {"action": "undo", "pair_id": pair.pk})

    assert TransferPair.objects.get(leg_a=checking_leg, leg_b=second).status == TransferPair.Status.SUGGESTED


def test_garbage_in_the_undone_parameter_is_ignored():
    owner, *_rest, pairs = _world()
    undo_transfer_pair(owner, pairs[0].pk)
    query = f"abc,\u00b2,-1,99999999999999999999999,{pairs[0].pk},,"
    response = _client(owner).get(reverse("transfer-review"), {"undone": query})
    assert response.status_code == 200
    excluded = _forms(_excluded_section(response.content.decode()))
    assert (pairs[0].pk, "restore") in excluded
    assert len(excluded) == 3
