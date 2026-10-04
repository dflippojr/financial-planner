from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.category_services import ensure_household_categories, split_transaction
from finance.lifecycle_services import unshare_account
from finance.models import (
    Account,
    CategorySuggestion,
    Household,
    ImportBatch,
    Membership,
    Person,
    SavedTransactionFilter,
    Transaction,
)
from finance.transaction_filters import PAGE_SIZE


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    description="Synthetic groceries",
    amount_minor=-1234,
    fingerprint=None,
    transaction_date=date(2026, 1, 2),
    note="",
    original_fields=None,
    category_source="",
    category=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or (
        f"{account.pk}-{amount_minor}-{transaction_date}-{description}-{note}".encode().hex().ljust(64, "a")[:64]
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        note=note,
        source_row_number=2,
        fingerprint=digest,
        original_fields=original_fields or {"Synthetic Amount": str(amount_minor)},
        category_source=category_source,
        category=category,
    )


def ids(response):
    return [txn.pk for txn in response.context["transactions"]]


@pytest.mark.django_db
def test_search_matches_description_payee_memo_and_note_without_private_leak():
    owner = make_person("owner")
    other = make_person("other")
    owner_account = make_account(owner, name="Owner checking")
    other_account = make_account(other, name="Other checking")
    by_description = make_transaction(owner, owner_account, description="Synthetic coffee shop")
    by_payee = make_transaction(
        owner,
        owner_account,
        description="Card purchase",
        original_fields={"Payee": "Synthetic coffee mill", "Memo": "weekday"},
    )
    by_memo = make_transaction(
        owner,
        owner_account,
        description="POS",
        original_fields={"payee": "Store", "memo": "Synthetic coffee beans"},
    )
    by_note = make_transaction(owner, owner_account, description="Market", note="Synthetic coffee run")
    make_transaction(owner, owner_account, description="Unrelated hardware")
    leaked = make_transaction(
        other,
        other_account,
        description="Hidden",
        note="Synthetic coffee secret",
        original_fields={"Payee": "Synthetic coffee vault"},
    )
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("transaction-list"), {"q": "coffee"})

    assert set(ids(response)) == {by_description.pk, by_payee.pk, by_memo.pk, by_note.pk}
    assert leaked.pk not in ids(response)
    assert "Synthetic coffee secret" not in response.content.decode()


@pytest.mark.django_db
def test_amount_has_note_split_and_set_by_filters_alone_and_combined():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    spend_small = make_transaction(owner, account, description="Small out", amount_minor=-500)
    spend_mid = make_transaction(owner, account, description="Mid out", amount_minor=-2500, note="Has a note")
    income = make_transaction(owner, account, description="Paycheck", amount_minor=2500)
    hand = make_transaction(
        owner,
        account,
        description="Hand set",
        amount_minor=-2500,
        category=groceries,
        category_source=Transaction.CategorySource.MANUAL,
    )
    ruled = make_transaction(
        owner,
        account,
        description="Rule set",
        amount_minor=-2500,
        category=dining,
        category_source=Transaction.CategorySource.RULE,
    )
    suggested = make_transaction(
        owner,
        account,
        description="AI set",
        amount_minor=-2500,
        category=dining,
        category_source=Transaction.CategorySource.MANUAL,
    )
    CategorySuggestion.objects.create(
        member=owner,
        transaction=suggested,
        category=dining,
        provider="agent_harness",
        backend="local",
        status=CategorySuggestion.Status.ACCEPTED,
        snapshot_hash="a" * 64,
    )
    split = make_transaction(owner, account, description="Split store", amount_minor=-10000)
    split_transaction(
        owner,
        split.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -6000},
            {"category_id": dining.pk, "amount_minor": -4000},
        ),
    )
    client = Client()
    client.force_login(owner.user)
    url = reverse("transaction-list")

    signed = client.get(url, {"amount_min": "-30.00", "amount_max": "-20.00", "amount_mode": "signed"})
    assert set(ids(signed)) == {spend_mid.pk, hand.pk, ruled.pk, suggested.pk}

    absolute = client.get(url, {"amount_min": "20.00", "amount_max": "30.00", "amount_mode": "absolute"})
    assert set(ids(absolute)) == {spend_mid.pk, income.pk, hand.pk, ruled.pk, suggested.pk}

    noted = client.get(url, {"has_note": "1"})
    assert ids(noted) == [spend_mid.pk]

    splits = client.get(url, {"is_split": "1"})
    assert ids(splits) == [split.pk]

    by_hand = client.get(url, {"set_by": "hand"})
    assert set(ids(by_hand)) == {hand.pk}

    by_rule = client.get(url, {"set_by": "rule"})
    assert ids(by_rule) == [ruled.pk]

    by_ai = client.get(url, {"set_by": "suggestion"})
    assert ids(by_ai) == [suggested.pk]

    combined = client.get(
        url,
        {
            "amount_min": "-30.00",
            "amount_max": "-20.00",
            "amount_mode": "signed",
            "set_by": "hand",
            "q": "Hand",
        },
    )
    assert ids(combined) == [hand.pk]
    assert spend_small.pk not in ids(signed)


@pytest.mark.django_db
def test_deep_link_reproduces_the_same_result_set():
    owner = make_person("owner")
    account = make_account(owner)
    matching = make_transaction(owner, account, description="Synthetic bakery", note="birthday cake", amount_minor=-1800)
    make_transaction(owner, account, description="Synthetic bakery", amount_minor=-400)
    client = Client()
    client.force_login(owner.user)
    query = {
        "q": "bakery",
        "has_note": "1",
        "amount_min": "-20.00",
        "amount_max": "-10.00",
        "amount_mode": "signed",
    }

    first = client.get(reverse("transaction-list"), query)
    second = client.get(reverse("transaction-list"), query)

    assert ids(first) == [matching.pk]
    assert ids(second) == ids(first)
    parsed = parse_qs(urlparse(first.context["list_query"]).query)
    assert parsed["q"] == ["bakery"]
    assert parsed["has_note"] == ["1"]
    assert parsed["amount_min"] == ["-20.00"]


@pytest.mark.django_db
def test_saved_filters_are_private_and_drop_unseen_accounts():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(
        owner,
        name="Shared checking",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private = make_account(owner, name="Owner private")
    make_transaction(owner, shared, description="Shared row")
    private_txn = make_transaction(owner, private, description="Private row")
    client = Client()
    client.force_login(member.user)
    save = client.post(
        reverse("transaction-saved-filter-create"),
        {"name": "Shared hunt", "account": str(shared.pk), "q": "Shared"},
    )
    saved = SavedTransactionFilter.objects.visible_to(member).get()
    assert save.status_code == 302
    assert SavedTransactionFilter.objects.visible_to(owner).count() == 0

    owner_client = Client()
    owner_client.force_login(owner.user)
    hidden = owner_client.get(reverse("transaction-list"))
    assert saved.name not in hidden.content.decode()
    assert owner_client.get(reverse("transaction-saved-filter-apply", args=(saved.pk,))).status_code == 404
    assert owner_client.post(reverse("transaction-saved-filter-delete", args=(saved.pk,))).status_code == 404

    applied = client.get(reverse("transaction-saved-filter-apply", args=(saved.pk,)))
    assert applied.status_code == 302
    follow = client.get(applied.url)
    assert "Shared row" in follow.content.decode()
    assert private_txn.pk not in ids(follow)

    unshare_account(owner, shared.pk)
    after_unshare = client.get(reverse("transaction-saved-filter-apply", args=(saved.pk,)))
    follow_dropped = client.get(after_unshare.url)
    query = parse_qs(urlparse(after_unshare.url).query)
    assert "account" not in query
    assert private_txn.pk not in ids(follow_dropped)
    assert "Shared row" not in follow_dropped.content.decode()


@pytest.mark.django_db
def test_pagination_keeps_filter_query():
    owner = make_person("owner")
    account = make_account(owner)
    for index in range(PAGE_SIZE + 1):
        make_transaction(
            owner,
            account,
            description=f"Synthetic page {index}",
            amount_minor=-100 - index,
            transaction_date=date(2026, 1, 2),
        )
    client = Client()
    client.force_login(owner.user)

    page_one = client.get(reverse("transaction-list"), {"q": "Synthetic page"})
    page_two = client.get(reverse("transaction-list"), {"q": "Synthetic page", "page": "2"})

    assert page_one.context["page"].paginator.num_pages == 2
    assert len(ids(page_one)) == PAGE_SIZE
    assert len(ids(page_two)) == 1
    assert set(ids(page_one)).isdisjoint(ids(page_two))
    assert "page=2" in page_one.content.decode()
    assert "q=Synthetic+page" in page_one.content.decode() or "q=Synthetic%20page" in page_one.content.decode()


@pytest.mark.django_db
def test_a_category_changed_by_hand_after_accepting_a_suggestion_is_set_by_hand():
    from django.utils import timezone

    from finance.category_services import assign_category

    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    txn = make_transaction(owner, account, description="Synthetic bistro", amount_minor=-2500)
    assign_category(owner, txn.pk, dining.pk)
    CategorySuggestion.objects.create(
        member=owner,
        transaction=txn,
        category=dining,
        provider="agent_harness",
        backend="local",
        status=CategorySuggestion.Status.ACCEPTED,
        resolved_at=timezone.now(),
        snapshot_hash="b" * 64,
    )
    assign_category(owner, txn.pk, groceries.pk)
    assign_category(owner, txn.pk, dining.pk)
    client = Client()
    client.force_login(owner.user)
    url = reverse("transaction-list")

    assert ids(client.get(url, {"set_by": "suggestion"})) == []
    assert ids(client.get(url, {"set_by": "hand"})) == [txn.pk]
