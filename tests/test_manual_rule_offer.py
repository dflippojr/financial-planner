from unittest.mock import patch

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from finance.category_services import assign_category, split_transaction
from finance.models import Account, Category, CategoryRule, CategorySuggestion, Membership, Transaction
from finance.rule_services import preview_unsaved_rule, unsaved_rule
from tests.test_category_rules import dining, groceries, make_account, make_household, make_person, make_transaction


COFFEE = "SYNTHETIC COFFEE"


def _setup(others=2):
    owner = make_person("offer-owner")
    household = make_household(owner)
    account = make_account(owner)
    edited = make_transaction(owner, account, amount_minor=-450, description=COFFEE)
    rest = [
        make_transaction(owner, account, amount_minor=-500 - index, description=f"{COFFEE} #{index}")
        for index in range(others)
    ]
    client = Client()
    client.force_login(owner.user)
    return owner, household, account, edited, rest, client


def _offer(client, txn):
    return client.get(reverse("transaction-edit", args=(txn.pk,))).context["rule_offer"]


def _categorize(client, txn, category):
    return client.post(
        reverse("transaction-categorize", args=(txn.pk,)), {"category": str(category.pk)}, follow=True
    )


@pytest.mark.django_db
def test_manual_category_offers_a_prefilled_rule():
    owner, household, account, edited, rest, client = _setup()
    response = _categorize(client, edited, dining(household))
    assert response.status_code == 200
    offer = response.context["rule_offer"]
    assert offer["count"] == 2
    html = response.content.decode()
    assert "2 other transactions contain “SYNTHETIC COFFEE” and aren't in Dining." in html
    assert "Preview a rule for “SYNTHETIC COFFEE”" in html
    form = client.get(offer["url"]).context["form"]
    assert form.initial["description_contains"] == COFFEE
    assert str(form.initial["category"]) == str(dining(household).pk)


@pytest.mark.django_db
def test_count_matches_unsaved_rule_preview_and_skips_manual_rows():
    owner, household, account, edited, rest, client = _setup(others=3)
    rest[0].category = dining(household)
    rest[0].category_source = Transaction.CategorySource.RULE
    rest[0].save()
    hand_set = make_transaction(owner, account, amount_minor=-777, description=COFFEE.lower())
    assign_category(owner, hand_set.pk, groceries(household).pk)
    assign_category(owner, edited.pk, dining(household).pk)
    offer = _offer(client, edited)
    rule = unsaved_rule(
        owner, owner_kind="personal", description_contains=offer["description_contains"],
        category_id=dining(household).pk,
    )
    changed = [txn for txn in preview_unsaved_rule(owner, rule) if txn.category_id != rule.category_id]
    assert {txn.pk for txn in changed} == {rest[1].pk, rest[2].pk}
    assert offer["count"] == len(changed) == 2


@pytest.mark.django_db
def test_no_offer_with_fewer_than_two_changeable_others():
    owner, household, account, edited, rest, client = _setup(others=1)
    assert _categorize(client, edited, dining(household)).context["rule_offer"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("code", [Category.Code.TRANSFER, Category.Code.UNCATEGORIZED])
def test_no_offer_for_transfer_or_uncategorized(code):
    owner, household, account, edited, rest, client = _setup()
    category = Category.objects.get(household=household, code=code)
    Transaction.objects.filter(pk=edited.pk).update(
        category=category, category_source=Transaction.CategorySource.MANUAL
    )
    response = client.get(reverse("transaction-edit", args=(edited.pk,)))
    assert response.context["rule_offer"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("source", [
    Transaction.CategorySource.RULE,
    Transaction.CategorySource.INHERITED,
    Transaction.CategorySource.UNSET,
])
def test_no_offer_unless_the_category_was_set_by_hand(source):
    owner, household, account, edited, rest, client = _setup()
    Transaction.objects.filter(pk=edited.pk).update(category=dining(household), category_source=source)
    response = client.get(reverse("transaction-edit", args=(edited.pk,)))
    assert response.context["rule_offer"] is None


@pytest.mark.django_db
def test_no_offer_for_a_split_transaction():
    owner, household, account, edited, rest, client = _setup()
    assign_category(owner, edited.pk, dining(household).pk)
    split_transaction(owner, edited.pk, [
        {"category_id": dining(household).pk, "amount_minor": -200},
        {"category_id": groceries(household).pk, "amount_minor": -250},
    ])
    assert client.get(reverse("transaction-edit", args=(edited.pk,))).context["rule_offer"] is None


@pytest.mark.django_db
def test_no_offer_for_an_accepted_ai_suggestion():
    owner, household, account, edited, rest, client = _setup()
    CategorySuggestion.objects.create(
        member=owner, transaction=edited, category=dining(household), provider="synthetic", backend="synthetic",
        status=CategorySuggestion.Status.ACCEPTED, snapshot_hash="c" * 64,
    )
    assert _categorize(client, edited, dining(household)).context["rule_offer"] is None


@pytest.mark.django_db
def test_no_offer_when_an_identical_visible_rule_exists():
    owner, household, account, edited, rest, client = _setup()
    CategoryRule.objects.create(
        owner_person=owner, description_contains="synthetic coffee", category=dining(household), enabled=False
    )
    assert _categorize(client, edited, dining(household)).context["rule_offer"] is None
    assert _categorize(client, edited, groceries(household)).context["rule_offer"]["count"] == 2


@pytest.mark.django_db
def test_another_members_private_rows_never_count():
    owner, household, account, edited, rest, client = _setup(others=1)
    other = make_person("offer-other")
    Membership.objects.create(person=other, household=household)
    private = make_account(other, name="Synthetic Private Card")
    for index in range(3):
        make_transaction(other, private, amount_minor=-900 - index, description=f"{COFFEE} PRIVATE {index}")
    shared = make_account(other, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    response = _categorize(client, edited, dining(household))
    assert response.context["rule_offer"] is None
    make_transaction(other, shared, amount_minor=-901, description=f"{COFFEE} SHARED")
    response = _categorize(client, edited, groceries(household))
    assert response.context["rule_offer"]["count"] == 2
    html = response.content.decode()
    assert "PRIVATE" not in html
    assert "Synthetic Private Card" not in html


@pytest.mark.django_db
def test_offer_adds_at_most_two_queries():
    owner, household, account, edited, rest, client = _setup()
    assign_category(owner, edited.pk, dining(household).pk)
    url = reverse("transaction-edit", args=(edited.pk,))
    client.get(url)
    with CaptureQueriesContext(connection) as with_offer:
        assert client.get(url).context["rule_offer"]["count"] == 2
    with patch("finance.views.manual_rule_offer", return_value=None), CaptureQueriesContext(connection) as without:
        client.get(url)
    assert len(with_offer) - len(without) <= 2


@pytest.mark.django_db
def test_count_follows_existing_rule_precedence_like_the_preview():
    owner, household, account, edited, rest, client = _setup(others=3)
    other = make_person("offer-housemate")
    Membership.objects.create(person=other, household=household)
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    shared_rows = [
        make_transaction(owner, shared, amount_minor=-600 - index, description=f"{COFFEE} SHARED {index}")
        for index in range(2)
    ]
    # Wins on every private row: an earlier personal rule at priority 0.
    CategoryRule.objects.create(
        owner_person=owner, account=account, description_contains="coffee", category=groceries(household)
    )
    # Loses: priority 5 sorts after the new rule.
    CategoryRule.objects.create(
        owner_person=owner, description_contains="coffee #1", category=groceries(household), priority=5
    )
    # A housemate's personal rule reaches the shared rows only.
    CategoryRule.objects.create(
        owner_person=other, description_contains="shared 0", category=groceries(household)
    )
    assign_category(owner, edited.pk, dining(household).pk)
    rule = unsaved_rule(owner, owner_kind="personal", description_contains=COFFEE, category_id=dining(household).pk)
    expected = {txn.pk for txn in preview_unsaved_rule(owner, rule) if txn.category_id != rule.category_id}
    assert expected == {shared_rows[1].pk}
    assert _offer(client, edited) is None
    make_transaction(owner, shared, amount_minor=-700, description=f"{COFFEE} SHARED 9")
    expected = {txn.pk for txn in preview_unsaved_rule(owner, rule) if txn.category_id != rule.category_id}
    assert len(expected) == 2
    assert _offer(client, edited)["count"] == 2


@pytest.mark.django_db
def test_matching_folds_case_like_the_rule_preview():
    owner = make_person("offer-fold")
    household = make_household(owner)
    account = make_account(owner)
    edited = make_transaction(owner, account, amount_minor=-450, description="SYNTHETIC STRASSE CAFE")
    for index in range(2):
        make_transaction(owner, account, amount_minor=-500 - index, description=f"Synthetic Straße Cafe {index}")
    assign_category(owner, edited.pk, dining(household).pk)
    client = Client()
    client.force_login(owner.user)
    rule = unsaved_rule(
        owner, owner_kind="personal", description_contains="SYNTHETIC STRASSE CAFE", category_id=dining(household).pk
    )
    assert len(preview_unsaved_rule(owner, rule)) == 2
    assert _offer(client, edited)["count"] == 2


@pytest.mark.django_db
def test_household_rules_never_block_a_new_personal_rule():
    owner = make_person("offer-household-rule")
    household = make_household(owner)
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    edited = make_transaction(owner, shared, amount_minor=-450, description=COFFEE)
    others = [
        make_transaction(owner, shared, amount_minor=-500 - index, description=f"{COFFEE} {index}")
        for index in range(2)
    ]
    CategoryRule.objects.create(owner_household=household, description_contains="coffee", category=groceries(household))
    assign_category(owner, edited.pk, dining(household).pk)
    client = Client()
    client.force_login(owner.user)
    personal = unsaved_rule(owner, owner_kind="personal", description_contains=COFFEE, category_id=dining(household).pk)
    assert {txn.pk for txn in preview_unsaved_rule(owner, personal)} == {txn.pk for txn in others}
    assert _offer(client, edited)["count"] == 2
    # The reverse holds for a household rule: any matching personal rule runs first.
    CategoryRule.objects.create(
        owner_person=owner, description_contains="coffee 0", category=groceries(household), priority=9
    )
    household_rule = unsaved_rule(
        owner, owner_kind="household", description_contains=COFFEE, category_id=dining(household).pk, priority=-1
    )
    assert {txn.pk for txn in preview_unsaved_rule(owner, household_rule)} == {others[1].pk}
