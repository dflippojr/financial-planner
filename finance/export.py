"""Build a member's data export zip. Do not log export contents."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from django.apps import apps

from .models import (
    Account,
    AiJob,
    AiProviderConnection,
    AiUsageEvent,
    Category,
    ImportBatch,
    PrivacyPolicyAcceptance,
    RecurringSeries,
    RefundLink,
    Transaction,
    TransactionSplit,
    TransferPair,
)

CENTS = Decimal("0.01")
ENTITY_FILES = (
    "accounts",
    "categories",
    "transactions",
    "import_batches",
    "transfer_pairs",
    "transaction_splits",
    "recurring_series",
    "privacy_policy_acceptances",
    "ai_connections",
    "ai_jobs",
    "ai_usage",
)
CSV_FIELDS = {
    "accounts": (
        "id",
        "name",
        "account_type",
        "owner_username",
        "scope",
        "share_mode",
        "household_id",
        "currency",
        "status",
        "archived_at",
        "created_at",
    ),
    "categories": ("id", "household_id", "name", "code", "created_at"),
    "transactions": (
        "id",
        "account_id",
        "import_batch_id",
        "transaction_date",
        "amount_minor",
        "amount_decimal",
        "currency",
        "description",
        "kind",
        "source_row_number",
        "source_transaction_id",
        "category_id",
        "category_name",
        "excluded_from_income_and_spending",
        "refund_original_id",
        "refund_original_part_id",
        "status",
        "archived_at",
        "fingerprint",
    ),
    "import_batches": (
        "id",
        "account_id",
        "source",
        "source_file_sha256",
        "date_range_start",
        "date_range_end",
        "status",
        "archived_at",
        "imported_at",
        "imported_by_username",
    ),
    "transfer_pairs": ("id", "leg_a_id", "leg_b_id", "status", "kind", "confidence", "reasons", "created_at"),
    "transaction_splits": (
        "id",
        "transaction_id",
        "position",
        "category_id",
        "category_name",
        "amount_minor",
        "amount_decimal",
    ),
    "recurring_series": (
        "id",
        "merchant_key",
        "display_name",
        "cadence",
        "typical_amount_minor",
        "typical_amount_decimal",
        "currency",
        "status",
        "confidence",
        "is_active",
        "member_transaction_ids",
        "fingerprint",
    ),
    "privacy_policy_acceptances": (
        "id",
        "policy_version",
        "is_material",
        "accepted_at",
    ),
    "ai_connections": (
        "id",
        "kind",
        "base_url",
        "chat_backend",
        "background_backend",
        "connected_at",
    ),
    "ai_jobs": (
        "id",
        "feature",
        "backend",
        "status",
        "attempts",
        "input_refs",
        "result_ref",
        "failure_code",
        "created_at",
        "finished_at",
    ),
    "ai_usage": (
        "id",
        "provider",
        "backend",
        "feature",
        "prompt_tokens",
        "completion_tokens",
        "outcome",
        "created_at",
    ),
    "balance_snapshots": (
        "id",
        "account_id",
        "snapshot_date",
        "amount_minor",
        "amount_decimal",
        "currency",
        "source",
        "note",
    ),
}

README_TEXT = """Financial Planner data export
=============================

Sign convention: a negative amount means money out of the account; a positive
amount means money in. Amounts appear as integer minor units (cents for USD)
and as a decimal string, together with the ISO currency code, on every money
field.

This zip holds one CSV and one JSON file per entity that is visible to the
member who requested the export: their private accounts plus household-shared
accounts. Another member's private accounts, their transactions, and any pair
or link that touches those private records are omitted.

Archived (undone) transactions and import batches are included. The status
column is "archived" when the row is not active.

privacy_policy_acceptances.csv records each time this member accepted a
privacy-policy version, including the version number and whether that version
was material.

Files
-----
accounts.csv / accounts.json
  id, name, account_type, owner_username, scope, share_mode (co_owned or lent
  for household accounts, empty for private), household_id, currency,
  status, archived_at, created_at

categories.csv / categories.json
  Household categories visible to the member.
  id, household_id, name, code, created_at

transactions.csv / transactions.json
  id, account_id, import_batch_id, transaction_date, amount_minor,
  amount_decimal, currency, description, kind, source_row_number,
  source_transaction_id, category_id, category_name,
  excluded_from_income_and_spending, refund_original_id, refund_original_part_id, status, archived_at,
  fingerprint
  excluded_from_income_and_spending is true only when both legs of an
  excluding transfer pair are visible. refund_original_id is set only when the
  original transaction is also visible. refund_original_part_id is set when that
  original is split and the chosen part is on a visible transaction.

import_batches.csv / import_batches.json
  Provenance for imports on visible accounts.
  id, account_id, source, source_file_sha256, date_range_start, date_range_end,
  status, archived_at, imported_at, imported_by_username

transfer_pairs.csv / transfer_pairs.json
  Pairs whose legs are both visible.
  id, leg_a_id, leg_b_id, status, kind, confidence, reasons, created_at

transaction_splits.csv / transaction_splits.json
  Parts of visible split transactions.
  id, transaction_id, position, category_id, category_name, amount_minor, amount_decimal

recurring_series.csv / recurring_series.json
  Series belonging to the exporting member that do not include hidden
  transactions.
  id, merchant_key, display_name, cadence, typical_amount_minor,
  typical_amount_decimal, currency, status, confidence, is_active,
  member_transaction_ids, fingerprint

balance_snapshots.csv / balance_snapshots.json
  Dated account balances (SimpleFIN or manual) for visible accounts:
  id, account_id, snapshot_date, amount_minor, amount_decimal, currency, source, note,
  net_contribution_minor, net_contribution_decimal.
  net_contribution_minor is null except on manual statement entries.
  Present only when the balance snapshot model exists in this installation.

privacy_policy_acceptances.csv / privacy_policy_acceptances.json
  This member's policy-acceptance rows: id, policy_version, is_material,
  accepted_at.

ai_connections.csv / ai_connections.json
  This member's AI provider connections: kind, base URL, chosen backends.
  Tokens and other secrets are omitted.

ai_jobs.csv / ai_jobs.json
  Background AI jobs for this member. Inputs are references (ids), never
  copies of financial rows. Prompts and model answers are omitted.

ai_usage.csv / ai_usage.json
  This member's AI usage: provider, backend, feature, token counts, outcome.
  Prompts and responses are omitted.

JSON files are arrays of objects. CSV uses UTF-8. Nested lists in CSV are JSON
arrays. Date and datetime values are ISO-8601.
"""


def money_decimal(minor: int) -> str:
    return str((Decimal(minor) / Decimal(100)).quantize(CENTS))


def minor_from_decimal_string(value: str) -> int:
    return int((Decimal(value) * Decimal(100)).quantize(Decimal("1")))


def export_filename(when: date) -> str:
    return f"financial-planner-export-{when.strftime('%Y%m%d')}.zip"


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _write_csv(buf: io.StringIO, rows: list[dict], fieldnames: tuple[str, ...]) -> None:
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_json(buf: io.StringIO, rows: list[dict]) -> None:
    payload = [{key: _json_value(value) for key, value in row.items()} for row in rows]
    json.dump(payload, buf, indent=2, ensure_ascii=False)
    buf.write("\n")


def _account_rows(person):
    rows = []
    accounts = Account.objects.visible_to(person).select_related("owner__user").order_by("pk")
    for account in accounts:
        rows.append(
            {
                "id": account.pk,
                "name": account.name,
                "account_type": account.account_type,
                "owner_username": account.owner.user.username,
                "scope": account.scope,
                "share_mode": account.share_mode,
                "household_id": account.household_id,
                "currency": account.currency,
                "status": account.status,
                "archived_at": account.archived_at,
                "created_at": account.created_at,
            }
        )
    return rows


def _category_rows(person):
    rows = []
    categories = Category.objects.visible_to(person).order_by("pk")
    for category in categories:
        rows.append(
            {
                "id": category.pk,
                "household_id": category.household_id,
                "name": category.name,
                "code": category.code,
                "created_at": category.created_at,
            }
        )
    return rows


def _excluded_transaction_ids(visible_txn_ids):
    pairs = TransferPair.objects.excluding_income_and_spending().filter(
        leg_a_id__in=visible_txn_ids,
        leg_b_id__in=visible_txn_ids,
    )
    ids = set()
    for pair in pairs:
        ids.add(pair.leg_a_id)
        ids.add(pair.leg_b_id)
    return ids


def _visible_refund_originals(visible_txn_ids):
    links = RefundLink.objects.filter(
        refund_id__in=visible_txn_ids,
        original_id__in=visible_txn_ids,
    )
    return {link.refund_id: (link.original_id, link.original_part_id) for link in links}


def _transaction_rows(person, visible_txn_ids):
    excluded = _excluded_transaction_ids(visible_txn_ids)
    refunds = _visible_refund_originals(visible_txn_ids)
    rows = []
    transactions = (
        Transaction.objects.visible_to(person)
        .select_related("category")
        .order_by("pk")
    )
    for txn in transactions:
        original_id, original_part_id = refunds.get(txn.pk, (None, None))
        rows.append(
            {
                "id": txn.pk,
                "account_id": txn.account_id,
                "import_batch_id": txn.import_batch_id,
                "transaction_date": txn.transaction_date,
                "amount_minor": txn.amount_minor,
                "amount_decimal": money_decimal(txn.amount_minor),
                "currency": txn.currency,
                "description": txn.description,
                "kind": txn.kind,
                "source_row_number": txn.source_row_number,
                "source_transaction_id": txn.source_transaction_id,
                "category_id": txn.category_id,
                "category_name": txn.category.name if txn.category_id else "",
                "excluded_from_income_and_spending": txn.pk in excluded,
                "refund_original_id": original_id,
                "refund_original_part_id": original_part_id,
                "status": txn.status,
                "archived_at": txn.archived_at,
                "fingerprint": txn.fingerprint,
            }
        )
    return rows


def _import_batch_rows(person):
    rows = []
    batches = ImportBatch.objects.visible_to(person).select_related("imported_by__user").order_by("pk")
    for batch in batches:
        rows.append(
            {
                "id": batch.pk,
                "account_id": batch.account_id,
                "source": batch.source,
                "source_file_sha256": batch.source_file_sha256,
                "date_range_start": batch.date_range_start,
                "date_range_end": batch.date_range_end,
                "status": batch.status,
                "archived_at": batch.archived_at,
                "imported_at": batch.imported_at,
                "imported_by_username": batch.imported_by.user.username,
            }
        )
    return rows


def _transfer_pair_rows(person):
    rows = []
    pairs = TransferPair.objects.visible_to(person).order_by("pk")
    for pair in pairs:
        rows.append(
            {
                "id": pair.pk,
                "leg_a_id": pair.leg_a_id,
                "leg_b_id": pair.leg_b_id,
                "status": pair.status,
                "kind": pair.kind,
                "confidence": pair.confidence,
                "reasons": list(pair.reasons),
                "created_at": pair.created_at,
            }
        )
    return rows


def _split_rows(person):
    rows = []
    parts = (
        TransactionSplit.objects.filter(transaction__in=Transaction.objects.visible_to(person))
        .select_related("category")
        .order_by("transaction_id", "position", "pk")
    )
    for part in parts:
        rows.append(
            {
                "id": part.pk,
                "transaction_id": part.transaction_id,
                "position": part.position,
                "category_id": part.category_id,
                "category_name": part.category.name,
                "amount_minor": part.amount_minor,
                "amount_decimal": money_decimal(part.amount_minor),
            }
        )
    return rows


def _recurring_series_rows(person):
    rows = []
    series_qs = RecurringSeries.objects.visible_to(person).prefetch_related("members").order_by("pk")
    for series in series_qs:
        member_ids = sorted(member.transaction_id for member in series.members.all())
        rows.append(
            {
                "id": series.pk,
                "merchant_key": series.merchant_key,
                "display_name": series.display_name,
                "cadence": series.cadence,
                "typical_amount_minor": series.typical_amount_minor,
                "typical_amount_decimal": money_decimal(series.typical_amount_minor),
                "currency": series.currency,
                "status": series.status,
                "confidence": series.confidence,
                "is_active": series.is_active,
                "member_transaction_ids": member_ids,
                "fingerprint": series.fingerprint,
            }
        )
    return rows


def _privacy_acceptance_rows(person):
    rows = []
    query = (
        PrivacyPolicyAcceptance.objects.filter(person=person)
        .select_related("policy_version")
        .order_by("pk")
    )
    for row in query:
        rows.append(
            {
                "id": row.pk,
                "policy_version": row.policy_version.version,
                "is_material": row.policy_version.is_material,
                "accepted_at": row.accepted_at,
            }
        )
    return rows


def _ai_connection_rows(person):
    rows = []
    query = AiProviderConnection.objects.owned_by(person).order_by("pk")
    for row in query:
        rows.append(
            {
                "id": row.pk,
                "kind": row.kind,
                "base_url": row.base_url,
                "chat_backend": row.chat_backend,
                "background_backend": row.background_backend,
                "connected_at": row.connected_at,
            }
        )
    return rows


def _ai_job_rows(person):
    rows = []
    for row in AiJob.objects.visible_to(person).order_by("pk"):
        rows.append(
            {
                "id": row.pk,
                "feature": row.feature,
                "backend": row.backend,
                "status": row.status,
                "attempts": row.attempts,
                "input_refs": row.input_refs,
                "result_ref": row.result_ref,
                "failure_code": row.failure_code,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
        )
    return rows


def _ai_usage_rows(person):
    rows = []
    for row in AiUsageEvent.objects.visible_to(person).order_by("pk"):
        rows.append(
            {
                "id": row.pk,
                "provider": row.provider,
                "backend": row.backend,
                "feature": row.feature,
                "prompt_tokens": row.prompt_tokens,
                "completion_tokens": row.completion_tokens,
                "outcome": row.outcome,
                "created_at": row.created_at,
            }
        )
    return rows


def _balance_snapshot_model():
    try:
        return apps.get_model("finance", "BalanceSnapshot")
    except LookupError:
        return None


def collect_export_tables(person) -> dict[str, list[dict]]:
    visible_txn_ids = set(Transaction.objects.visible_to(person).values_list("pk", flat=True))
    tables = {
        "accounts": _account_rows(person),
        "categories": _category_rows(person),
        "transactions": _transaction_rows(person, visible_txn_ids),
        "import_batches": _import_batch_rows(person),
        "transfer_pairs": _transfer_pair_rows(person),
        "transaction_splits": _split_rows(person),
        "recurring_series": _recurring_series_rows(person),
        "privacy_policy_acceptances": _privacy_acceptance_rows(person),
        "ai_connections": _ai_connection_rows(person),
        "ai_jobs": _ai_job_rows(person),
        "ai_usage": _ai_usage_rows(person),
    }
    snapshot_model = _balance_snapshot_model()
    if snapshot_model is not None:
        tables["balance_snapshots"] = _snapshot_rows(person, snapshot_model)
    return tables


def _snapshot_rows(person, model):
    visible_accounts = Account.objects.visible_to(person).values("pk")
    query = model.objects.filter(account_id__in=visible_accounts).order_by("pk")
    rows = []
    for snap in query:
        minor = snap.amount_minor
        contribution_minor = getattr(snap, "net_contribution_minor", None)
        rows.append(
            {
                "id": snap.pk,
                "account_id": snap.account_id,
                "snapshot_date": snap.snapshot_date,
                "amount_minor": minor,
                "amount_decimal": money_decimal(minor),
                "currency": snap.currency,
                "source": getattr(snap, "source", ""),
                "note": getattr(snap, "note", ""),
                "net_contribution_minor": contribution_minor,
                "net_contribution_decimal": money_decimal(contribution_minor) if contribution_minor is not None else None,
            }
        )
    return rows


def write_export_zip(person) -> bytes:
    tables = collect_export_tables(person)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README_TEXT)
        for name, rows in tables.items():
            csv_buf = io.StringIO()
            _write_csv(csv_buf, rows, CSV_FIELDS[name])
            archive.writestr(f"{name}.csv", csv_buf.getvalue())
            json_buf = io.StringIO()
            _write_json(json_buf, rows)
            archive.writestr(f"{name}.json", json_buf.getvalue())
    return buffer.getvalue()
