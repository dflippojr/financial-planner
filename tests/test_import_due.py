"""Import-due reminders for accounts kept current by CSV (#428)."""

from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import alerts_for, run_daily_alert_pass, save_alert_settings, settings_for
from finance.import_due import due_import_accounts, evaluate_import_due_alerts
from finance.models import Account, AccountLink, Alert, ImportBatch, Transaction
from tests.test_alerts import make_account, make_household, make_person, signed_in
from tests.test_csv_import_page import link_account

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 13)


def add_batch(account, *, ends, source=ImportBatch.Source.APPLE_CARD, status=ImportBatch.Status.ACTIVE):
    return ImportBatch.objects.create(
        account=account,
        imported_by=account.owner,
        source=source,
        source_file_sha256=f"{account.pk:x}{ends:%Y%m%d}{source}".encode().hex().ljust(64, "0")[:64],
        date_range_start=ends - timedelta(days=30),
        date_range_end=ends,
        status=status,
        archived_at=timezone.now() if status == ImportBatch.Status.ARCHIVED else None,
    )


def add_transaction(batch):
    return Transaction.objects.create(
        account=batch.account,
        import_batch=batch,
        transaction_date=batch.date_range_end,
        amount_minor=-1234,
        description="SYNTHETIC MERCHANT",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=f"{batch.pk:x}".ljust(64, "f"),
        original_fields={"Synthetic Amount": "-12.34"},
    )


def due_names(principal, today=TODAY):
    return [account.name for account in due_import_accounts(Account.objects.visible_to(principal), today=today)]


def import_due_alerts():
    return sorted(Alert.objects.filter(kind=Alert.Kind.IMPORT_DUE).values_list("recipient_id", "dedupe_key"))


def test_due_boundary_is_strict_after_thirty_five_days():
    owner = make_person("owner")
    stale = make_account(owner, name="Synthetic Card 36", account_type=Account.Type.CREDIT_CARD)
    fresh = make_account(owner, name="Synthetic Card 35", account_type=Account.Type.CREDIT_CARD)
    add_batch(stale, ends=TODAY - timedelta(days=36))
    add_batch(fresh, ends=TODAY - timedelta(days=35))

    assert due_names(owner) == ["Synthetic Card 36"]


def test_newest_active_non_manual_batch_sets_coverage():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Checking")
    add_batch(account, ends=TODAY - timedelta(days=60))
    add_batch(account, ends=TODAY - timedelta(days=5), source=ImportBatch.Source.MANUAL)
    add_batch(account, ends=TODAY - timedelta(days=4), status=ImportBatch.Status.ARCHIVED)

    [due] = due_import_accounts(Account.objects.visible_to(owner), today=TODAY)
    assert due.last_covered == TODAY - timedelta(days=60)

    add_batch(account, ends=TODAY - timedelta(days=3))
    assert due_names(owner) == []


def test_linked_archived_asset_and_never_imported_accounts_are_never_due():
    owner = make_person("owner")
    linked = make_account(owner, name="Synthetic Linked")
    archived = make_account(owner, name="Synthetic Archived")
    asset = make_account(owner, name="Synthetic House", account_type=Account.Type.REAL_ESTATE)
    loan = make_account(owner, name="Synthetic Loan", account_type=Account.Type.LOAN)
    make_account(owner, name="Synthetic Unimported")
    only_manual = make_account(owner, name="Synthetic Manual Only")
    for account in (linked, archived, asset, loan):
        add_batch(account, ends=TODAY - timedelta(days=90))
    add_batch(only_manual, ends=TODAY - timedelta(days=90), source=ImportBatch.Source.MANUAL)
    link_account(owner, linked)
    Account.objects.filter(pk=archived.pk).update(status=Account.Status.ARCHIVED, archived_at=timezone.now())

    assert due_names(owner) == []


def test_balances_only_link_still_counts_as_due():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Balances Only")
    csv_end = TODAY - timedelta(days=40)
    add_batch(account, ends=csv_end)
    link = link_account(owner, account)
    link.mode = AccountLink.Mode.BALANCES_ONLY
    link.save()
    # Balances-only syncs still record a SimpleFIN batch through the sync date.
    add_batch(account, ends=TODAY, source=ImportBatch.Source.SIMPLEFIN)

    [due] = due_import_accounts(Account.objects.visible_to(owner), today=TODAY)
    assert (due.name, due.last_covered) == ("Synthetic Balances Only", csv_end)


def test_balance_sync_batches_stay_out_of_coverage_after_unlinking():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Formerly Balances Only")
    csv_end = TODAY - timedelta(days=100)
    add_batch(account, ends=csv_end)
    link = link_account(owner, account)
    link.mode = AccountLink.Mode.BALANCES_ONLY
    link.save()
    add_batch(account, ends=TODAY, source=ImportBatch.Source.SIMPLEFIN)
    link.delete()

    [due] = due_import_accounts(Account.objects.visible_to(owner), today=TODAY)
    assert due.last_covered == csv_end


def test_simplefin_batches_with_transactions_count_as_coverage():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Formerly Linked")
    add_transaction(add_batch(account, ends=TODAY - timedelta(days=40), source=ImportBatch.Source.SIMPLEFIN))

    assert due_names(owner) == ["Synthetic Formerly Linked"]
    add_transaction(add_batch(account, ends=TODAY - timedelta(days=1), source=ImportBatch.Source.SIMPLEFIN))
    assert due_names(owner) == []


def test_needs_attention_lists_only_visible_due_accounts():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    today = timezone.localdate()
    shared = make_account(owner, name="Synthetic Shared Card", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Synthetic Owner Private Card")
    add_batch(shared, ends=today - timedelta(days=40))
    add_batch(private, ends=today - timedelta(days=40))

    owner_items = signed_in(owner).get(reverse("home")).context["attention_items"]
    member_page = signed_in(member).get(reverse("home"))
    member_items = member_page.context["attention_items"]

    assert sorted(item.title for item in owner_items) == [
        "Import Synthetic Owner Private Card",
        "Import Synthetic Shared Card",
    ]
    assert [(item.title, item.url) for item in member_items] == [
        ("Import Synthetic Shared Card", reverse("csv-import-preview", args=[shared.pk])),
    ]
    assert member_items[0].detail.startswith("Last covered ")
    assert "Owner Private" not in member_page.content.decode()


def _home_queries(person):
    client = signed_in(person)
    client.get(reverse("home"))
    with CaptureQueriesContext(connection) as queries:
        client.get(reverse("home"))
    return len(queries)


def test_home_query_count_is_flat_in_due_accounts():
    owner = make_person("owner")
    make_household(owner)
    today = timezone.localdate()
    add_batch(make_account(owner, name="Synthetic Card 0"), ends=today - timedelta(days=40))
    one = _home_queries(owner)
    for index in range(1, 10):
        add_batch(make_account(owner, name=f"Synthetic Card {index}"), ends=today - timedelta(days=40))

    assert _home_queries(owner) == one


def test_daily_pass_raises_once_per_coverage_end_to_the_audience():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared Card", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name="Synthetic Owner Private Card")
    ends = TODAY - timedelta(days=36)
    add_batch(shared, ends=ends)
    add_batch(private, ends=ends)

    run_daily_alert_pass(today=TODAY)
    run_daily_alert_pass(today=TODAY + timedelta(days=1))

    assert import_due_alerts() == sorted([
        (owner.pk, f"import-due:{shared.pk}:{ends.isoformat()}"),
        (member.pk, f"import-due:{shared.pk}:{ends.isoformat()}"),
        (owner.pk, f"import-due:{private.pk}:{ends.isoformat()}"),
    ])
    alert = Alert.objects.get(recipient=member, account=shared)
    assert alert.title == f"Import Synthetic Shared Card: last covered {ends:%b} {ends.day}, {ends.year}"
    assert alert.link == reverse("csv-import-preview", args=[shared.pk])
    assert list(alerts_for(member).filter(kind=Alert.Kind.IMPORT_DUE)) == [alert]


def test_new_import_clears_and_a_later_lapse_raises_again():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Card")
    first_end = TODAY - timedelta(days=40)
    add_batch(account, ends=first_end)
    evaluate_import_due_alerts(today=TODAY)

    later_end = TODAY - timedelta(days=2)
    add_batch(account, ends=later_end)
    assert evaluate_import_due_alerts(today=TODAY) == []
    assert due_names(owner) == []

    lapse = later_end + timedelta(days=36)
    created = evaluate_import_due_alerts(today=lapse)

    assert [alert.dedupe_key for alert in created] == [f"import-due:{account.pk}:{later_end.isoformat()}"]
    assert import_due_alerts() == sorted([
        (owner.pk, f"import-due:{account.pk}:{first_end.isoformat()}"),
        (owner.pk, f"import-due:{account.pk}:{later_end.isoformat()}"),
    ])


def test_no_new_alert_once_the_first_could_have_been_purged():
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Long Stale Card")
    ends = TODAY - timedelta(days=36)
    add_batch(account, ends=ends)
    first_due = ends + timedelta(days=36)
    last_kept = first_due + timedelta(days=179)

    assert len(evaluate_import_due_alerts(today=last_kept)) == 1
    Alert.objects.all().delete()  # As the 180-day retention purge would.

    assert evaluate_import_due_alerts(today=last_kept + timedelta(days=1)) == []
    assert due_names(owner, today=last_kept + timedelta(days=1)) == ["Synthetic Long Stale Card"]


def test_members_with_the_toggle_off_get_none():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Shared Card", scope=Account.Scope.HOUSEHOLD, household=household)
    add_batch(shared, ends=TODAY - timedelta(days=50))
    assert settings_for(member).import_due_enabled is True
    save_alert_settings(
        member,
        sync_enabled=True, recurring_price_enabled=True, recurring_missed_enabled=True, budget_enabled=True,
        large_transaction_enabled=True, monthly_review_enabled=True, monthly_review_ai_enabled=True,
        large_transaction_minor=None, import_due_enabled=False,
    )

    evaluate_import_due_alerts(today=TODAY)

    assert [recipient for recipient, _key in import_due_alerts()] == [owner.pk]


def test_settings_page_saves_the_import_due_toggle():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    page = client.get(reverse("settings-alerts"))
    assert "Accounts due for a CSV import" in page.content.decode()

    client.post(reverse("settings-alerts"), {"sync_enabled": "on"})

    assert settings_for(owner).import_due_enabled is False
