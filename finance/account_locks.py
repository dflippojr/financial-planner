"""The one place that takes Account row locks (#810).

Every `SELECT ... FOR UPDATE` on accounts in finance/ goes through
lock_accounts or lock_account (tests/test_account_lock_contract.py fails on
any other). They lock in primary-key order, and while a member deletion runs
they hold its guard: the deletion locks every account it will touch in one
first pass and records that set with holding_accounts. A later request, from
any module, for an account outside the set raises AccountSetGrew before
locking, so the deletion rolls back, releasing every lock, and starts again
with a fresh first pass instead of taking a lower pk after a higher one.
Outside a deletion the guard does nothing.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .models import Account

_held_accounts: ContextVar[frozenset | None] = ContextVar("held_accounts", default=None)


class AccountSetGrew(Exception):
    """A member deletion attempt needs a lock its first pass did not take, so it starts again."""

    def __init__(self, account_ids, household_ids=frozenset()):
        super().__init__("member deletion account set grew")
        self.account_ids = set(account_ids)
        self.household_ids = set(household_ids)


@contextmanager
def holding_accounts(account_ids):
    """While inside, every account lock must name only these accounts (a member deletion's first pass)."""
    token = _held_accounts.set(frozenset(account_ids))
    try:
        yield
    finally:
        _held_accounts.reset(token)


def _require_held_accounts(account_ids):
    held = _held_accounts.get()
    if held is None:
        return
    wanted = set(account_ids)
    if not wanted <= held:
        raise AccountSetGrew(held | wanted)


def lock_accounts(account_ids, *, queryset=None, of_self=False, pks_only=False) -> list[Any]:
    """Lock these accounts in pk order and return the locked rows (or their pks with `pks_only`).

    `queryset` narrows what is locked and read, for example to the accounts a
    member can see, or adds annotations; only rows it returns are locked.
    `of_self` locks the account rows alone when the queryset joins others.
    The guard checks the requested ids, before any lock is taken. Rows are
    typed Any, as the scoped managers' querysets are (QuerySet.as_manager()).
    """
    ids = sorted({pk for pk in account_ids if pk is not None})
    if not ids:
        return []
    _require_held_accounts(ids)
    base = Account.objects.all() if queryset is None else queryset
    rows = base.select_for_update(of=("self",) if of_self else ()).filter(pk__in=ids).order_by("pk")
    if pks_only:
        return list(rows.values_list("pk", flat=True))
    return list(rows)


def lock_account(account_id, *, queryset=None, of_self=False) -> Any:
    """Lock one account as lock_accounts does; returns it, or None when `queryset` leaves it out."""
    rows = lock_accounts((account_id,), queryset=queryset, of_self=of_self)
    return rows[0] if rows else None


def update_accounts(queryset, **fields):
    """Run `queryset.update(**fields)` on Account rows, checking the guard first (review r6 on #811).

    An UPDATE takes each row's lock like `SELECT ... FOR UPDATE`, so inside a
    member deletion every account it would write must be held. The rows are
    read first and the guard checks them; the UPDATE then keeps the queryset's
    own filter and is limited to those rows. Returns the number updated.
    """
    ids = list(queryset.values_list("pk", flat=True))
    if not ids:
        return 0
    _require_held_accounts(ids)
    return queryset.filter(pk__in=ids).update(**fields)


def delete_accounts(queryset):
    """Delete these Account rows, checking the guard first, as update_accounts does."""
    ids = list(queryset.values_list("pk", flat=True))
    if not ids:
        return 0, {}
    _require_held_accounts(ids)
    return queryset.filter(pk__in=ids).delete()
