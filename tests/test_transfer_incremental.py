from datetime import date, timedelta
from unittest.mock import patch
import random

import pytest
from django.db import connection, transaction
from django.utils import timezone

from finance.category_services import (
    refresh_transfer_pairs, transfer_matching_key, confirm_transfer_pair,
    dismiss_transfer_pair, undo_transfer_pair,
)
from finance.csv_import.services import categorize_imported_batch
from finance.models import Transaction, TransferPair, TransactionCorrectionHistory
from tests.test_categorization import make_person, make_household, make_account, make_transaction


def snapshot():
    return (
        list(TransferPair.objects.order_by('leg_a_id', 'leg_b_id').values_list(
            'leg_a_id', 'leg_b_id', 'status', 'confidence', 'kind', 'reasons',
            'leg_a_category_id_at_mark', 'leg_b_category_id_at_mark')),
        list(Transaction.objects.order_by('pk').values_list('pk', 'category_id', 'category_source')),
        list(TransactionCorrectionHistory.objects.order_by('transaction_id', 'field_name', 'previous_description', 'new_description').values_list(
            'transaction_id', 'field_name', 'previous_description', 'new_description')),
    )


def assert_matches_rebuild(person, ids, previous_keys=()):
    # Start both paths from the same exact settled state, with the same pks.
    sid = transaction.savepoint()
    refresh_transfer_pairs(person)
    expected = snapshot()
    transaction.savepoint_rollback(sid)
    refresh_transfer_pairs(person, transaction_ids=ids, previous_keys=previous_keys)
    assert snapshot() == expected


@pytest.mark.django_db
def test_incremental_matches_full_after_imports_edits_archives_and_settlements():
    random.seed(42)
    person = make_person('incremental')
    other = make_person('other')
    household = make_household(person, other)
    accounts = [make_account(person, name=f'Synthetic {i}', account_type='credit_card' if i == 2 else 'checking') for i in range(3)]
    # Include shared and invisible private accounts in the same household.
    accounts.append(make_account(other, scope='household', household=household))
    hidden = make_account(other)
    make_transaction(other, hidden, amount_minor=1234)
    rows = []
    for i in range(80):
        rows.append(make_transaction(person, random.choice(accounts),
            amount_minor=random.choice([-1234, 1234, -2000, 2000, -9000, 9000]),
            transaction_date=date(2026, 1, 1) + timedelta(days=random.randrange(40))))
    for amount in (71111, 82222):
        rows.extend([make_transaction(person, accounts[0], amount_minor=-amount),
                     make_transaction(person, accounts[2], amount_minor=amount)])
    refresh_transfer_pairs(person)
    # Exercise each settled state and invalidation of confirmed/auto-marked legs.
    for status in ('confirm', 'dismiss', 'undo'):
        pair = TransferPair.objects.filter(status='suggested' if status != 'undo' else 'auto_marked').first()
        assert pair is not None
        {'confirm': confirm_transfer_pair, 'dismiss': dismiss_transfer_pair, 'undo': undo_transfer_pair}[status](person, pair.pk)
    assert set(TransferPair.objects.values_list("status", flat=True)) >= {
        "suggested", "auto_marked", "confirmed", "dismissed", "undone"
    }
    for i in range(30):
        if i % 3 == 0:
            row = make_transaction(person, random.choice(accounts), amount_minor=random.choice([-1234, 1234, -2000, 2000]),
                transaction_date=date(2026, 1, 1) + timedelta(days=random.randrange(40)))
            rows.append(row)
            assert_matches_rebuild(person, [row.pk])
        else:
            row = random.choice(rows)
            row.refresh_from_db()
            old = transfer_matching_key(row)
            row.amount_minor = random.choice([-1234, 1234, -2000, 2000, -3333])
            row.transaction_date += timedelta(days=random.choice([-15, 15]))
            row.description = f'Synthetic correction {i}'
            row.save()
            assert_matches_rebuild(person, [row.pk], [old])
    row = rows[-1]
    row.status = 'archived'; row.archived_at = timezone.now(); row.save()
    assert_matches_rebuild(person, [row.pk])


@pytest.mark.django_db
def test_incremental_revisits_old_neighbors_without_a_stored_pair():
    p = make_person('old'); make_household(p)
    a, b = make_account(p), make_account(p)
    rows = [make_transaction(p, a, amount_minor=-1000), make_transaction(p, a, amount_minor=-1000),
            make_transaction(p, b, amount_minor=1000)]
    refresh_transfer_pairs(p)
    old = transfer_matching_key(rows[1])
    rows[1].amount_minor = -2000; rows[1].save()
    assert_matches_rebuild(p, [rows[1].pk], [old])


@pytest.mark.django_db
def test_duplicate_batch_and_empty_ids_do_not_score_or_query():
    with patch('finance.category_services._score_pairs') as score:
        assert categorize_imported_batch(None, None) == []
        assert refresh_transfer_pairs(None, transaction_ids=[]) == []
    score.assert_not_called()


@pytest.mark.django_db
def test_incremental_does_not_materialize_unrelated_ledger():
    p = make_person('bounded'); make_household(p)
    a, b = make_account(p), make_account(p)
    seed = make_transaction(p, a, amount_minor=-1000)
    mate = make_transaction(p, b, amount_minor=1000)
    Transaction.objects.bulk_create([
        Transaction(account=a, import_batch=seed.import_batch, transaction_date=date(2020,1,1),
                    amount_minor=-4000-i, description='Synthetic unrelated', source_row_number=i+2,
                    fingerprint=f'{i:064x}', original_fields={}) for i in range(20000)
    ], batch_size=1000)
    from finance import category_services
    original = category_services._score_pairs
    with patch.object(category_services, '_score_pairs', wraps=original) as score:
        refresh_transfer_pairs(p, transaction_ids=[seed.pk])
    assert {row.pk for row in score.call_args.args[0]} == {seed.pk, mate.pk}
    assert TransferPair.objects.get().status == 'auto_marked'


@pytest.mark.django_db
def test_maintenance_command_requires_explicit_scope_and_rebuilds():
    from django.core.management import call_command
    from django.core.management.base import CommandError
    p = make_person('maintenance'); make_household(p)
    a, b = make_account(p), make_account(p)
    make_transaction(p, a, amount_minor=-1000)
    make_transaction(p, b, amount_minor=1000)
    for args in ((), ('--username', 'maintenance', '--all'), ('--username', 'missing')):
        with pytest.raises(CommandError):
            call_command('rebuild_transfer_pairs', *args)
    call_command('rebuild_transfer_pairs', '--username', 'maintenance')
    assert TransferPair.objects.get().status == 'auto_marked'
    call_command('rebuild_transfer_pairs', '--all')
    assert TransferPair.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_overlapping_incremental_refreshes_lock_in_order_and_do_not_duplicate():
    if connection.vendor != 'postgresql':
        pytest.skip('row locking requires PostgreSQL')
    import threading
    from django.db import connections
    p = make_person('concurrent'); make_household(p)
    a, b = make_account(p), make_account(p)
    left = make_transaction(p, a, amount_minor=-1000)
    right = make_transaction(p, b, amount_minor=1000)
    barrier = threading.Barrier(2)
    errors = []

    def run(ids):
        try:
            barrier.wait(timeout=10)
            refresh_transfer_pairs(p, transaction_ids=ids)
        except Exception as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    workers = [threading.Thread(target=run, args=([row.pk],)) for row in (left, right)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert TransferPair.objects.get().status == 'auto_marked'
