"""Per-row transfer state is judged by the viewer: a pair counts only when both legs are visible."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.urls import reverse

from finance.category_services import (
    SPLIT_TRANSFER_ERROR,
    exclusion_exists_for,
    refresh_transfer_pairs,
    with_transfer_state,
)
from finance.chat_proposals import card_for
from finance.models import Account, AiProposal, Transaction, TransferPair
from finance.policy_services import accept_policy, publish_policy
from tests.test_categorization import make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db


def _cross_scope_pair():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private_leg = make_transaction(owner, private, amount_minor=-3000, description="Synthetic transfer out")
    shared_leg = make_transaction(owner, shared, amount_minor=3000, description="Synthetic transfer in")
    refresh_transfer_pairs(owner)
    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    return owner, member, household, private_leg, shared_leg


def _client(person):
    client = Client()
    client.force_login(person.user)
    return client


def _split_post(household):
    groceries = household.categories.get(name="Groceries")
    return {
        "part_count": "2",
        "part_0_category": str(groceries.pk),
        "part_0_amount": "10.00",
        "part_1_category": str(groceries.pk),
        "part_1_amount": "20.00",
    }


def _card_label(person, txn):
    proposal = AiProposal(
        kind=AiProposal.Kind.SET_CATEGORY,
        payload={"items": [{"id": txn.pk}], "category_id": None},
    )
    [row] = card_for(person, proposal)["transactions"]
    return row.category_display


def test_member_edit_page_and_split_treat_a_pair_they_cannot_see_as_no_pair():
    _owner, member, household, _private_leg, shared_leg = _cross_scope_pair()
    client = _client(member)

    edit = client.get(reverse("transaction-edit", args=(shared_leg.pk,)))
    assert edit.status_code == 200
    assert b'id="split-form"' in edit.content
    assert b"Link a refund" in edit.content
    assert edit.context["can_link_refund"] is True

    response = client.post(reverse("transaction-split", args=(shared_leg.pk,)), _split_post(household))
    assert response.status_code == 302
    shared_leg.refresh_from_db()
    assert shared_leg.category_source == Transaction.CategorySource.SPLIT
    assert shared_leg.splits.count() == 2


def test_owner_who_sees_both_legs_keeps_transfer_behaviour():
    owner, _member, household, _private_leg, shared_leg = _cross_scope_pair()
    client = _client(owner)

    edit = client.get(reverse("transaction-edit", args=(shared_leg.pk,)))
    assert edit.status_code == 200
    assert b'id="split-form"' not in edit.content
    assert b"Link a refund" not in edit.content

    response = client.post(reverse("transaction-split", args=(shared_leg.pk,)), _split_post(household))
    assert response.status_code == 200
    assert response.context["split_form"].non_field_errors() == [SPLIT_TRANSFER_ERROR]
    shared_leg.refresh_from_db()
    assert shared_leg.category_source != Transaction.CategorySource.SPLIT
    assert not shared_leg.splits.exists()


def test_owner_edit_page_rerender_after_a_failed_post_keeps_transfer_state():
    owner, _member, _household, _private_leg, shared_leg = _cross_scope_pair()
    response = _client(owner).post(
        reverse("transaction-edit", args=(shared_leg.pk,)),
        {"transaction_date": "not-a-date", "description": "x", "amount": "30.00"},
    )
    assert response.status_code == 200
    assert b'id="split-form"' not in response.content


def test_chat_card_label_matches_each_viewers_transaction_list():
    owner, member, _household, _private_leg, shared_leg = _cross_scope_pair()
    policy = publish_policy(material=True, body="Synthetic privacy policy for AI tests")
    for person in (owner, member):
        accept_policy(person, policy)

    member_list = (
        Transaction.objects.visible_to(member)
        .annotate(_excluded=exclusion_exists_for(member))
        .get(pk=shared_leg.pk)
    )
    assert _card_label(member, shared_leg) == member_list.category_display == "Uncategorized"
    assert _card_label(owner, shared_leg) == "Transfer"


def test_transfer_state_never_falls_back_to_pairs_the_viewer_cannot_see():
    owner, member, _household, _private_leg, shared_leg = _cross_scope_pair()

    unannotated = Transaction.objects.get(pk=shared_leg.pk)
    with pytest.raises(ImproperlyConfigured):
        unannotated.is_excluded_transfer
    with pytest.raises(ImproperlyConfigured):
        unannotated.category_display

    assert with_transfer_state(member, Transaction.objects.get(pk=shared_leg.pk)).is_excluded_transfer is False
    assert with_transfer_state(owner, Transaction.objects.get(pk=shared_leg.pk)).is_excluded_transfer is True


def test_transfer_review_lists_a_pair_only_for_a_viewer_who_sees_both_legs():
    owner, member, _household, _private_leg, _shared_leg = _cross_scope_pair()

    owner_page = _client(owner).get(reverse("transfer-review")).content.decode()
    member_page = _client(member).get(reverse("transfer-review")).content.decode()

    assert "Owner Private" in owner_page and "Shared Checking" in owner_page
    assert "Owner Private" not in member_page and "Shared Checking" not in member_page
    assert "No automatic or confirmed exclusions." in member_page
