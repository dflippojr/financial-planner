from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from .audit_services import changed_names, record, snapshot as audit_snapshot
from .lifecycle_services import _DENIED, _person_for, lock_actor_household
from .models import Account, AuditEvent, BalanceSnapshot


class SnapshotError(Exception):
    """Safe, non-leaking error for a rejected manual snapshot write."""


DUPLICATE_MANUAL = "A manual balance already exists for this date."


def _lock_editable_account(principal, account_id):
    person = _person_for(principal)
    lock_actor_household(person)
    account = (
        Account.objects.visible_to(principal)
        .select_for_update(of=("self",))
        .filter(pk=account_id, status=Account.Status.ACTIVE, archived_at__isnull=True)
        .first()
    )
    if account is None:
        raise PermissionDenied(_DENIED)
    return account


def _lock_manual_snapshot(account, snapshot_id):
    snapshot = (
        BalanceSnapshot.objects.select_for_update(of=("self",))
        .filter(pk=snapshot_id, account=account, source=BalanceSnapshot.Source.MANUAL)
        .first()
    )
    if snapshot is None:
        raise PermissionDenied(_DENIED)
    return snapshot


SNAPSHOT_AUDIT_FIELDS = {"date": "snapshot_date", "amount": "amount_minor", "contribution": "net_contribution_minor"}


def _record_snapshot(person, account, snapshot, action, changed=()):
    record(person, action, AuditEvent.TargetType.BALANCE, snapshot.pk, audience={"account": account}, fields=changed)


def _apply_manual_fields(person, account, snapshot, *, snapshot_date, amount_minor, note, net_contribution_minor):
    before = audit_snapshot(snapshot, SNAPSHOT_AUDIT_FIELDS)
    note_changed = snapshot.note != note
    snapshot.snapshot_date = snapshot_date
    snapshot.amount_minor = amount_minor
    snapshot.note = note
    snapshot.net_contribution_minor = net_contribution_minor
    try:
        snapshot.save(update_fields=("snapshot_date", "amount_minor", "note", "net_contribution_minor"))
    except IntegrityError as exc:
        raise SnapshotError(DUPLICATE_MANUAL) from exc
    changed = changed_names(before, audit_snapshot(snapshot, SNAPSHOT_AUDIT_FIELDS))
    if note_changed:
        changed = sorted([*changed, "note"])
    if changed:
        _record_snapshot(person, account, snapshot, AuditEvent.Action.RECORD_EDITED, changed)
    return snapshot


@transaction.atomic
def record_manual_snapshot(principal, account_id, *, snapshot_date, amount_minor, note="", net_contribution_minor=None):
    account = _lock_editable_account(principal, account_id)
    person = _person_for(principal)
    existing = (
        BalanceSnapshot.objects.select_for_update(of=("self",))
        .filter(account=account, snapshot_date=snapshot_date, source=BalanceSnapshot.Source.MANUAL)
        .first()
    )
    if existing is not None:
        return _apply_manual_fields(
            person,
            account,
            existing,
            snapshot_date=snapshot_date,
            amount_minor=amount_minor,
            note=note,
            net_contribution_minor=net_contribution_minor,
        )
    try:
        created = BalanceSnapshot.objects.create(
            account=account,
            snapshot_date=snapshot_date,
            amount_minor=amount_minor,
            currency=account.currency,
            source=BalanceSnapshot.Source.MANUAL,
            note=note,
            net_contribution_minor=net_contribution_minor,
        )
    except IntegrityError as exc:
        raise SnapshotError(DUPLICATE_MANUAL) from exc
    _record_snapshot(person, account, created, AuditEvent.Action.RECORD_CREATED)
    return created


@transaction.atomic
def update_manual_snapshot(principal, account_id, snapshot_id, *, snapshot_date, amount_minor, note="", net_contribution_minor=None):
    account = _lock_editable_account(principal, account_id)
    snapshot = _lock_manual_snapshot(account, snapshot_id)
    return _apply_manual_fields(
        _person_for(principal),
        account,
        snapshot,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        note=note,
        net_contribution_minor=net_contribution_minor,
    )


@transaction.atomic
def delete_manual_snapshot(principal, account_id, snapshot_id):
    account = _lock_editable_account(principal, account_id)
    snapshot = _lock_manual_snapshot(account, snapshot_id)
    target_id = snapshot.pk
    snapshot.delete()
    record(_person_for(principal), AuditEvent.Action.RECORD_DELETED, AuditEvent.TargetType.BALANCE, target_id,
           audience={"account": account})
