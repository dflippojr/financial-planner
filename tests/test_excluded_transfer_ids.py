"""Characterization of the excluded-transfer ids that unusual spending and the monthly review each compute.

Both call sites read the same pair-visible rule: a pair is excluded when it is
auto-marked or confirmed, both legs are active, and the viewer sees both legs.
A household pair counts for every member, a pair with one private leg only for
a viewer who sees both legs, and an unmarked pair for nobody. #599 moves the two
copies into one helper; these assertions hold before and after that move.
"""

from datetime import date

import pytest
from django.utils import timezone

from finance import monthly_review, unusual_spending
from finance.models import Account, Transaction, TransferPair
from tests.test_categorization import make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db

SEPTEMBER = date(2026, 9, 10)


def _pair(first, second, status):
    low, high = sorted((first, second), key=lambda txn: txn.pk)
    return TransferPair.objects.create(
        leg_a=low,
        leg_b=high,
        status=status,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.HIGH,
        reasons=["synthetic"],
    )


def _excluded_ids(viewer):
    from_unusual = unusual_spending._excluded_transfer_ids(viewer)
    from_review = monthly_review._excluded_transfer_ids(viewer)
    assert from_unusual == from_review
    return from_unusual


@pytest.fixture
def world():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    checking = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    savings = make_account(owner, name="Shared Savings", scope=Account.Scope.HOUSEHOLD, household=household)

    def txn(account, amount_minor, description):
        return make_transaction(owner, account, transaction_date=SEPTEMBER, amount_minor=amount_minor, description=description)

    household_out = txn(checking, -1000, "Synthetic household out")
    household_in = txn(savings, 1000, "Synthetic household in")
    private_out = txn(private, -2500, "Synthetic private out")
    shared_in = txn(savings, 2500, "Synthetic private counterpart in")
    unmarked_a = txn(checking, -700, "Synthetic suggested out")
    unmarked_b = txn(savings, 700, "Synthetic suggested in")
    dismissed_a = txn(checking, -900, "Synthetic dismissed out")
    dismissed_b = txn(savings, 900, "Synthetic dismissed in")
    archived_leg = txn(checking, -300, "Synthetic archived out")
    archived_partner = txn(savings, 300, "Synthetic archived in")
    Transaction.objects.filter(pk=archived_leg.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())

    _pair(household_out, household_in, TransferPair.Status.AUTO_MARKED)
    _pair(private_out, shared_in, TransferPair.Status.CONFIRMED)
    _pair(unmarked_a, unmarked_b, TransferPair.Status.SUGGESTED)
    _pair(dismissed_a, dismissed_b, TransferPair.Status.DISMISSED)
    _pair(archived_leg, archived_partner, TransferPair.Status.AUTO_MARKED)

    return {
        "owner": owner,
        "member": member,
        "household": {household_out.pk, household_in.pk},
        "one_private_leg": {private_out.pk, shared_in.pk},
        "unmarked": {unmarked_a.pk, unmarked_b.pk, dismissed_a.pk, dismissed_b.pk, archived_leg.pk, archived_partner.pk},
    }


def test_member_sees_only_the_household_pair(world):
    assert _excluded_ids(world["member"]) == world["household"]


def test_owner_sees_the_pair_with_their_private_leg_as_well(world):
    assert _excluded_ids(world["owner"]) == world["household"] | world["one_private_leg"]


def test_unmarked_and_archived_pairs_are_excluded_for_nobody(world):
    for viewer in (world["owner"], world["member"]):
        assert not _excluded_ids(viewer) & world["unmarked"]
