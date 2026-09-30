from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.category_services import ensure_household_categories, refresh_transfer_pairs
from finance.csv_import.services import undo_import_batch
from finance.lifecycle_services import archive_account
from finance.models import Account, Household, ImportBatch, Membership, Person, RecurringSeries, Transaction
from finance.recurring_services import (
    confirm_recurring_series,
    confirmed_totals,
    detect_recurring_series,
    dismiss_recurring_series,
    refresh_recurring_series,
)


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


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 2),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
    kind=Transaction.Kind.CASH_FLOW,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 12, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=kind,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def add_monthly_charges(owner, account, *, description="Synthetic Stream", amount_minor=-1599, count=3, start=date(2026, 1, 15)):
    rows = []
    for index in range(count):
        month = start.month + index
        year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        rows.append(
            make_transaction(
                owner,
                account,
                transaction_date=date(year, month, start.day),
                amount_minor=amount_minor,
                description=description,
            )
        )
    return rows


@pytest.mark.django_db
def test_three_monthly_charges_are_suggested_with_reasons():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account)

    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()

    assert series.status == RecurringSeries.Status.SUGGESTED
    assert series.cadence == RecurringSeries.Cadence.MONTHLY
    assert series.confidence == RecurringSeries.Confidence.HIGH
    assert series.members.count() == 3
    assert any("monthly" in reason for reason in series.reasons)
    assert any("exactly" in reason for reason in series.reasons)


@pytest.mark.django_db
def test_two_occurrences_are_only_possible():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, count=2)

    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()

    assert series.status == RecurringSeries.Status.POSSIBLE
    assert series.confidence == RecurringSeries.Confidence.LOW
    assert "possible" in series.get_status_display().lower() or series.status == "possible"


@pytest.mark.django_db
def test_amount_tolerance_uses_selected_cadence_chain_median_not_cluster():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, transaction_date=date(2026, 1, 10), amount_minor=-800, description="Synthetic Cluster Mix")
    make_transaction(owner, account, transaction_date=date(2026, 2, 10), amount_minor=-800, description="Synthetic Cluster Mix")
    make_transaction(owner, account, transaction_date=date(2026, 3, 10), amount_minor=-1200, description="Synthetic Cluster Mix")
    make_transaction(owner, account, transaction_date=date(2026, 1, 16), amount_minor=-1000, description="Synthetic Cluster Mix")
    make_transaction(owner, account, transaction_date=date(2026, 2, 22), amount_minor=-1000, description="Synthetic Cluster Mix")

    refresh_recurring_series(owner)
    series_rows = list(RecurringSeries.objects.filter(merchant_key="synthetic cluster mix"))

    assert series_rows == []
    detected = detect_recurring_series(list(Transaction.objects.filter(account=account)))
    assert detected == []


@pytest.mark.django_db
def test_unrelated_same_merchant_charges_do_not_split_a_monthly_series():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    description = "Synthetic Cluster Mix"
    make_transaction(owner, account, transaction_date=date(2026, 1, 1), amount_minor=-800, description=description)
    make_transaction(owner, account, transaction_date=date(2026, 1, 2), amount_minor=-800, description=description)
    make_transaction(owner, account, transaction_date=date(2026, 1, 3), amount_minor=-800, description=description)
    make_transaction(owner, account, transaction_date=date(2026, 1, 15), amount_minor=-1000, description=description)
    make_transaction(owner, account, transaction_date=date(2026, 2, 15), amount_minor=-1200, description=description)
    make_transaction(owner, account, transaction_date=date(2026, 3, 15), amount_minor=-1200, description=description)

    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get(merchant_key="synthetic cluster mix")

    assert series.status == RecurringSeries.Status.SUGGESTED
    assert series.cadence == RecurringSeries.Cadence.MONTHLY
    assert series.typical_amount_minor == -1200
    assert series.members.count() == 3
    assert set(series.members.values_list("transaction__transaction_date", flat=True)) == {
        date(2026, 1, 15),
        date(2026, 2, 15),
        date(2026, 3, 15),
    }


@pytest.mark.django_db
def test_varying_amounts_within_25_percent_are_medium_confidence():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, transaction_date=date(2026, 1, 10), amount_minor=-1000, description="Synthetic Cloud")
    make_transaction(owner, account, transaction_date=date(2026, 2, 10), amount_minor=-1100, description="Synthetic Cloud")
    make_transaction(owner, account, transaction_date=date(2026, 3, 10), amount_minor=-1200, description="Synthetic Cloud")

    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()

    assert series.status == RecurringSeries.Status.SUGGESTED
    assert series.confidence in {RecurringSeries.Confidence.MEDIUM, RecurringSeries.Confidence.LOW}
    assert series.confidence != RecurringSeries.Confidence.HIGH
    assert any("25%" in reason for reason in series.reasons)


@pytest.mark.django_db
def test_weekly_biweekly_quarterly_and_annual_cadences():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    start = date(2026, 1, 7)
    for index in range(3):
        make_transaction(
            owner,
            account,
            transaction_date=start + timedelta(days=7 * index),
            amount_minor=-499,
            description="Synthetic Weekly Box",
        )
    bi_start = date(2026, 1, 2)
    for index in range(3):
        make_transaction(
            owner,
            account,
            transaction_date=bi_start + timedelta(days=14 * index),
            amount_minor=-799,
            description="Synthetic Payday App",
        )
    make_transaction(owner, account, transaction_date=date(2026, 1, 5), amount_minor=-4500, description="Synthetic Insurance")
    make_transaction(owner, account, transaction_date=date(2026, 4, 5), amount_minor=-4500, description="Synthetic Insurance")
    make_transaction(owner, account, transaction_date=date(2026, 7, 5), amount_minor=-4500, description="Synthetic Insurance")
    make_transaction(owner, account, transaction_date=date(2024, 3, 1), amount_minor=-12000, description="Synthetic Domain")
    make_transaction(owner, account, transaction_date=date(2025, 3, 1), amount_minor=-12000, description="Synthetic Domain")
    make_transaction(owner, account, transaction_date=date(2026, 3, 1), amount_minor=-12000, description="Synthetic Domain")

    refresh_recurring_series(owner)
    cadences = set(RecurringSeries.objects.values_list("cadence", flat=True))

    assert cadences == {
        RecurringSeries.Cadence.WEEKLY,
        RecurringSeries.Cadence.BIWEEKLY,
        RecurringSeries.Cadence.QUARTERLY,
        RecurringSeries.Cadence.ANNUAL,
    }


@pytest.mark.django_db
def test_transfers_and_investment_activity_are_not_recurring():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    for index in range(3):
        day = date(2026, 1, 3) + timedelta(days=30 * index)
        make_transaction(owner, checking, transaction_date=day, amount_minor=-2500, description="Synthetic to savings")
        make_transaction(owner, savings, transaction_date=day, amount_minor=2500, description="Synthetic from checking")
    invest = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    for index in range(3):
        make_transaction(
            owner,
            invest,
            transaction_date=date(2026, 1, 8) + timedelta(days=30 * index),
            amount_minor=-800,
            description="Synthetic contribution",
            kind=Transaction.Kind.INVESTMENT_ACTIVITY,
        )
    refresh_transfer_pairs(owner)
    refresh_recurring_series(owner)

    assert not RecurringSeries.objects.exists()


@pytest.mark.django_db
def test_confirm_shows_exact_minor_unit_totals_on_recurring_page():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account)
    client = Client()
    client.force_login(owner.user)

    page = client.get(reverse("recurring-review"))
    series = RecurringSeries.objects.get()
    assert page.status_code == 200
    assert b"Synthetic Stream" in page.content
    assert b"Monthly" in page.content

    confirm = client.post(reverse("recurring-review"), {"series_id": series.pk, "action": "confirm"})
    assert confirm.status_code == 302
    confirmed = RecurringSeries.objects.get()
    assert confirmed.status == RecurringSeries.Status.CONFIRMED
    assert confirmed.monthly_minor == 1599
    assert confirmed.annual_minor == 19188

    home = client.get(reverse("home"))
    confirmed_page = client.get(reverse("recurring-review"))
    assert b"Recurring" in home.content
    assert b"1,599.00 USD" in confirmed_page.content or b"15.99 USD" in confirmed_page.content
    assert b"19188" in confirmed_page.content
    assert b"1599" in confirmed_page.content


@pytest.mark.django_db
def test_refresh_deactivates_confirmed_series_when_occurrences_are_no_longer_eligible():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    confirm_recurring_series(owner, series.pk)

    archive_account(owner, account.pk)
    refresh_recurring_series(owner)
    series.refresh_from_db()

    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.is_active is False
    assert series.members.count() == 0
    assert confirmed_totals([series]) == (0, 0)

    replacement = make_account(owner, name="Synthetic Replacement")
    add_monthly_charges(owner, replacement)
    refresh_recurring_series(owner)
    series.refresh_from_db()

    assert RecurringSeries.objects.filter(person=owner).count() == 1
    assert series.is_active is True
    assert series.members.count() == 3
    assert confirmed_totals([series])[0] > 0


@pytest.mark.django_db
def test_refresh_deactivates_confirmed_series_when_occurrences_are_transfers():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    add_monthly_charges(owner, checking, description="Synthetic to savings", amount_minor=-2500)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    confirm_recurring_series(owner, series.pk)

    start = date(2026, 1, 15)
    for index in range(3):
        month = start.month + index
        year = start.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        make_transaction(
            owner,
            savings,
            transaction_date=date(year, month, start.day),
            amount_minor=2500,
            description="Synthetic from checking",
        )
    refresh_transfer_pairs(owner)
    refresh_recurring_series(owner)
    series.refresh_from_db()

    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.is_active is False
    assert series.members.count() == 0
    assert confirmed_totals([series]) == (0, 0)


@pytest.mark.django_db
def test_confirmed_series_does_not_absorb_a_second_amount_cluster():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-1000)
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-1300)
    client = Client()
    client.force_login(owner.user)

    listing = client.get(reverse("recurring-review"))
    assert listing.status_code == 200
    cheap = RecurringSeries.objects.get(typical_amount_minor=-1000)
    costly = RecurringSeries.objects.get(typical_amount_minor=-1300)
    assert cheap.pk != costly.pk

    confirm = client.post(reverse("recurring-review"), {"series_id": cheap.pk, "action": "confirm"})
    assert confirm.status_code == 302

    cheap.refresh_from_db()
    costly.refresh_from_db()
    assert cheap.status == RecurringSeries.Status.CONFIRMED
    assert cheap.typical_amount_minor == -1000
    assert cheap.fingerprint != costly.fingerprint
    assert RecurringSeries.objects.filter(person=owner).count() == 2
    assert RecurringSeries.objects.filter(typical_amount_minor=-1300).exclude(status=RecurringSeries.Status.CONFIRMED).count() == 1

    page = client.get(reverse("recurring-review"))
    assert page.status_code == 200


@pytest.mark.django_db
def test_confirmed_series_stays_active_while_any_occurrence_is_eligible():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    rows = add_monthly_charges(owner, account)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    confirm_recurring_series(owner, series.pk)

    undo_import_batch(owner, account.pk, rows[0].import_batch_id)
    undo_import_batch(owner, account.pk, rows[1].import_batch_id)
    refresh_recurring_series(owner)
    series.refresh_from_db()

    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.is_active is True
    assert series.members.count() == 1
    assert series.members.get().transaction_id == rows[2].pk
    assert confirmed_totals([series])[0] > 0


@pytest.mark.django_db
def test_dismissed_series_is_not_resuggested_until_transactions_change():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    dismiss_recurring_series(owner, series.pk)
    refresh_recurring_series(owner)

    assert RecurringSeries.objects.filter(status=RecurringSeries.Status.DISMISSED).count() == 1
    assert not RecurringSeries.objects.filter(status__in=(RecurringSeries.Status.SUGGESTED, RecurringSeries.Status.POSSIBLE)).exists()

    add_monthly_charges(owner, account, count=1, start=date(2026, 4, 15))
    refresh_recurring_series(owner)

    assert RecurringSeries.objects.filter(status=RecurringSeries.Status.SUGGESTED).exists()


@pytest.mark.django_db
def test_private_account_does_not_leak_into_household_member_recurring_view():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    add_monthly_charges(owner, private, description="Synthetic Secret Sub")
    add_monthly_charges(owner, shared, description="Synthetic Shared Sub", amount_minor=-899)
    refresh_recurring_series(owner)

    client = Client()
    client.force_login(member.user)
    page = client.get(reverse("recurring-review"))

    assert page.status_code == 200
    assert b"Synthetic Secret Sub" not in page.content
    assert b"Synthetic Shared Sub" in page.content
    assert RecurringSeries.objects.visible_to(member).count() == 1
    secret_ids = list(
        RecurringSeries.objects.filter(display_name="Synthetic Secret Sub").values_list("pk", flat=True)
    )
    assert not RecurringSeries.objects.visible_to(member).filter(pk__in=secret_ids).exists()
    owner_series = RecurringSeries.objects.visible_to(owner)
    assert owner_series.filter(display_name="Synthetic Secret Sub").exists()


@pytest.mark.django_db
def test_member_cannot_confirm_another_persons_private_series():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner)
    add_monthly_charges(owner, private, description="Synthetic Secret Sub")
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    client = Client()
    client.force_login(member.user)

    response = client.post(reverse("recurring-review"), {"series_id": series.pk, "action": "confirm"})

    assert response.status_code == 404
    series.refresh_from_db()
    assert series.status == RecurringSeries.Status.SUGGESTED


@pytest.mark.django_db
def test_correcting_every_amount_of_a_confirmed_series_keeps_one_series():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    charges = add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-1000)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get(person=owner)
    confirm_recurring_series(owner, series.pk)
    Transaction.objects.filter(pk__in=[charge.pk for charge in charges]).update(amount_minor=-2000)
    client = Client()
    client.force_login(owner.user)

    first = client.get(reverse("recurring-review"))
    second = client.get(reverse("recurring-review"))

    assert first.status_code == 200
    assert second.status_code == 200
    series.refresh_from_db()
    assert RecurringSeries.objects.filter(person=owner).count() == 1
    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.is_active
    assert series.typical_amount_minor == -2000


@pytest.mark.django_db
def test_corrected_confirmed_series_with_a_new_occurrence_is_not_counted_twice():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    charges = add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-1000)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get(person=owner)
    confirm_recurring_series(owner, series.pk)
    Transaction.objects.filter(pk__in=[charge.pk for charge in charges]).update(amount_minor=-2000)
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-2000, count=1, start=date(2026, 4, 15))

    refresh_recurring_series(owner)
    series.refresh_from_db()

    assert RecurringSeries.objects.filter(person=owner).count() == 1
    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.typical_amount_minor == -2000
    assert series.members.count() == 4


@pytest.mark.django_db
def test_merged_confirmed_series_leave_no_overlapping_active_series():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-1000)
    later = add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-3000, start=date(2026, 4, 15))
    refresh_recurring_series(owner)
    for series in RecurringSeries.objects.filter(person=owner):
        confirm_recurring_series(owner, series.pk)
    Transaction.objects.filter(pk__in=[charge.pk for charge in later]).update(amount_minor=-1000)

    refresh_recurring_series(owner)
    active = RecurringSeries.objects.filter(
        person=owner, status=RecurringSeries.Status.CONFIRMED, is_active=True
    )
    monthly, _annual = confirmed_totals(RecurringSeries.objects.filter(person=owner))

    assert active.count() == 1
    assert abs(monthly) == 1000


@pytest.mark.django_db
def test_long_descriptions_are_truncated_to_the_stored_name_length():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Stream " + "x" * 240)

    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get(person=owner)

    assert len(series.display_name) == RecurringSeries._meta.get_field("display_name").max_length
    assert len(series.merchant_key) <= RecurringSeries._meta.get_field("merchant_key").max_length


@pytest.mark.django_db
def test_second_amount_cluster_suggestion_survives_a_new_occurrence():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-1000)
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-3000)
    refresh_recurring_series(owner)
    assert RecurringSeries.objects.filter(person=owner).count() == 2
    add_monthly_charges(owner, account, description="Synthetic Stream", amount_minor=-3000, count=1, start=date(2026, 4, 15))

    refresh_recurring_series(owner)
    amounts = sorted(RecurringSeries.objects.filter(person=owner).values_list("typical_amount_minor", flat=True))

    assert amounts == [-3000, -1000]


@pytest.mark.django_db
def test_confirmed_series_is_renamed_when_named_transactions_become_private():
    from finance.lifecycle_services import unshare_account

    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Member Card")
    make_transaction(owner, shared, transaction_date=date(2026, 1, 15), amount_minor=-1599, description="Synthetic Gym 111111")
    make_transaction(owner, shared, transaction_date=date(2026, 2, 15), amount_minor=-1599, description="Synthetic Gym 111111")
    make_transaction(member, private, transaction_date=date(2026, 3, 15), amount_minor=-1599, description="Synthetic Gym 222222")
    refresh_recurring_series(member)
    series = RecurringSeries.objects.get(person=member)
    assert series.display_name == "Synthetic Gym 111111"
    confirm_recurring_series(member, series.pk)

    unshare_account(owner, shared.pk)
    refresh_recurring_series(member)
    series.refresh_from_db()

    assert series.is_active
    assert series.display_name == "Synthetic Gym 222222"


@pytest.mark.django_db
def test_dismissed_series_does_not_starve_a_confirmed_series():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Stream")
    refresh_recurring_series(owner)
    dismiss_recurring_series(owner, RecurringSeries.objects.get(person=owner).pk)
    fourth = add_monthly_charges(owner, account, description="Synthetic Stream", count=1, start=date(2026, 4, 15))[0]
    refresh_recurring_series(owner)
    confirmed = RecurringSeries.objects.get(person=owner, status=RecurringSeries.Status.SUGGESTED)
    confirm_recurring_series(owner, confirmed.pk)

    undo_import_batch(owner, account.pk, fourth.import_batch_id)
    refresh_recurring_series(owner)
    confirmed.refresh_from_db()

    assert confirmed.status == RecurringSeries.Status.CONFIRMED
    assert confirmed.is_active
    assert confirmed.members.count() == 3


@pytest.mark.django_db
@pytest.mark.parametrize("action", [confirm_recurring_series, dismiss_recurring_series])
def test_suggestion_deleted_while_waiting_for_the_lock_is_denied(monkeypatch, action):
    import finance.recurring_services as recurring_services
    from django.core.exceptions import PermissionDenied

    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Stream")
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get(person=owner)
    real_lock = recurring_services.lock_actor_household

    def lock_after_a_concurrent_refresh(person):
        result = real_lock(person)
        series.members.all().delete()
        RecurringSeries.objects.filter(pk=series.pk).delete()
        return result

    monkeypatch.setattr(recurring_services, "lock_actor_household", lock_after_a_concurrent_refresh)

    with pytest.raises(PermissionDenied):
        action(owner, series.pk)
