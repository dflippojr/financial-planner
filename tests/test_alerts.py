from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import (
    alerts_for,
    evaluate_budget_alert,
    mark_all_alerts_read,
    mark_alert_read,
    purge_old_read_alerts,
    raise_alert,
    raise_large_transaction_alerts,
    raise_sync_alert,
    unread_alert_count,
)
from finance.budget_services import save_budget, progress_snapshot
from finance.category_services import assign_category, refresh_transfer_pairs
from finance.lifecycle_services import delete_account, leave_household
from finance.models import (
    Account,
    Alert,
    AlertSettings,
    Budget,
    Household,
    ImportBatch,
    Membership,
    Person,
    SimpleFinConnection,
    Transaction,
    TransferPair,
)
from finance.simplefin_errors import SimpleFinError
from finance.simplefin_services import sync_connection
from tests.test_simplefin import connect_owner


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    from finance.category_services import ensure_household_categories

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


def make_transaction(owner, account, *, transaction_date=date(2026, 10, 1), amount_minor=-50_000, description="Synthetic row"):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(transaction_date.year, transaction_date.month, 1),
        date_range_end=date(transaction_date.year, transaction_date.month, 28),
    )
    digest = f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64]
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def add_budget(owner, *, category=None, amount_minor=10_000, month=date(2026, 10, 1), scope=Budget.Scope.PRIVATE, household=None):
    return save_budget(
        owner.user,
        {
            "scope": scope,
            "category": category,
            "amount_minor": amount_minor,
            "effective_month": month,
            "rollover_enabled": False,
        },
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_raise_alert_dedupes_and_skips_disabled_kind():
    owner = make_person("owner")
    make_household(owner)
    first = raise_alert(
        [owner],
        Alert.Kind.SYNC,
        "A SimpleFIN sync failed",
        reverse("simplefin-connections"),
        "sync:1:2026-10-03",
    )
    second = raise_alert(
        [owner],
        Alert.Kind.SYNC,
        "A SimpleFIN sync failed",
        reverse("simplefin-connections"),
        "sync:1:2026-10-03",
    )
    assert len(first) == 1
    assert second == []
    assert Alert.objects.filter(recipient=owner).count() == 1

    prefs = AlertSettings.objects.get(person=owner)
    prefs.sync_enabled = False
    prefs.save()
    third = raise_alert(
        [owner],
        Alert.Kind.SYNC,
        "A SimpleFIN sync failed",
        reverse("simplefin-connections"),
        "sync:2:2026-10-03",
    )
    assert third == []


@pytest.mark.django_db
def test_raise_alert_rejects_external_link():
    owner = make_person("owner")
    with pytest.raises(ValidationError):
        raise_alert([owner], Alert.Kind.SYNC, "Nope", "https://example.test/x", "sync:x")


@pytest.mark.django_db
def test_private_budget_alert_goes_only_to_owner():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    txn = make_transaction(owner, checking, amount_minor=-9000)
    assign_category(owner, txn.pk, groceries.pk)
    budget = add_budget(owner, category=groceries, amount_minor=10_000)
    evaluate_budget_alert(budget, today=date(2026, 10, 15))

    assert Alert.objects.filter(recipient=owner, kind=Alert.Kind.BUDGET).count() == 1
    assert not Alert.objects.filter(recipient=member).exists()
    key = Alert.objects.get(recipient=owner).dedupe_key
    assert key == f"budget:{budget.pk}:2026-10:90"


@pytest.mark.django_db
def test_household_budget_alert_goes_to_current_members():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household, name="Shared")
    groceries = household.categories.get(name="Groceries")
    txn = make_transaction(owner, shared, amount_minor=-10_000)
    assign_category(owner, txn.pk, groceries.pk)
    budget = add_budget(
        owner,
        category=groceries,
        amount_minor=10_000,
        scope=Budget.Scope.HOUSEHOLD,
        household=household,
    )
    evaluate_budget_alert(budget, today=date(2026, 10, 15))

    keys = set(Alert.objects.filter(kind=Alert.Kind.BUDGET).values_list("recipient_id", "dedupe_key"))
    assert (owner.pk, f"budget:{budget.pk}:2026-10:90") in keys
    assert (member.pk, f"budget:{budget.pk}:2026-10:90") in keys
    assert (owner.pk, f"budget:{budget.pk}:2026-10:100") in keys
    assert (member.pk, f"budget:{budget.pk}:2026-10:100") in keys


@pytest.mark.django_db
def test_household_budget_alert_uses_each_member_view():
    card_owner = make_person("cardowner")
    roommate = make_person("roommate")
    household = make_household(card_owner, roommate)
    checking = make_account(
        card_owner,
        name="Joint Checking",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private_card = make_account(
        card_owner,
        name="Private Card",
        account_type=Account.Type.CREDIT_CARD,
    )
    make_transaction(
        card_owner,
        checking,
        amount_minor=-9000,
        description="Synthetic card payment out",
    )
    make_transaction(
        card_owner,
        private_card,
        amount_minor=9000,
        description="Synthetic card payment in",
    )
    refresh_transfer_pairs(card_owner)
    pair = TransferPair.objects.get()
    assert pair.status in (TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED)
    budget = add_budget(
        card_owner,
        category=None,
        amount_minor=10_000,
        scope=Budget.Scope.HOUSEHOLD,
        household=household,
    )
    month = date(2026, 10, 1)
    owner_card = progress_snapshot(budget, month, card_owner)
    roommate_card = progress_snapshot(budget, month, roommate)
    assert owner_card.spent_minor * 10 < owner_card.available_minor * 9
    assert roommate_card.spent_minor * 10 >= roommate_card.available_minor * 9
    evaluate_budget_alert(budget, today=date(2026, 10, 15))

    recipients = set(Alert.objects.filter(kind=Alert.Kind.BUDGET).values_list("recipient_id", flat=True))
    assert recipients == {roommate.pk}
    assert Alert.objects.filter(recipient=roommate, kind=Alert.Kind.BUDGET).count() == 1
    assert Alert.objects.get(recipient=roommate).dedupe_key == f"budget:{budget.pk}:2026-10:90"


@pytest.mark.django_db
def test_former_member_does_not_see_household_alerts():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household, name="Shared")
    txn = make_transaction(owner, shared, amount_minor=-80_000)
    AlertSettings.objects.create(person=owner, large_transaction_minor=50_000)
    AlertSettings.objects.create(person=member, large_transaction_minor=50_000)
    raise_large_transaction_alerts([txn])
    assert alerts_for(member).count() == 1

    leave_household(member)
    assert alerts_for(member).count() == 0
    assert alerts_for(owner).count() == 1


@pytest.mark.django_db
def test_large_transaction_threshold_and_kind_switch():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    small = make_transaction(owner, checking, amount_minor=-4000, description="small")
    large = make_transaction(owner, checking, amount_minor=-9000, description="large")
    AlertSettings.objects.create(person=owner, large_transaction_minor=5000)
    raise_large_transaction_alerts([small, large])
    assert Alert.objects.filter(recipient=owner, kind=Alert.Kind.LARGE_TRANSACTION).count() == 1
    assert Alert.objects.get().dedupe_key == f"large:{large.pk}"

    prefs = AlertSettings.objects.get(person=owner)
    prefs.large_transaction_enabled = False
    prefs.save()
    bigger = make_transaction(owner, checking, amount_minor=-12_000, description="bigger")
    raise_large_transaction_alerts([bigger])
    assert Alert.objects.filter(kind=Alert.Kind.LARGE_TRANSACTION).count() == 1


@pytest.mark.django_db
def test_private_large_transaction_does_not_alert_housemate():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    checking = make_account(owner)
    txn = make_transaction(owner, checking, amount_minor=-80_000)
    AlertSettings.objects.create(person=owner, large_transaction_minor=1000)
    AlertSettings.objects.create(person=member, large_transaction_minor=1000)
    raise_large_transaction_alerts([txn])
    assert Alert.objects.filter(recipient=owner).count() == 1
    assert not Alert.objects.filter(recipient=member).exists()


@pytest.mark.django_db
def test_sync_failure_alerts_connection_owner(monkeypatch):
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    connection = connect_owner(owner, monkeypatch)

    def unreachable(*args, **kwargs):
        raise SimpleFinError("SimpleFIN could not be reached.")

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", unreachable)
    with pytest.raises(SimpleFinError):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)
    assert Alert.objects.filter(recipient=owner, kind=Alert.Kind.SYNC).count() == 1
    assert not Alert.objects.filter(recipient=member).exists()
    title = Alert.objects.get().title
    assert "failed" in title.lower()


@pytest.mark.django_db
def test_disabled_connection_uses_relink_title():
    owner = make_person("owner")
    make_household(owner)
    connection = SimpleFinConnection.objects.create(
        owner=owner,
        encrypted_access_url=b"synthetic",
        disabled=True,
        last_sync_result="SimpleFIN access was denied.",
    )
    raise_sync_alert(connection)
    assert "re-linking" in Alert.objects.get().title


@pytest.mark.django_db
def test_unread_count_and_mark_read():
    owner = make_person("owner")
    make_household(owner)
    raise_alert([owner], Alert.Kind.SYNC, "A SimpleFIN sync failed", reverse("simplefin-connections"), "sync:1:a")
    raise_alert([owner], Alert.Kind.SYNC, "A SimpleFIN sync failed", reverse("simplefin-connections"), "sync:1:b")
    assert unread_alert_count(owner) == 2
    first = Alert.objects.order_by("pk").first()
    mark_alert_read(owner, first.pk)
    assert unread_alert_count(owner) == 1
    mark_all_alerts_read(owner)
    assert unread_alert_count(owner) == 0


@pytest.mark.django_db
def test_alerts_page_and_nav_unread():
    owner = make_person("owner")
    make_household(owner)
    raise_alert([owner], Alert.Kind.SYNC, "A SimpleFIN sync failed", reverse("simplefin-connections"), "sync:nav:1")
    client = signed_in(owner)
    home = client.get(reverse("home"))
    alerts_item = next(item for item in home.context["nav_items"] if item["label"] == "Alerts")
    assert alerts_item["unread"] == 1
    page = client.get(reverse("alert-list"))
    assert page.status_code == 200
    assert b"A SimpleFIN sync failed" in page.content
    alert = Alert.objects.get()
    marked = client.post(reverse("alert-mark-read", args=[alert.pk]))
    assert marked.status_code == 302
    assert unread_alert_count(owner) == 0


@pytest.mark.django_db
def test_mark_all_read_from_page():
    owner = make_person("owner")
    make_household(owner)
    raise_alert([owner], Alert.Kind.SYNC, "A SimpleFIN sync failed", reverse("simplefin-connections"), "sync:all:1")
    raise_alert([owner], Alert.Kind.SYNC, "A SimpleFIN sync failed", reverse("simplefin-connections"), "sync:all:2")
    client = signed_in(owner)
    response = client.post(reverse("alert-mark-all-read"))
    assert response.status_code == 302
    assert unread_alert_count(owner) == 0


@pytest.mark.django_db
def test_alert_settings_form_saves_threshold():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    response = client.post(
        reverse("account-settings"),
        {
            "action": "save-alert-settings",
            "sync_enabled": "on",
            "budget_enabled": "on",
            "large_transaction_enabled": "on",
            "large_transaction_amount": "25.00",
        },
    )
    assert response.status_code == 302
    prefs = AlertSettings.objects.get(person=owner)
    assert prefs.large_transaction_minor == 2500
    assert prefs.sync_enabled is True
    assert prefs.recurring_price_enabled is False
    assert prefs.monthly_review_enabled is False


@pytest.mark.django_db
def test_daily_pass_purges_old_read_alerts():
    owner = make_person("owner")
    make_household(owner)
    alert = raise_alert(
        [owner],
        Alert.Kind.SYNC,
        "A SimpleFIN sync failed",
        reverse("simplefin-connections"),
        "sync:old:1",
    )[0]
    Alert.objects.filter(pk=alert.pk).update(
        read_at=timezone.now() - timedelta(days=10),
        created_at=timezone.now() - timedelta(days=181),
    )
    purge_old_read_alerts()
    assert not Alert.objects.filter(pk=alert.pk).exists()


@pytest.mark.django_db
def test_daily_pass_rechecks_sync_from_scheduler_command():
    from django.core.management import call_command

    owner = make_person("owner")
    make_household(owner)
    SimpleFinConnection.objects.create(
        owner=owner,
        encrypted_access_url=b"synthetic",
        disabled=True,
        last_sync_result="SimpleFIN access was denied.",
    )
    call_command("sync_simplefin")
    assert Alert.objects.filter(recipient=owner, kind=Alert.Kind.SYNC).count() == 1


@pytest.mark.django_db
def test_account_with_large_transaction_alert_can_be_deleted():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    txn = make_transaction(owner, checking, amount_minor=-80_000)
    AlertSettings.objects.create(person=owner, large_transaction_minor=50_000)
    raise_large_transaction_alerts([txn])
    assert Alert.objects.filter(recipient=owner, account=checking).count() == 1
    account_id = checking.pk

    delete_account(owner, account_id)

    assert not Account.objects.filter(pk=account_id).exists()
    assert not Alert.objects.filter(recipient=owner, kind=Alert.Kind.LARGE_TRANSACTION).exists()
    assert alerts_for(owner).count() == 0
