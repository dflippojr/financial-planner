"""Large-transaction alerts read only rows that could cross a threshold (#429)."""

from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance import alert_services
from finance.alert_services import (
    audience_for_account,
    evaluate_recent_large_transactions,
    schedule_after_new_transactions,
)
from finance.cash_flow import format_minor
from finance.input_limits import MAX_LARGE_ALERTS
from finance.models import Account, Alert, AlertSettings, Transaction
from tests.test_alerts import make_account, make_household, make_person, make_transaction


def reference_large_transaction_alerts(transactions):
    """The implementation before #429, kept to prove the prefilter changes no alert."""
    transactions = [txn for txn in transactions if txn.status == Transaction.Status.ACTIVE]
    accounts = {txn.account_id: txn.account for txn in transactions}
    people = {}
    audience = {}
    for account in accounts.values():
        audience[account.pk] = audience_for_account(account)
        people.update({person.pk: person for person in audience[account.pk]})
    preferences = {prefs.person_id: prefs for prefs in AlertSettings.objects.filter(person_id__in=people)}
    missing = [AlertSettings(person=person) for pk, person in people.items() if pk not in preferences]
    AlertSettings.objects.bulk_create(missing, ignore_conflicts=True)
    if missing:
        preferences = {prefs.person_id: prefs for prefs in AlertSettings.objects.filter(person_id__in=people)}
    pending = {}
    counts = {}
    omitted = {}
    for txn in transactions:
        amount = abs(txn.amount_minor)
        link = reverse("transaction-edit", args=[txn.pk])
        title = f"Large transaction of {format_minor(amount, txn.currency)}"
        for person in audience[txn.account_id]:
            prefs = preferences[person.pk]
            threshold = prefs.large_transaction_minor
            if not prefs.large_transaction_enabled or threshold is None or threshold <= 0 or amount < threshold:
                continue
            key = (person.pk, f"large:{txn.pk}")
            if key in pending:
                continue
            if counts.get(person.pk, 0) >= MAX_LARGE_ALERTS:
                omitted.setdefault((person.pk, txn.account_id), txn)
                continue
            counts[person.pk] = counts.get(person.pk, 0) + 1
            pending[key] = Alert(
                recipient=person, kind=Alert.Kind.LARGE_TRANSACTION, title=title,
                link=link, dedupe_key=key[1], account=txn.account,
            )
    summarized = set()
    for (person_id, account_id), txn in omitted.items():
        if person_id in summarized:
            continue
        summarized.add(person_id)
        key = (person_id, f"large-summary:{account_id}:{transactions[0].pk}:{transactions[-1].pk}")
        pending[key] = Alert(
            recipient=people[person_id], kind=Alert.Kind.LARGE_TRANSACTION,
            title="More large transactions are available in your transaction list",
            link=f"{reverse('transaction-list')}?account={account_id}",
            dedupe_key=key[1], account=txn.account,
        )
    existing = set(Alert.objects.filter(
        recipient_id__in=people, dedupe_key__in=[key[1] for key in pending],
    ).values_list("recipient_id", "dedupe_key"))
    Alert.objects.bulk_create([alert for key, alert in pending.items() if key not in existing], ignore_conflicts=True)


def snapshot():
    return sorted(Alert.objects.values_list("recipient_id", "kind", "title", "link", "account_id", "dedupe_key"))


def mixed_household():
    """Two members with different thresholds, private and household accounts,
    and rows below, at and above each threshold."""
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household, name="Shared")
    owner_private = make_account(owner, name="Owner private")
    member_private = make_account(member, name="Member private")
    AlertSettings.objects.create(person=owner, large_transaction_minor=50_000)
    AlertSettings.objects.create(person=member, large_transaction_minor=10_000)
    amounts = (-9_999, -10_000, 10_001, -49_999, 50_000, -50_001, 120_000)
    for account, holder in ((shared, owner), (owner_private, owner), (member_private, member)):
        for i, amount in enumerate(amounts):
            make_transaction(holder, account, amount_minor=amount, description=f"{account.name} {i}")
    # Enough member-visible large rows to hit the detail cap and its summary.
    for i in range(MAX_LARGE_ALERTS):
        make_transaction(member, member_private, amount_minor=-15_000, description=f"Cap {i}")
    archived = make_transaction(owner, shared, amount_minor=-900_000, description="Archived")
    Transaction.objects.filter(pk=archived.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())
    return owner, member


@pytest.mark.django_db
def test_prefilter_matches_reference_for_both_entry_points(django_capture_on_commit_callbacks):
    owner, member = mixed_household()
    rows = Transaction.objects.order_by("pk").select_related("account")
    reference_large_transaction_alerts(list(rows))
    expected = snapshot()
    assert sum(1 for row in expected if row[5].startswith("large-summary:")) == 1
    assert {row[0] for row in expected} == {owner.pk, member.pk}

    Alert.objects.all().delete()
    created = evaluate_recent_large_transactions()
    assert snapshot() == expected
    assert sorted((a.recipient_id, a.dedupe_key) for a in created) == sorted((r[0], r[5]) for r in expected)
    assert all(alert.pk for alert in created)
    assert evaluate_recent_large_transactions() == []

    Alert.objects.all().delete()
    with django_capture_on_commit_callbacks(execute=True):
        schedule_after_new_transactions(list(Transaction.objects.all()))
    assert snapshot() == expected


@pytest.mark.django_db
def test_no_enabled_threshold_reads_no_transaction_rows(django_capture_on_commit_callbacks):
    owner, member = mixed_household()
    AlertSettings.objects.update(large_transaction_minor=None)
    AlertSettings.objects.filter(person=owner).update(large_transaction_minor=1, large_transaction_enabled=False)
    rows = list(Transaction.objects.all())
    with CaptureQueriesContext(connection) as queries:
        assert evaluate_recent_large_transactions() == []
    assert not [q for q in queries if "finance_transaction" in q["sql"]], [q["sql"] for q in queries]
    with patch.object(alert_services, "evaluate_active_budget_alerts", return_value=[]):
        with django_capture_on_commit_callbacks(execute=True):
            with CaptureQueriesContext(connection) as queries:
                schedule_after_new_transactions(rows)
    assert not [q for q in queries if "finance_transaction" in q["sql"]], [q["sql"] for q in queries]
    assert not Alert.objects.exists()


@pytest.mark.django_db
def test_rows_below_every_threshold_are_never_instantiated():
    owner, member = mixed_household()
    AlertSettings.objects.update(large_transaction_minor=1_000_000)
    built = []
    original = Transaction.from_db.__func__

    def counting_from_db(cls, *args, **kwargs):
        built.append(1)
        return original(cls, *args, **kwargs)

    with patch.object(Transaction, "from_db", classmethod(counting_from_db)):
        with CaptureQueriesContext(connection) as queries:
            assert evaluate_recent_large_transactions() == []
    assert built == []
    reads = [q["sql"] for q in queries if "finance_transaction" in q["sql"]]
    assert len(reads) == 1 and "original_fields" not in reads[0]
