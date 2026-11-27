"""finance.account_locks and the joined-lock shapes its static contract rejects (#810).

The two probes are the concurrency reviewer's (#811 comment 7173541959): a
Transaction query that joins Account, locked with `of=()` or through a filter
join, locks the account row too, without going through lock_accounts or its
guard. A second connection's NOWAIT lock on that account then fails. That is
why tests/test_account_lock_contract.py rejects both shapes. Synthetic data.
"""

import threading

import pytest
from django.db import OperationalError, connection, connections, transaction

from finance.account_locks import AccountSetGrew, holding_accounts, lock_accounts
from finance.models import Account, Transaction
from tests.lock_races import JOIN_SECONDS
from tests.test_categorization import make_account, make_person, make_transaction


def _require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("row locks between two connections can only be exercised on PostgreSQL")


def _nowait_lock_fails_elsewhere(account_id):
    """Try `SELECT ... FOR UPDATE NOWAIT` on the account from another connection; True if it was refused."""
    outcome = []

    def probe():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT id FROM finance_account WHERE id = %s FOR UPDATE NOWAIT", [account_id])
            outcome.append(False)
        except OperationalError as exc:
            outcome.append("could not obtain lock" in str(exc))
        finally:
            connections.close_all()

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(timeout=JOIN_SECONDS)
    assert not worker.is_alive()
    return outcome == [True]


SHAPES = {
    "empty of= with select_related": lambda owner: Transaction.objects.select_for_update(of=()).select_related(
        "account"
    ),
    "filter join": lambda owner: Transaction.objects.select_for_update().filter(account__owner_id=owner.pk),
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_joined_transaction_lock_also_locks_the_account_outside_the_guard(shape):
    _require_postgres()
    owner = make_person("owner")
    account = make_account(owner, name="Synthetic Checking")
    row = make_transaction(owner, account, amount_minor=-1_000, description="SYNTHETIC ROW")

    with transaction.atomic(), holding_accounts(()):
        # No AccountSetGrew: the guard never sees this lock.
        assert SHAPES[shape](owner).filter(pk=row.pk).first() is not None
        assert _nowait_lock_fails_elsewhere(account.pk)


@pytest.mark.django_db
def test_lock_accounts_refuses_an_account_outside_the_held_set():
    owner = make_person("owner")
    held = make_account(owner, name="Synthetic Held")
    other = make_account(owner, name="Synthetic Other")

    with holding_accounts((held.pk,)):
        assert lock_accounts((held.pk,), pks_only=True) == [held.pk]
        with pytest.raises(AccountSetGrew) as grew:
            lock_accounts((held.pk, other.pk))
    assert grew.value.account_ids == {held.pk, other.pk}
    assert lock_accounts((other.pk,), queryset=Account.objects.filter(pk=held.pk)) == []
