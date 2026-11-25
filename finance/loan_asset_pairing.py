"""Loan-to-asset pairing: which asset account secures a loan. Transfer pairing is in transfer_pairing.py."""

from django.core.exceptions import PermissionDenied
from django.db import transaction

from .access import DENIED as _DENIED
from .access import visible_rows
from .audit_services import append_event
from .models import Account, AuditEvent, _person_for, loan_asset_pairing_allowed
from .snapshot_services import _lock_editable_account
from .account_locks import lock_account


class PairingError(Exception):
    """Safe, non-leaking error for a rejected loan-to-asset pairing."""


@transaction.atomic
def set_loan_secured_asset(principal, loan_id, asset_id):
    account = _lock_editable_account(principal, loan_id)
    if account.account_type != Account.Type.LOAN:
        raise PermissionDenied(_DENIED)
    person = _person_for(principal)
    if not asset_id:
        changed = account.secured_asset_id is not None
        account.secured_asset = None
        account.save(update_fields=("secured_asset", "updated_at"))
        if changed:
            _audit_pairing(account, person)
        return account
    asset = lock_account(
        asset_id,
        queryset=visible_rows(Account, principal).filter(status=Account.Status.ACTIVE, archived_at__isnull=True),
        of_self=True,
    )
    if asset is None:
        raise PermissionDenied(_DENIED)
    if not loan_asset_pairing_allowed(account, asset):
        raise PairingError(Account.PAIRING_REJECTED)
    changed = account.secured_asset_id != asset.pk
    account.secured_asset = asset
    account.save(update_fields=("secured_asset", "updated_at"))
    if changed:
        _audit_pairing(account, person)
    return account


def _audit_pairing(account, person):
    append_event(account=account, action=AuditEvent.Action.LOAN_PAIRING_CHANGED, actor=person,
                 changed_fields=("secured_asset",))
