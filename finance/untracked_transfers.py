"""Transfers to or from an account the planner doesn't track (#642).

A member marks a cash-flow row as money moved to or from an account the app
has no history for, such as a brokerage tracked by balance only. The mark is on
the row, so it holds for every viewer, and `exclusion_exists_for` leaves the row
out of income and spending wherever a marked transfer pair is left out. The
row's category is left as it is, so removing the mark makes the row count
again exactly as before.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .access import DENIED as _DENIED
from .access import require_person as _person_for
from .category_services import (
    _lock_accounts_and_transactions,
    _record_text_history,
    drop_refund_links_to_originals,
    with_transfer_state,
)
from .models import Account, Transaction, TransactionCorrectionHistory

UNTRACKED_LABEL = Transaction.UNTRACKED_TRANSFER_LABEL
COUNTED_LABEL = "Counted in income and spending"
INVESTMENT_REFUSED = (
    "Investment activity and investment accounts are already left out of income and spending, "
    "so they can't be marked."
)
LINKED_REFUND_REFUSED = "A linked refund follows its purchase, so it can't be marked. Unlink it first."
ALREADY_EXCLUDED = "This row is already left out of income and spending as a transfer."
NOT_MARKED = "This row is not marked as a transfer to or from an untracked account."
ALREADY_MARKED = "This row is already marked as a transfer to or from an untracked account."


def is_investment_row(txn):
    return txn.kind == Transaction.Kind.INVESTMENT_ACTIVITY or txn.account.account_type == Account.Type.INVESTMENT


def mark_refusal(txn):
    """Why `txn` cannot be marked, or None. `txn` carries the viewer's `_excluded`."""
    if is_investment_row(txn):
        return INVESTMENT_REFUSED
    if hasattr(txn, "refund_link"):
        return LINKED_REFUND_REFUSED
    if txn.untracked_transfer:
        return ALREADY_MARKED
    if txn.is_excluded_transfer:
        return ALREADY_EXCLUDED
    return None


def history_label(marked, suffix=""):
    return f"{UNTRACKED_LABEL}{suffix}" if marked else COUNTED_LABEL


def set_mark(person, txn, marked, *, label_suffix=""):
    """Write the mark on a locked row with its correction history.

    The history row's audit event follows the caller's origin: one event per
    row from the edit page, one bounded event for a bulk edit or rule run.
    Marking a purchase stops it counting, so its refund links are dropped the
    way archiving drops them (#570): each refund counts as income again.
    """
    previous = txn.untracked_transfer
    txn.untracked_transfer = marked
    txn.save(update_fields=("untracked_transfer", "updated_at"))
    if marked:
        drop_refund_links_to_originals([txn.pk], person)
    return _record_text_history(
        txn,
        person,
        TransactionCorrectionHistory.Field.UNTRACKED,
        history_label(previous),
        history_label(marked, label_suffix),
    )


def _locked_row(person, transaction_id):
    txn = (
        Transaction.objects.visible_to(person)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Transaction.objects.visible_to in the baseline
        .filter(pk=transaction_id, status=Transaction.Status.ACTIVE)
        .select_related("account")
        .first()
    )
    if txn is None:
        raise PermissionDenied(_DENIED)
    # Visible means editable here: every member who sees a row may edit it. The
    # lock helper rechecks visibility once the household and rows are locked.
    locked = _lock_accounts_and_transactions(person, [txn])[0]
    return with_transfer_state(person, locked)


@transaction.atomic
def mark_untracked_transfer(principal, transaction_id):
    person = _person_for(principal)
    txn = _locked_row(person, transaction_id)
    refusal = mark_refusal(txn)
    if refusal:
        raise ValidationError(refusal)
    set_mark(person, txn, True)
    _after_change()
    return txn


@transaction.atomic
def unmark_untracked_transfer(principal, transaction_id):
    person = _person_for(principal)
    txn = _locked_row(person, transaction_id)
    if not txn.untracked_transfer:
        raise ValidationError(NOT_MARKED)
    set_mark(person, txn, False)
    _after_change()
    return txn


def _after_change():
    # Budgets and alerts read spending, which the mark just changed.
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
