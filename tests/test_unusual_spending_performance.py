"""Differential semantics and bounded work for the chronological merchant pass."""
import random
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from finance import unusual_spending as spending
from finance.alert_services import settings_for
from finance.models import Account, Membership, Transaction, TransferPair
from tests.test_unusual_spending import (
    SEP, make_account, make_household, make_person, make_transaction,
)
from tests.unusual_spending_reference import reference_merchant_flags


def reference_flags(person, **filters):
    # The unchanged category policy plus the old full-object merchant oracle.
    with patch.object(spending, "_merchant_flags", reference_merchant_flags):
        return spending.compute_unusual_flags(person, SEP, **filters)


@pytest.mark.django_db
@pytest.mark.parametrize("threshold", [None, 0, 5000])
def test_complete_flags_match_scan_sort_reference(threshold):
    person = make_person("differential")
    other = make_person("other")
    household = make_household(person, other)
    private = make_account(person)
    shared = make_account(other, scope=Account.Scope.HOUSEHOLD, household=household)
    hidden = make_account(other)
    archived = make_account(person)
    archived.status = Account.Status.ARCHIVED
    archived.archived_at = timezone.now()
    archived.save()
    prefs = settings_for(person)
    prefs.large_transaction_minor = threshold
    prefs.save()
    # Same-day IDs, minimum-count boundary, exact 2x, and half-even .5 medians.
    for merchant, amounts in (
        ("Synthetic Even", [1000, 1000, 1001, 1001, 2001, 2002]),
        ("Synthetic Round Down", [1000, 1000, 1001, 1001, 5000]),
        ("Synthetic Odd", [1001, 1002, 1002, 2004, 2005]),
        ("Synthetic Short", [10, 10, 10000, 10001]),
        ("Synthetic New", [5000, 15000, 15000, 50000]),
        ("Synthetic Below", [4999]),
        ("Synthetic Round Up", [1001, 1001, 1002, 1002, 5000]),
    ):
        for index, amount in enumerate(amounts):
            make_transaction(
                person, private, transaction_date=SEP, amount_minor=-amount,
                description=merchant.upper() if index % 2 else merchant,
            )
    rng = random.Random(265)
    for index in range(180):
        account = rng.choice([private, shared, hidden, archived])
        row = make_transaction(
            account.owner, account,
            transaction_date=SEP + timedelta(days=rng.randint(-900, 35)),
            amount_minor=rng.choice([-5000, -1000, -1001, -1002, -2001, -2002, 0, 1000]),
            description=rng.choice(["Synthetic-Random-A!!", "Synthetic Random A", "Synthetic Random B", "Synthetic Random C", "!!!"]),
            kind=Transaction.Kind.INVESTMENT_ACTIVITY if index % 11 == 0 else Transaction.Kind.CASH_FLOW,
        )
        if index % 13 == 0:
            row.status = Transaction.Status.ARCHIVED
            row.archived_at = timezone.now()
            row.save()
    for filters in ({}, {"account": private}, {"scope": "private"}, {"scope": "household"}):
        actual = spending.compute_unusual_flags(person, SEP, **filters)
        assert actual == reference_flags(person, **filters)
    if threshold == 5000:
        merchants = [item for item in spending.compute_unusual_flags(person, SEP) if item["kind"] == "merchant"]
        assert any(item["median_minor"] == 1000 for item in merchants)
        assert any(item["median_minor"] == 1002 for item in merchants)


@pytest.mark.django_db
def test_transfer_visibility_and_membership_revocation_match_reference():
    person = make_person("viewer")
    other = make_person("partner")
    household = make_household(person, other)
    private = make_account(person)
    shared = make_account(other, scope=Account.Scope.HOUSEHOLD, household=household)
    hidden = make_account(other)
    prefs = settings_for(person)
    prefs.large_transaction_minor = 5000
    prefs.save()
    for counterpart in (private, hidden):
        outflow = make_transaction(other, shared, transaction_date=SEP, amount_minor=-9000, description="Synthetic transfer")
        inflow = make_transaction(counterpart.owner, counterpart, transaction_date=SEP, amount_minor=9000, description="Hidden counterpart detail")
        TransferPair.objects.create(
            leg_a=outflow, leg_b=inflow, status=TransferPair.Status.CONFIRMED,
            kind=TransferPair.Kind.TRANSFER, confidence=TransferPair.Confidence.HIGH,
            reasons=["synthetic"],
        )
    assert spending.compute_unusual_flags(person, SEP) == reference_flags(person)
    flags = spending.compute_unusual_flags(person, SEP, scope="household")
    assert flags == reference_flags(person, scope="household")
    assert any(item["name"] == "Synthetic transfer" for item in flags)
    assert "Hidden counterpart detail" not in str(flags)
    Membership.objects.filter(person=person, household=household).delete()
    assert spending.compute_unusual_flags(person, SEP) == reference_flags(person) == []


@pytest.mark.django_db
def test_frequent_merchant_inserts_each_eligible_charge_once():
    person = make_person("frequent")
    make_household(person)
    account = make_account(person)
    first = make_transaction(person, account, transaction_date=date(2020, 1, 1), amount_minor=-1001, description="Synthetic frequent")
    rows = [Transaction(
        account=account, import_batch=first.import_batch,
        transaction_date=date(2020, 1, 1) if index < 10000 else SEP,
        amount_minor=-1000 - index % 3, description="Synthetic frequent",
        source_row_number=index + 3, fingerprint=f"{index:064x}", original_fields={"unused": "synthetic"},
    ) for index in range(11000)]
    Transaction.objects.bulk_create(rows, batch_size=2000)
    additions = []
    medians = []
    original_add = spending._RunningMedian.add
    original_median = spending._RunningMedian.median

    def counted_add(self, amount):
        additions.append(amount)
        return original_add(self, amount)

    def counted_median(self):
        medians.append(len(self))
        return original_median(self)

    with patch.object(spending._RunningMedian, "add", counted_add), patch.object(spending._RunningMedian, "median", counted_median), patch.object(spending, "_median_minor", side_effect=AssertionError("merchant histories must not be sorted")):
        with CaptureQueriesContext(connection) as queries:
            flags = spending._merchant_flags(person, SEP, date(2026, 9, 30), settings_for(person), [account], set())
    assert flags == []
    assert len(additions) == 11001
    assert medians == list(range(10001, 11001))
    transaction_queries = [query["sql"] for query in queries if 'FROM "finance_transaction"' in query["sql"]]
    assert len(transaction_queries) == 1
    projection = transaction_queries[0].split(" FROM ")[0]
    assert "original_fields" not in projection
    assert '"finance_account"."name"' not in projection


@pytest.mark.parametrize("values", [[], [2], [2, 1], [3, 1, 2], [10**18, 10**18 + 1, 1, 2], list(range(50)), list(range(50, 0, -1))])
def test_running_median_matches_exact_sorted_prefixes(values):
    median = spending._RunningMedian()
    assert median.median() == Decimal(0)
    for index, value in enumerate(values):
        median.add(value)
        assert median.median() == spending._median_minor(values[:index + 1])

