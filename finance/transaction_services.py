"""Corrections to a transaction's date, description and amount.

The transaction edit view locks the rows and then calls correct_transaction.
The form only validates input; it never writes.
"""

from django.core.exceptions import ValidationError
from django.utils import timezone

from .calculated_balances import refresh_calculated_balances
from .models import Transaction, TransactionCorrectionHistory
from .splits import SPLIT_AMOUNT_ERROR


def _correction_history_rows(transaction, actor, recorded_at, new_date, new_description, new_amount_minor):
    rows = []
    if new_date != transaction.transaction_date:
        rows.append(
            TransactionCorrectionHistory(
                transaction=transaction,
                actor=actor,
                recorded_at=recorded_at,
                field_name=TransactionCorrectionHistory.Field.TRANSACTION_DATE,
                previous_date=transaction.transaction_date,
                new_date=new_date,
            )
        )
    if new_description != transaction.description:
        rows.append(
            TransactionCorrectionHistory(
                transaction=transaction,
                actor=actor,
                recorded_at=recorded_at,
                field_name=TransactionCorrectionHistory.Field.DESCRIPTION,
                previous_description=transaction.description,
                new_description=new_description,
            )
        )
    if new_amount_minor != transaction.amount_minor:
        rows.append(
            TransactionCorrectionHistory(
                transaction=transaction,
                actor=actor,
                recorded_at=recorded_at,
                field_name=TransactionCorrectionHistory.Field.AMOUNT_MINOR,
                previous_amount_minor=transaction.amount_minor,
                new_amount_minor=new_amount_minor,
                currency=transaction.currency,
            )
        )
    return rows


def correct_transaction(actor, transaction, cleaned_data):
    """Apply a validated correction; the caller holds the account and transaction locks.

    cleaned_data is TransactionCorrectionForm.cleaned_data. Raises ValidationError
    for a split's amount. Returns the transaction unchanged when nothing differs.
    """
    new_date = cleaned_data["transaction_date"]
    new_description = cleaned_data["description"]
    new_amount_minor = int(cleaned_data["amount"] * 100)
    recorded_at = timezone.now()
    history_rows = _correction_history_rows(
        transaction,
        actor,
        recorded_at,
        new_date,
        new_description,
        new_amount_minor,
    )
    if not history_rows:
        return transaction
    if (
        transaction.category_source == Transaction.CategorySource.SPLIT
        and new_amount_minor != transaction.amount_minor
    ):
        raise ValidationError(SPLIT_AMOUNT_ERROR)
    transaction.transaction_date = new_date
    transaction.description = new_description
    transaction.amount_minor = new_amount_minor
    transaction.save(update_fields=("transaction_date", "description", "amount_minor", "updated_at"))
    TransactionCorrectionHistory.objects.bulk_create(history_rows)
    refresh_calculated_balances(actor, transaction.account_id)
    return transaction
