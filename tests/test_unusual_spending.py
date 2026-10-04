from datetime import date
from hashlib import sha256

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.alert_services import save_alert_settings, settings_for
from finance.cash_flow import spending_by_category_report
from finance.category_services import assign_category, ensure_household_categories
from finance.models import (
    Account,
    Alert,
    AlertSettings,
    Household,
    ImportBatch,
    Membership,
    Person,
    Transaction,
    TransferPair,
)
from finance.monthly_review import compute_monthly_review_facts, store_monthly_review
from finance.unusual_spending import (
    KIND_CATEGORY,
    KIND_MERCHANT,
    KIND_NEW_MERCHANT,
    compute_unusual_flags,
    exceeds_category_baseline,
    exceeds_merchant_median,
)


PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 10, 3)
SEP = date(2026, 9, 1)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(
    owner,
    *,
    name="Synthetic Checking",
    account_type=Account.Type.CHECKING,
    scope=Account.Scope.PRIVATE,
    household=None,
):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date,
    amount_minor,
    description,
    kind=Transaction.Kind.CASH_FLOW,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=sha256(
            f"{account.pk}-{transaction_date}-{amount_minor}-{description}".encode()
        ).hexdigest(),
        date_range_start=date(transaction_date.year, transaction_date.month, 1),
        date_range_end=date(transaction_date.year, transaction_date.month, 28),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=kind,
        source_row_number=2,
        fingerprint=sha256(
            f"txn-{account.pk}-{amount_minor}-{transaction_date}-{description}".encode()
        ).hexdigest(),
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def _baseline_months():
    return (
        date(2026, 3, 10),
        date(2026, 4, 10),
        date(2026, 5, 10),
        date(2026, 6, 10),
        date(2026, 7, 10),
        date(2026, 8, 10),
    )


def _seed_category_history(owner, account, category, monthly_minor, *, prefix="Synthetic grocer"):
    for when in _baseline_months():
        row = make_transaction(
            owner,
            account,
            transaction_date=when,
            amount_minor=-monthly_minor,
            description=f"{prefix} {when.isoformat()}",
        )
        assign_category(owner, row.pk, category.pk)


@pytest.mark.django_db
def test_category_threshold_boundaries():
    assert exceeds_category_baseline(15_000, 10_000, 50, 5_000)
    assert not exceeds_category_baseline(14_999, 10_000, 50, 5_000)
    assert exceeds_category_baseline(9_000, 4_000, 50, 5_000)
    assert not exceeds_category_baseline(8_999, 4_000, 50, 5_000)
    assert exceeds_category_baseline(30_000, 20_000, 50, 5_000)
    assert not exceeds_category_baseline(29_999, 20_000, 50, 5_000)
    assert exceeds_category_baseline(5_000, 0, 50, 5_000)
    assert not exceeds_category_baseline(4_999, 0, 50, 5_000)


@pytest.mark.django_db
def test_merchant_threshold_boundaries():
    assert not exceeds_merchant_median(2_000, 1_000, prior_count=3)
    assert exceeds_merchant_median(2_001, 1_000, prior_count=3)
    assert not exceeds_merchant_median(50_000, 1_000, prior_count=2)
    assert not exceeds_merchant_median(2_001, 1_000, prior_count=0)


@pytest.mark.django_db
def test_category_flag_matches_spending_page_and_respects_filters():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Savings")
    groceries = household.categories.get(name="Groceries")
    _seed_category_history(owner, checking, groceries, 10_000)
    spike = make_transaction(
        owner,
        checking,
        transaction_date=date(2026, 9, 12),
        amount_minor=-15_000,
        description="Synthetic grocer spike",
    )
    assign_category(owner, spike.pk, groceries.pk)
    other = make_transaction(
        owner,
        savings,
        transaction_date=date(2026, 9, 12),
        amount_minor=-80_000,
        description="Synthetic other spike",
    )
    assign_category(owner, other.pk, groceries.pk)

    flags = compute_unusual_flags(owner, SEP, account=checking)
    report = spending_by_category_report(
        owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), account=checking
    )
    grocery_row = next(row for row in report.rows if row.name == "Groceries")
    category_flags = [item for item in flags if item["kind"] == KIND_CATEGORY]
    assert [item["name"] for item in category_flags] == ["Groceries"]
    assert category_flags[0]["month_minor"] == grocery_row.spending_minor == 15_000
    assert category_flags[0]["baseline_minor"] == "10000"

    mixed = compute_unusual_flags(owner, SEP)
    mixed_grocery = next(item for item in mixed if item["kind"] == KIND_CATEGORY and item["name"] == "Groceries")
    full = spending_by_category_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    full_row = next(row for row in full.rows if row.name == "Groceries")
    assert mixed_grocery["month_minor"] == full_row.spending_minor == 95_000


@pytest.mark.django_db
def test_merchant_and_new_merchant_flags():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    prefs = settings_for(owner)
    prefs.large_transaction_minor = 5_000
    prefs.save()
    for when, amount in (
        (date(2026, 6, 2), -1_000),
        (date(2026, 7, 2), -1_000),
        (date(2026, 8, 2), -1_000),
    ):
        make_transaction(owner, checking, transaction_date=when, amount_minor=amount, description="SYNTHETIC-CAFE")
    exact = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 3), amount_minor=-2_000, description="synthetic cafe"
    )
    over = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 4), amount_minor=-2_001, description="Synthetic-Cafe!!"
    )
    first_small = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 5), amount_minor=-4_999, description="Brand new shop"
    )
    first_large = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 6), amount_minor=-5_000, description="Other new shop"
    )

    flags = compute_unusual_flags(owner, SEP)
    merchants = [item for item in flags if item["kind"] == KIND_MERCHANT]
    new_merchants = [item for item in flags if item["kind"] == KIND_NEW_MERCHANT]
    assert [item["transaction_id"] for item in merchants] == [over.pk]
    assert exact.pk not in [item["transaction_id"] for item in merchants]
    assert [item["transaction_id"] for item in new_merchants] == [first_large.pk]
    assert first_small.pk not in [item["transaction_id"] for item in new_merchants]


@pytest.mark.django_db
def test_excludes_transfers_investment_and_other_members_private():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    checking = make_account(owner)
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    brokerage = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    secret = make_account(member, name="Member secret")
    for person in (owner, member):
        prefs = settings_for(person)
        prefs.large_transaction_minor = 1_000
        prefs.save()
    out_leg = make_transaction(
        owner, checking, transaction_date=date(2026, 9, 8), amount_minor=-9_000, description="Card payment"
    )
    in_leg = make_transaction(
        owner, card, transaction_date=date(2026, 9, 8), amount_minor=9_000, description="Card payment in"
    )
    first, second = (out_leg, in_leg) if out_leg.pk < in_leg.pk else (in_leg, out_leg)
    TransferPair.objects.create(
        leg_a=first,
        leg_b=second,
        status=TransferPair.Status.CONFIRMED,
        kind=TransferPair.Kind.CARD_PAYMENT,
        confidence=TransferPair.Confidence.HIGH,
        reasons=["synthetic pair"],
    )
    make_transaction(
        owner,
        brokerage,
        transaction_date=date(2026, 9, 9),
        amount_minor=-20_000,
        description="Broker buy",
        kind=Transaction.Kind.INVESTMENT_ACTIVITY,
    )
    make_transaction(
        member, secret, transaction_date=date(2026, 9, 9), amount_minor=-12_000, description="Secret shop"
    )

    owner_flags = compute_unusual_flags(owner, SEP)
    member_flags = compute_unusual_flags(member, SEP)
    assert owner_flags == []
    assert [item["name"] for item in member_flags if item["kind"] == KIND_NEW_MERCHANT] == ["Secret shop"]
    blob = str(owner_flags)
    assert "Secret shop" not in blob
    assert "12,000" not in blob


@pytest.mark.django_db
def test_monthly_review_section_alerts_and_settings():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    _seed_category_history(owner, checking, groceries, 10_000)
    spike = make_transaction(
        owner,
        checking,
        transaction_date=date(2026, 9, 12),
        amount_minor=-15_000,
        description="Synthetic grocer spike",
    )
    assign_category(owner, spike.pk, groceries.pk)

    facts = compute_monthly_review_facts(owner, SEP, today=TODAY)
    assert facts["unusual"]
    store_monthly_review(owner, SEP, today=TODAY)
    alerts = list(Alert.objects.filter(recipient=owner, kind=Alert.Kind.UNUSUAL_SPENDING))
    assert len(alerts) == 1
    store_monthly_review(owner, SEP, today=TODAY, force=True)
    assert Alert.objects.filter(recipient=owner, kind=Alert.Kind.UNUSUAL_SPENDING).count() == 1

    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    body = page.content.decode()
    assert "Unusual this month" in body
    assert "Groceries" in body

    save_alert_settings(
        owner,
        sync_enabled=True,
        recurring_price_enabled=True,
        recurring_missed_enabled=True,
        budget_enabled=True,
        large_transaction_enabled=True,
        monthly_review_enabled=True,
        monthly_review_ai_enabled=True,
        large_transaction_minor=None,
        unusual_spending_enabled=False,
    )
    Alert.objects.filter(recipient=owner, kind=Alert.Kind.UNUSUAL_SPENDING).delete()
    store_monthly_review(owner, SEP, today=TODAY, force=True)
    assert not Alert.objects.filter(recipient=owner, kind=Alert.Kind.UNUSUAL_SPENDING).exists()
    review_page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    assert "Unusual this month" in review_page.content.decode()
    assert "Groceries" in review_page.content.decode()

    settings_page = signed_in(owner).get(reverse("settings-alerts"))
    assert b"Unusual spending" in settings_page.content
    response = signed_in(owner).post(
        reverse("settings-alerts"),
        {
            "unusual_spending_enabled": "on",
            "unusual_category_percent": "80",
            "unusual_category_amount": "75.00",
        },
    )
    assert response.status_code == 302
    prefs = AlertSettings.objects.get(person=owner)
    assert prefs.unusual_spending_enabled is True
    assert prefs.unusual_category_percent == 80
    assert prefs.unusual_category_floor_minor == 7_500
    flags = compute_unusual_flags(owner, SEP)
    assert flags == []
