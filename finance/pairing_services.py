from django.core.exceptions import PermissionDenied
from django.db import transaction

from .lifecycle_services import _DENIED
from .models import Account, loan_asset_pairing_allowed
from .snapshot_services import _lock_editable_account


class PairingError(Exception):
    """Safe, non-leaking error for a rejected loan-to-asset pairing."""


@transaction.atomic
def set_loan_secured_asset(principal, loan_id, asset_id):
    account = _lock_editable_account(principal, loan_id)
    if account.account_type != Account.Type.LOAN:
        raise PermissionDenied(_DENIED)
    if not asset_id:
        account.secured_asset = None
        account.save(update_fields=("secured_asset", "updated_at"))
        return account
    asset = (
        Account.objects.visible_to(principal)
        .select_for_update(of=("self",))
        .filter(pk=asset_id, status=Account.Status.ACTIVE, archived_at__isnull=True)
        .first()
    )
    if asset is None:
        raise PermissionDenied(_DENIED)
    if not loan_asset_pairing_allowed(account, asset):
        raise PairingError(Account.PAIRING_REJECTED)
    account.secured_asset = asset
    account.save(update_fields=("secured_asset", "updated_at"))
    return account
