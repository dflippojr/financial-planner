"""Workflow audit coverage (#273): the action matrix, privacy and cardinality."""
import json
import zipfile
from datetime import date
from io import BytesIO, StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse

from finance.audit_services import events_for, origin
from finance.bulk_edit_services import ACTION_CATEGORY, apply_bulk_edit, undo_bulk_edit
from finance.budget_services import reset_budget_rollover, save_budget, set_budget_archived, set_budget_rollover
from finance.category_services import (
    ensure_household_categories, add_category, assign_category, confirm_transfer_pair, rename_category, split_transaction,
)
from finance.csv_import.saved_mappings import (
    delete_or_archive_csv_mapping, save_csv_mapping, set_account_default_mapping, update_csv_mapping,
)
from finance.csv_import.services import commit_csv_import, undo_import_batch
from finance.lifecycle_services import delete_member_data, leave_household
from finance.manual_entry_services import add_manual_transaction, delete_manual_transaction
from finance.models import (
    Account, AuditEvent, Budget, Household, ImportBatch, Membership, PlannedItem, RecurringSeries,
    SavingsGoal, Transaction, TransactionCorrectionHistory,
)
from finance.planning_services import save_planned_item, set_planned_item_enabled
from finance.receipt_services import attach_receipt, remove_receipt
from finance.recurring_services import (
    add_recurring_members, cancel_recurring_series, confirm_recurring_series, dismiss_price_change,
    dismiss_recurring_series, merge_recurring_series, remove_recurring_member, undo_cancel_recurring_series,
)
from finance.rule_services import apply_rule, reverse_application, save_category_rule, set_rule_enabled
from finance.savings_goal_services import save_savings_goal, set_savings_goal_archived, set_savings_goal_completed
from finance.snapshot_services import delete_manual_snapshot, record_manual_snapshot, update_manual_snapshot
from finance.tag_services import add_tag, archive_tag, rename_tag, set_transaction_note_and_tags
from tests.test_manual_transactions import (
    CSV, ENTRY_DATE, category, import_csv, make_account, make_household, make_person, mapping, signed_in,
)
from tests.test_receipts import JPEG, make_transaction as make_receipt_transaction
from finance.csv_import.parser import read_csv

pytestmark = pytest.mark.django_db
A = AuditEvent.Action
T = AuditEvent.TargetType


def only(action, **filters):
    rows = list(AuditEvent.objects.filter(action=action, **filters))
    assert len(rows) == 1, [row.action for row in AuditEvent.objects.all()]
    return rows[0]


def other_household(person):
    household = Household.objects.create(name="Other Household")
    Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def household_pair():
    owner, member = make_person("owner"), make_person("member")
    household = make_household(owner, member)
    return owner, member, household


def make_txn(owner, account, description="Synthetic row", amount=-1000, day=2):
    batch = ImportBatch.objects.create(
        account=account, imported_by=owner, source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64, date_range_start=date(2026, 1, 1), date_range_end=date(2026, 12, 31),
    )
    return Transaction.objects.create(
        account=account, import_batch=batch, transaction_date=date(2026, 1, day), amount_minor=amount,
        description=description, source_row_number=2, fingerprint=f"{account.pk}{day}{amount}".encode().hex().ljust(64, "a")[:64],
        original_fields={"Synthetic Amount": str(amount)},
    )


# --- imports and manual entries -------------------------------------------------

def test_import_commit_no_new_rows_and_undo_identify_each_member():
    owner, member, household = household_pair()
    shared = make_account(owner, household=household)
    result = import_csv(owner, shared)
    committed = only(A.IMPORT_COMMITTED)
    assert (committed.actor_id, committed.target_type, committed.target_id) == (owner.pk, T.IMPORT_BATCH, result.batch.pk)
    assert committed.metadata == {"new_count": 1, "duplicate_count": 0, "invalid_count": 0,
                                  "format": "huntington", "batch_id": result.batch.pk}
    assert committed.account_id == shared.pk and committed.source == "ui"
    # An exact reimport creates no batch and exactly one distinct no-new-rows event.
    again = import_csv(member, shared)
    assert again.batch is None
    skipped = only(A.IMPORT_NO_NEW_ROWS)
    assert (skipped.actor_id, skipped.target_type, skipped.target_id) == (member.pk, T.ACCOUNT, shared.pk)
    assert skipped.metadata["duplicate_count"] == 1 and skipped.metadata["new_count"] == 0
    undo_import_batch(member, shared.pk, result.batch.pk)
    undone = only(A.IMPORT_UNDONE)
    assert undone.actor_id == member.pk != committed.actor_id
    assert undone.metadata["row_count"] == 1
    assert AuditEvent.objects.count() == 3
    # A denied undo and a second undo of the same batch write nothing.
    with pytest.raises(PermissionDenied):
        undo_import_batch(member, shared.pk, result.batch.pk)
    assert AuditEvent.objects.count() == 3


def test_import_event_carries_no_file_names_hashes_or_source_text():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    import_csv(owner, account)
    dump = json.dumps(list(AuditEvent.objects.values()), default=str)
    for canary in ("SYNTHETIC GROCER", "12.34", "09/27/2026", Transaction.objects.get().fingerprint,
                   ImportBatch.objects.get().source_file_sha256):
        assert canary not in dump


def test_failed_import_validation_records_nothing():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    with pytest.raises(ValidationError):
        commit_csv_import(owner, account.pk, content=CSV, document=read_csv(CSV), mapping=mapping(),
                          source=ImportBatch.Source.HUNTINGTON, date_range_start=date(2026, 9, 30),
                          date_range_end=date(2026, 9, 1))
    assert AuditEvent.objects.count() == 0


def test_manual_transaction_create_and_delete_link_existing_history():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    entry = add_manual_transaction(owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234,
                                   description="Synthetic canary description", note="Synthetic canary note")
    created = only(A.RECORD_CREATED, target_type=T.TRANSACTION)
    assert created.target_id == entry.pk and created.account_id == account.pk
    delete_manual_transaction(owner, entry.pk)
    deleted = only(A.RECORD_DELETED, target_type=T.TRANSACTION)
    history = TransactionCorrectionHistory.objects.get(transaction=entry, field_name="deleted")
    assert deleted.metadata["history_id"] == history.pk
    dump = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert "canary" not in dump and "1234" not in dump


def test_manual_delete_of_imported_row_and_foreign_account_write_nothing():
    owner, member, household = household_pair()
    outsider = make_person("outsider")
    other_household(outsider)
    imported = make_txn(owner, make_account(owner))
    with pytest.raises(PermissionDenied):
        delete_manual_transaction(owner, imported.pk)
    with pytest.raises(PermissionDenied):
        add_manual_transaction(outsider, make_account(owner, name="Hidden").pk, transaction_date=ENTRY_DATE,
                               amount_minor=-1, description="x")
    assert AuditEvent.objects.count() == 0


# --- balances, budgets, planned items and goals ----------------------------------

def test_balance_create_edit_upsert_and_delete_name_fields_not_values():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner, account_type=Account.Type.INVESTMENT)
    snapshot = record_manual_snapshot(owner, account.pk, snapshot_date=date(2026, 9, 1), amount_minor=123_456_789,
                                      note="Synthetic canary note")
    assert only(A.RECORD_CREATED, target_type=T.BALANCE).target_id == snapshot.pk
    # Same-date upsert with a changed amount and contribution is an edit, an identical one is not.
    record_manual_snapshot(owner, account.pk, snapshot_date=date(2026, 9, 1), amount_minor=1,
                           note="Synthetic canary note", net_contribution_minor=5)
    edited = only(A.RECORD_EDITED, target_type=T.BALANCE)
    assert edited.changed_fields == ["amount", "contribution"]
    record_manual_snapshot(owner, account.pk, snapshot_date=date(2026, 9, 1), amount_minor=1,
                           note="Synthetic canary note", net_contribution_minor=5)
    update_manual_snapshot(owner, account.pk, snapshot.pk, snapshot_date=date(2026, 9, 1), amount_minor=1,
                           note="Synthetic canary note", net_contribution_minor=5)
    assert AuditEvent.objects.count() == 2
    update_manual_snapshot(owner, account.pk, snapshot.pk, snapshot_date=date(2026, 9, 2), amount_minor=1,
                           note="Other canary note", net_contribution_minor=5)
    assert AuditEvent.objects.filter(action=A.RECORD_EDITED).count() == 2
    delete_manual_snapshot(owner, account.pk, snapshot.pk)
    assert only(A.RECORD_DELETED, target_type=T.BALANCE).target_id == snapshot.pk
    dump = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert "canary" not in dump and "123456789" not in dump


def test_balance_write_rolled_back_or_denied_records_nothing():
    owner, _member, household = household_pair()
    other = make_person("other")
    other_household(other)
    account = make_account(owner)
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            record_manual_snapshot(owner, account.pk, snapshot_date=date(2026, 9, 1), amount_minor=1)
            raise RuntimeError("rollback")
    with pytest.raises(PermissionDenied):
        record_manual_snapshot(other, account.pk, snapshot_date=date(2026, 9, 1), amount_minor=1)
    assert AuditEvent.objects.count() == 0


def budget_payload(groceries, **overrides):
    payload = {"scope": Budget.Scope.PRIVATE, "category": groceries, "amount_minor": 10_000,
               "effective_month": date(2026, 9, 1), "rollover_enabled": False}
    payload.update(overrides)
    return payload


def test_budget_lifecycle_events_use_scope_audience():
    owner, member, household = household_pair()
    groceries = category(household, "Groceries")
    dining = category(household, "Dining")
    private = save_budget(owner, budget_payload(groceries))
    shared = save_budget(owner, budget_payload(dining, scope=Budget.Scope.HOUSEHOLD))
    assert events_for(member).count() == 1 and events_for(owner).count() == 2
    save_budget(owner, budget_payload(groceries), budget=private)  # unchanged: no event
    save_budget(owner, budget_payload(groceries, amount_minor=20_000), budget=private)
    assert only(A.RECORD_EDITED, target_id=private.pk).changed_fields == ["amount"]
    set_budget_rollover(owner, private, True, month=date(2026, 9, 1))
    set_budget_rollover(owner, private, True, month=date(2026, 9, 1))
    assert only(A.ROLLOVER_TOGGLED).changed_fields == ["rollover"]
    reset_budget_rollover(owner, private, month=date(2026, 9, 1))
    set_budget_archived(owner, private, True)
    set_budget_archived(owner, private, True)
    set_budget_archived(owner, private, False)
    assert only(A.RECORD_ARCHIVED, target_id=private.pk).changed_fields == ["status"]
    assert only(A.RECORD_RESTORED, target_id=private.pk)
    assert AuditEvent.objects.filter(target_id=shared.pk, target_type=T.BUDGET).count() == 1
    assert all(row.actor_id == owner.pk for row in AuditEvent.objects.all())
    # The private budget's history never reaches the household member.
    assert {row.target_id for row in events_for(member)} == {shared.pk}
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            set_budget_archived(owner, shared, True)
            raise RuntimeError("rollback")
    assert not AuditEvent.objects.filter(target_id=shared.pk, action=A.RECORD_ARCHIVED).exists()


def test_planned_item_and_goal_events():
    owner = make_person("owner")
    household = make_household(owner)
    item = save_planned_item(owner, {"scope": "private", "name": "Synthetic canary item", "kind": "expense",
                                    "amount_minor": 99_999, "start_date": date(2026, 9, 1), "cadence": "monthly"})
    assert only(A.RECORD_CREATED, target_type=T.PLANNED_ITEM).target_id == item.pk
    set_planned_item_enabled(owner, item, False)
    set_planned_item_enabled(owner, item, False)
    assert only(A.RECORD_DISABLED, target_type=T.PLANNED_ITEM).changed_fields == ["enabled"]
    set_planned_item_enabled(owner, item, True)
    assert only(A.RECORD_ENABLED, target_type=T.PLANNED_ITEM)
    payload = {"scope": "household", "name": "Synthetic canary goal", "target_amount_minor": 5_000_000,
               "target_date": date(2027, 1, 1), "linked_account": None,
               "manual_amount_minor": None, "manual_amount_date": None}
    goal = save_savings_goal(owner, payload)
    assert only(A.RECORD_CREATED, target_type=T.GOAL).household_id == household.pk
    save_savings_goal(owner, payload, goal=goal)
    save_savings_goal(owner, {**payload, "target_amount_minor": 1}, goal=goal)
    assert only(A.RECORD_EDITED, target_type=T.GOAL).changed_fields == ["target"]
    set_savings_goal_completed(owner, goal, True)
    set_savings_goal_completed(owner, goal, True)
    assert only(A.RECORD_COMPLETED, target_type=T.GOAL)
    set_savings_goal_archived(owner, goal, True)
    set_savings_goal_archived(owner, goal, True)
    assert only(A.RECORD_ARCHIVED, target_type=T.GOAL)
    dump = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert "canary" not in dump and "99999" not in dump and "5000000" not in dump


# --- categories, tags, mappings, rules ------------------------------------------

def test_category_tag_and_mapping_changes_go_to_the_household():
    owner, member, household = household_pair()
    custom = add_category(owner, "Synthetic canary category")
    rename_category(member, custom.pk, "Synthetic canary renamed")
    rename_category(member, custom.pk, "Synthetic canary renamed")
    assert only(A.RECORD_CREATED, target_type=T.CATEGORY).actor_id == owner.pk
    renamed = only(A.RECORD_EDITED, target_type=T.CATEGORY)
    assert (renamed.actor_id, renamed.changed_fields) == (member.pk, ["name"])
    tag = add_tag(owner, "Synthetic canary tag")
    rename_tag(member, tag.pk, "Synthetic canary tag two")
    archive_tag(member, tag.pk)
    archive_tag(member, tag.pk)
    assert AuditEvent.objects.filter(target_type=T.TAG).count() == 3
    saved = save_csv_mapping(owner, name="Synthetic canary mapping", headers=["When", "Memo", "Amount"],
                             mapping=mapping())
    update_csv_mapping(member, saved.pk, name="Synthetic canary mapping 2")
    account = make_account(owner, household=household)
    set_account_default_mapping(owner, account.pk, saved.pk)
    set_account_default_mapping(owner, account.pk, saved.pk)
    assert only(A.DEFAULT_CHANGED).metadata == {"account_id": account.pk}
    delete_or_archive_csv_mapping(owner, saved.pk)
    assert only(A.RECORD_DELETED, target_type=T.CSV_MAPPING).target_id == saved.pk
    outsider = make_person("outsider")
    other_household(outsider)
    assert events_for(outsider).count() == 0
    dump = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert "canary" not in dump


def test_note_edits_have_no_event_but_tag_membership_changes_do():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_txn(owner, account)
    set_transaction_note_and_tags(owner, txn.pk, note="Synthetic canary note one", tag_ids=[])
    set_transaction_note_and_tags(owner, txn.pk, note="Synthetic canary note two", tag_ids=[])
    assert AuditEvent.objects.count() == 0
    tag = add_tag(owner, "Synthetic tag")
    set_transaction_note_and_tags(owner, txn.pk, note="Synthetic canary note two", tag_ids=[tag.pk])
    changed = only(A.TAGS_CHANGED)
    assert (changed.target_type, changed.target_id, changed.changed_fields) == (T.TRANSACTION, txn.pk, ["tags"])
    set_transaction_note_and_tags(owner, txn.pk, note="Different", tag_ids=[tag.pk])
    assert AuditEvent.objects.filter(action=A.TAGS_CHANGED).count() == 1
    assert "canary" not in json.dumps(list(AuditEvent.objects.values()), default=str)
    assert household


def rule_args(groceries, **overrides):
    args = {"owner_kind": "household", "description_contains": "SYNTHETIC CANARY", "account_id": None,
            "min_amount_minor": None, "max_amount_minor": None, "category_id": groceries.pk, "priority": 1}
    args.update(overrides)
    return args


def test_rule_edit_enable_apply_reverse_and_automatic_application_are_distinguishable():
    owner, member, household = household_pair()
    groceries = category(household, "Groceries")
    account = make_account(owner, household=household)
    make_txn(owner, account, description="SYNTHETIC CANARY STORE")
    rule = save_category_rule(owner, **rule_args(groceries))
    assert only(A.RECORD_CREATED, target_type=T.RULE).household_id == household.pk
    save_category_rule(owner, rule_id=rule.pk, **rule_args(groceries, priority=2))
    assert only(A.RECORD_EDITED, target_type=T.RULE).changed_fields == ["priority"]
    set_rule_enabled(owner, rule.pk, False)
    set_rule_enabled(owner, rule.pk, False)
    assert only(A.RECORD_DISABLED, target_type=T.RULE)
    set_rule_enabled(owner, rule.pk, True)
    application, _ = apply_rule(member, rule.pk)
    applied = only(A.RULE_APPLIED)
    assert applied.actor_id == member.pk and applied.source == "ui"
    assert applied.metadata == {"rule_application_id": application.pk, "row_count": 1}
    # One aggregate event, not one per transaction history row.
    assert AuditEvent.objects.filter(action=A.TRANSACTION_CORRECTED).count() == 0
    reverse_application(owner, application.pk)
    reversed_event = only(A.RULE_REVERSED)
    assert reversed_event.actor_id == owner.pk and reversed_event.metadata["row_count"] == 1
    assert "CANARY" not in json.dumps(list(AuditEvent.objects.values()), default=str)


def test_automatic_rule_application_after_import_uses_rule_source_and_one_event_per_rule():
    from finance.csv_import.services import categorize_imported_batch

    owner = make_person("owner")
    household = make_household(owner)
    groceries = category(household, "Groceries")
    account = make_account(owner, household=household)
    rule = save_category_rule(owner, **rule_args(groceries, description_contains="SYNTHETIC GROCER"))
    apply_rule(owner, rule.pk)  # confirms the preview for automatic use
    result = import_csv(owner, account)
    categorize_imported_batch(owner, result.batch)
    auto = only(A.RULE_APPLIED, source="rule")
    assert auto.target_id == rule.pk and auto.metadata["row_count"] == 1
    assert AuditEvent.objects.filter(action=A.TRANSACTION_CORRECTED).count() == 0


def test_former_member_reversing_household_rule_rows_is_audited_privately():
    owner, member, household = household_pair()
    groceries = category(household, "Groceries")
    lent = make_account(owner, household=household)
    Account.objects.filter(pk=lent.pk).update(share_mode=Account.ShareMode.LENT)
    make_txn(owner, lent, description="SYNTHETIC CANARY STORE")
    rule = save_category_rule(member, **rule_args(groceries))
    application, _ = apply_rule(member, rule.pk)
    leave_household(owner)
    reverse_application(owner, application.pk)
    event = only(A.RULE_REVERSED)
    assert (event.actor_id, event.private_owner_id, event.household_id) == (owner.pk, owner.pk, None)
    assert events_for(member).filter(action=A.RULE_REVERSED).count() == 0


# --- corrections, transfers, bulk edits, chat -----------------------------------

def test_manual_category_split_and_transfer_reference_correction_history():
    owner = make_person("owner")
    household = make_household(owner)
    groceries, dining = category(household, "Groceries"), category(household, "Dining")
    account = make_account(owner)
    txn = make_txn(owner, account)
    assign_category(owner, txn.pk, groceries.pk)
    corrected = only(A.TRANSACTION_CORRECTED)
    history = TransactionCorrectionHistory.objects.get(transaction=txn, field_name="category")
    assert corrected.metadata == {"history_id": history.pk} and corrected.changed_fields == ["category"]
    assert corrected.source == "ui" and corrected.account_id == account.pk
    assign_category(owner, txn.pk, groceries.pk)  # no change, no event
    assert AuditEvent.objects.count() == 1
    split_transaction(owner, txn.pk, [
        {"category_id": groceries.pk, "amount_minor": -600},
        {"category_id": dining.pk, "amount_minor": -400},
    ])
    assert AuditEvent.objects.filter(action=A.TRANSACTION_CORRECTED).count() == 2


def test_bulk_apply_and_undo_write_bounded_events_with_one_operation_id():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = category(household, "Groceries")
    first_account, second_account = make_account(owner), make_account(owner, name="Second")
    rows = [make_txn(owner, first_account, day=2), make_txn(owner, first_account, day=3),
            make_txn(owner, second_account, day=4)]
    ids = [row.pk for row in rows]
    matching = Transaction.objects.filter(pk__in=ids)
    _preview, undo = apply_bulk_edit(owner, matching=matching, transaction_ids=ids, select_matching=False,
                                     action=ACTION_CATEGORY, category_id=groceries.pk)
    applied = list(AuditEvent.objects.filter(action=A.BULK_APPLIED).order_by("target_id"))
    assert [(e.target_id, e.metadata["row_count"]) for e in applied] == [(first_account.pk, 2), (second_account.pk, 1)]
    assert {str(e.correlation_id) for e in applied} == {str(undo.pk)}
    assert {e.source for e in applied} == {"bulk"}
    assert AuditEvent.objects.filter(action=A.TRANSACTION_CORRECTED).count() == 0
    undo_bulk_edit(owner, undo.pk)
    undone = AuditEvent.objects.filter(action=A.BULK_UNDONE)
    assert undone.count() == 2 and {str(e.correlation_id) for e in undone} == {str(undo.pk)}
    assert all(e.changed_fields == ["category"] for e in undone)


def test_chat_origin_labels_events_with_proposal_and_operation():
    import uuid

    owner = make_person("owner")
    household = make_household(owner)
    groceries = category(household, "Groceries")
    txn = make_txn(owner, make_account(owner))
    operation = uuid.uuid4()
    with origin("chat_confirmation", correlation_id=operation, proposal_id=77):
        assign_category(owner, txn.pk, groceries.pk)
        save_budget(owner, budget_payload(groceries))
    events = list(AuditEvent.objects.all())
    assert {e.source for e in events} == {"chat_confirmation"}
    assert {e.correlation_id for e in events} == {operation}
    assert {e.metadata["proposal_id"] for e in events} == {77}
    assert all(e.actor_id == owner.pk for e in events)


# --- recurring, receipts and downloads --------------------------------------------

def series_for(person, name):
    return RecurringSeries.objects.create(
        person=person, merchant_key=name.lower(), display_name=name, cadence="monthly", typical_amount_minor=-1599,
        status=RecurringSeries.Status.SUGGESTED, confidence="high", reasons=["monthly"],
        fingerprint=name.encode().hex().ljust(64, "a")[:64])


def test_recurring_actions_record_private_events_without_names_or_amounts():
    owner, member, _household = household_pair()
    account = make_account(owner)
    rows = [make_txn(owner, account, description=f"Synthetic Canary Stream {i}", amount=-1599, day=2 + i) for i in range(3)]
    first, second = series_for(owner, "Synthetic Canary One"), series_for(owner, "Synthetic Canary Two")
    for series, row in ((first, rows[0]), (second, rows[1])):
        from finance.models import RecurringSeriesMember
        RecurringSeriesMember.objects.create(series=series, transaction=row, source="detected")
    confirm_recurring_series(owner, first.pk)
    dismiss_recurring_series(owner, second.pk)
    assert only(A.RECURRING_CONFIRMED).target_id == first.pk and only(A.RECURRING_DISMISSED).target_id == second.pk
    cancel_recurring_series(owner, first.pk)
    cancel_recurring_series(owner, first.pk)
    undo_cancel_recurring_series(owner, first.pk)
    only(A.RECURRING_CANCELED)
    only(A.RECURRING_RESUMED)
    dismiss_price_change(owner, first.pk)
    only(A.RECURRING_PRICE_ACKNOWLEDGED)
    assert events_for(member).count() == 0
    dump = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert "Canary" not in dump and "1599" not in dump
    assert {add_recurring_members, merge_recurring_series, remove_recurring_member}


def test_recurring_merge_keeps_surviving_id_after_source_removed():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    from finance.models import RecurringSeriesMember
    source, target = series_for(owner, "Source Series"), series_for(owner, "Target Series")
    for series, day in ((source, 2), (target, 3)):
        series.status = RecurringSeries.Status.POSSIBLE
        series.reasons = ["grouping edited manually"]
        series.save()
        RecurringSeriesMember.objects.create(series=series, transaction=make_txn(owner, account, day=day), source="manual")
    merge_recurring_series(owner, source.pk, target.pk)
    event = only(A.RECURRING_MERGED)
    assert (event.target_id, event.metadata) == (target.pk, {"source_id": source.pk, "surviving_id": target.pk})
    assert not RecurringSeries.objects.filter(pk=source.pk).exists()


def test_receipts_and_downloads_record_authorized_metadata_only(tmp_path, settings):
    settings.RECEIPT_STORAGE_DIR = tmp_path
    owner, member, household = household_pair()
    shared = make_account(owner, household=household)
    txn = make_receipt_transaction(owner, account=shared)
    receipt = attach_receipt(owner, txn.pk, SimpleUploadedFile("canary-name.jpg", JPEG, content_type="image/jpeg"))
    attached = only(A.RECEIPT_ATTACHED)
    assert (attached.target_id, attached.metadata["transaction_id"]) == (receipt.pk, txn.pk)
    client = signed_in(member)
    response = client.get(reverse("transaction-receipt-download", args=[txn.pk, receipt.pk]))
    assert response.status_code == 200
    downloaded = only(A.DOWNLOAD_PREPARED)
    assert downloaded.actor_id == member.pk and downloaded.metadata == {"export_kind": "receipt", "transaction_id": txn.pk}
    remove_receipt(member, txn.pk, receipt.pk)
    assert only(A.RECEIPT_REMOVED).target_id == receipt.pk
    assert "canary-name" not in json.dumps(list(AuditEvent.objects.values()), default=str)
    # The receipt's history is shared with the account's household and revoked with access.
    leave_household(member)
    assert events_for(member).count() == 0
    # A forged download of a receipt the member cannot see records nothing.
    count = AuditEvent.objects.count()
    assert client.get(reverse("transaction-receipt-download", args=[txn.pk, receipt.pk])).status_code == 404
    assert AuditEvent.objects.count() == count


def test_data_zip_and_year_end_downloads_record_enums_only():
    from tests.helpers import stamp_recent_auth

    owner = make_person("owner")
    make_household(owner)
    make_txn(owner, make_account(owner))
    client = signed_in(owner)
    response = client.get(reverse("year-end-csv", args=["spending"]), {"year": "2025", "scope": "private"})
    assert response.status_code == 200
    b"".join(response.streaming_content)
    year_end = only(A.DOWNLOAD_PREPARED)
    assert year_end.metadata == {"export_kind": "year_end_csv", "section": "spending"}
    assert year_end.target_type == T.EXPORT and year_end.private_owner_id == owner.pk
    assert "2025" not in json.dumps([year_end.metadata, year_end.changed_fields])
    stamp_recent_auth(client)
    zipped = client.post(reverse("account-export"))
    assert zipped.status_code == 200
    assert zipfile.is_zipfile(BytesIO(zipped.content))
    assert AuditEvent.objects.filter(metadata={"export_kind": "data_zip"}).count() == 1
    # Unauthenticated or unauthorized requests prepare no download.
    count = AuditEvent.objects.count()
    Client().get(reverse("year-end-csv", args=["spending"]))
    client.get(reverse("year-end-csv", args=["unknown"]))
    assert AuditEvent.objects.count() == count


# --- access, privacy and failure policy -------------------------------------------

def test_private_workflow_history_is_removed_with_member_data_and_shared_actor_anonymized():
    owner, member, household = household_pair()
    save_budget(owner, budget_payload(category(household, "Groceries")))
    save_budget(owner, budget_payload(category(household, "Dining"), scope=Budget.Scope.HOUSEHOLD))
    delete_member_data(owner)
    remaining = AuditEvent.objects.get()
    assert remaining.actor_id is None and remaining.household_id == household.pk
    assert events_for(member).count() == 1


def test_audit_write_gap_does_not_fail_the_workflow(caplog, monkeypatch):
    from django.db import DatabaseError

    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)

    def broken(self, *args, **kwargs):
        raise DatabaseError("synthetic canary parameter 12345")

    monkeypatch.setattr(AuditEvent, "save", broken)
    with caplog.at_level("ERROR"):
        with transaction.atomic():
            record_manual_snapshot(owner, account.pk, snapshot_date=date(2026, 9, 1), amount_minor=5)
    assert account.balance_snapshots.count() == 1
    assert "canary" not in caplog.text and "12345" not in caplog.text


def test_cli_and_page_list_workflow_events_for_authorized_members_only():
    owner, member, household = household_pair()
    save_budget(owner, budget_payload(category(household, "Groceries")))
    out = StringIO()
    call_command("query_audit", member=owner.pk, stdout=out)
    data = json.loads(out.getvalue())
    assert data["events"][0]["action"] == "record_created" and data["events"][0]["target_type"] == "budget"
    assert data["events"][0]["checksum_valid"] is True
    assert data["events"][0]["metadata"] == {}
    response = signed_in(member).get(reverse("settings-audit"))
    assert response.context["page"].paginator.count == 0
    response = signed_in(owner).get(reverse("settings-audit"), {"action": "record_created"})
    assert response.status_code == 200 and response.context["page"].paginator.count == 1


def test_metadata_allow_list_rejects_free_text_and_unknown_keys():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    from finance.audit_services import append_event

    for metadata in ({"file_name": "receipt-private.png"}, {"format": "Private bank"}, {"row_count": "12"},
                     {"row_count": True}, {"operation_id": "not-a-uuid"}, {"row_count": -1}):
        with pytest.raises(ValidationError) as error:
            with transaction.atomic():
                append_event(account=account, actor=owner, action=A.RECORD_CREATED, target_type=T.TRANSACTION,
                             target_id=1, metadata=metadata)
        assert "private" not in str(error.value).lower()
    assert AuditEvent.objects.count() == 0
