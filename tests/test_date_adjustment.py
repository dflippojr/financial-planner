"""Moving a late-posted transaction into the month it belongs to (owner decision 2026-09-30).

A charge that posts just after a month boundary can be corrected to the date it
belongs to. The move must change every report, must be recorded, and must not
make a reimport of the same export count the charge again.
"""
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from finance.category_services import income_and_spending_totals
from finance.models import Account, Transaction, TransactionCorrectionHistory
from tests.test_csv_import_overlap import commit, make_person


LATE_BILL_CSV = b"When,Memo,Amount,Currency\n10/01/2026,SYNTHETIC UTILITY SEPTEMBER BILL,-85.00,USD\n"
SEPTEMBER = {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}
OCTOBER = {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 31)}


@pytest.mark.django_db
def test_late_posted_charge_can_be_moved_into_the_month_it_belongs_to():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    import_range = {"date_range_start": date(2026, 9, 1), "date_range_end": date(2026, 10, 31)}
    commit(user, account, LATE_BILL_CSV, **import_range)
    bill = Transaction.objects.get(account=account)
    assert income_and_spending_totals(person, **OCTOBER).spending_minor == 8500

    client = Client()
    client.force_login(user)
    response = client.post(
        reverse("transaction-edit", args=(bill.pk,)),
        {"transaction_date": "2026-09-30", "description": bill.description, "amount": "-85.00"},
    )
    bill.refresh_from_db()

    assert response.status_code == 302
    assert bill.transaction_date == date(2026, 9, 30)
    assert income_and_spending_totals(person, **SEPTEMBER).spending_minor == 8500
    assert income_and_spending_totals(person, **OCTOBER).spending_minor == 0
    assert TransactionCorrectionHistory.objects.filter(
        transaction=bill,
        field_name=TransactionCorrectionHistory.Field.TRANSACTION_DATE,
        previous_date=date(2026, 10, 1),
        new_date=date(2026, 9, 30),
    ).exists()

    reimport = commit(user, account, LATE_BILL_CSV, **import_range)

    assert reimport.new_count == 0
    assert reimport.duplicate_count == 1
    assert Transaction.objects.filter(account=account, status="active").count() == 1
