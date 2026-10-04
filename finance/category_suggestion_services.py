"""Background AI category suggestions for uncategorized transactions."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .ai_jobs import enqueue_job
from .ai_services import member_has_ai, resolve_ai, run_structured
from .ai_tools import visible_accounts
from .ai_types import ProviderResult
from .lifecycle_services import lock_actor_household
from .category_services import (
    _DENIED,
    _person_for,
    assign_category,
    assignable_categories,
    exclusion_exists_for,
)
from .models import AiJob, Category, CategoryRule, CategorySuggestion, Transaction

FEATURE = "category_suggestions"
BATCH_SIZE = 40
MIN_RULE_ACCEPTS = 3
MIN_RULE_CONTAINS = 4
BACKEND_LABELS = {
    "local": "Local model",
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
}


def snapshot_hash(txn):
    payload = f"{txn.transaction_date.isoformat()}|{txn.amount_minor}|{txn.description}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def suggestion_is_current(suggestion, txn):
    return suggestion.snapshot_hash == snapshot_hash(txn)


def backend_label(backend):
    return BACKEND_LABELS.get(backend, backend or "Unknown backend")


def suggestion_label(suggestion):
    kind = "Agent Harness" if suggestion.provider == "agent_harness" else suggestion.provider
    return f"AI · {kind} · {backend_label(suggestion.backend)}"


def eligible_uncategorized(person, transaction_ids=None):
    query = (
        Transaction.objects.visible_to(person)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            account_id__in=visible_accounts(person).values("pk"),
        )
        .filter(category_source=Transaction.CategorySource.UNSET)
        .filter(category__isnull=True)
        .annotate(_excluded=exclusion_exists_for(person))
        .filter(_excluded=False)
    )
    if transaction_ids is not None:
        query = query.filter(pk__in=list(transaction_ids))
    covered = _current_suggestion_txn_ids(person)
    if covered:
        query = query.exclude(pk__in=covered)
    return query.order_by("pk")


def queue_category_suggestions_for(principal, transactions):
    person = _person_for(principal)
    if not member_has_ai(person):
        return []
    ids = list(eligible_uncategorized(person, [item.pk for item in transactions]).values_list("pk", flat=True))
    return _enqueue_ids(person, ids)


def queue_remaining_uncategorized(principal):
    person = _person_for(principal)
    if not member_has_ai(person):
        return []
    ids = list(eligible_uncategorized(person).values_list("pk", flat=True))
    return _enqueue_ids(person, ids)


def suggestions_pending(person):
    if person is None or not member_has_ai(person):
        return False
    return AiJob.objects.filter(
        member=person,
        feature=FEATURE,
        status__in=(AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL, AiJob.Status.RUNNING),
    ).exists()


def pending_suggestions_for(person, transactions):
    if person is None:
        return {}
    ids = [txn.pk for txn in transactions]
    rows = (
        CategorySuggestion.objects.visible_to(person)
        .filter(status=CategorySuggestion.Status.PENDING, transaction_id__in=ids)
        .select_related("category")
    )
    by_id = {}
    txns = {txn.pk: txn for txn in transactions}
    for row in rows:
        txn = txns.get(row.transaction_id)
        if txn is None or not suggestion_is_current(row, txn):
            continue
        if txn.category_id is not None or txn.category_source != Transaction.CategorySource.UNSET:
            continue
        by_id[row.transaction_id] = row
    return by_id


def proposed_rule_from_accepts(person):
    if person is None:
        return None
    accepted = list(
        CategorySuggestion.objects.visible_to(person)
        .filter(status=CategorySuggestion.Status.ACCEPTED)
        .select_related("category", "transaction")
        .order_by("-resolved_at", "-pk")[:40]
    )
    by_category = {}
    for row in accepted:
        by_category.setdefault(row.category_id, []).append(row)
    best = None
    for category_id, rows in by_category.items():
        if len(rows) < MIN_RULE_ACCEPTS:
            continue
        category = rows[0].category
        if category.code in {Category.Code.TRANSFER, Category.Code.UNCATEGORIZED}:
            continue
        contains = shared_description_contains([row.transaction.description for row in rows])
        if not contains:
            continue
        if CategoryRule.objects.visible_to(person).filter(
            description_contains=contains,
            category_id=category_id,
        ).exists():
            continue
        candidate = {
            "description_contains": contains,
            "category_id": category_id,
            "category_name": category.name,
            "count": len(rows),
            "url": reverse("category-rule-list")
            + "?"
            + urlencode({"description_contains": contains, "category": category_id}),
        }
        if best is None or candidate["count"] > best["count"]:
            best = candidate
    return best


@transaction.atomic
def accept_suggestion(principal, suggestion_id):
    person, suggestion, txn = _locked_pending(principal, suggestion_id)
    # Lock in the codebase's order (household, then transaction) and re-read,
    # so a category set by someone else since the first read is never overwritten.
    lock_actor_household(person)
    txn = Transaction.objects.select_for_update().filter(pk=txn.pk).first()
    suggestion = CategorySuggestion.objects.select_for_update().filter(pk=suggestion.pk).first()
    if (
        txn is None
        or suggestion is None
        or suggestion.status != CategorySuggestion.Status.PENDING
        or not suggestion_is_current(suggestion, txn)
    ):
        raise PermissionDenied(_DENIED)
    if not _may_apply(person, txn):
        suggestion.status = CategorySuggestion.Status.REJECTED
        suggestion.resolved_at = timezone.now()
        suggestion.save(update_fields=("status", "resolved_at", "updated_at"))
        return suggestion
    assign_category(person, txn.pk, suggestion.category_id)
    suggestion.status = CategorySuggestion.Status.ACCEPTED
    suggestion.resolved_at = timezone.now()
    suggestion.save(update_fields=("status", "resolved_at", "updated_at"))
    return suggestion


def reject_suggestion(principal, suggestion_id):
    _person, suggestion, _txn = _locked_pending(principal, suggestion_id)
    now = timezone.now()
    # Conditional on still pending, so a concurrent accept is never overwritten.
    changed = CategorySuggestion.objects.filter(
        pk=suggestion.pk, status=CategorySuggestion.Status.PENDING
    ).update(status=CategorySuggestion.Status.REJECTED, resolved_at=now, updated_at=now)
    if not changed:
        raise PermissionDenied(_DENIED)
    suggestion.refresh_from_db()
    return suggestion


def accept_suggestions(principal, suggestion_ids):
    accepted = []
    for suggestion_id in suggestion_ids:
        try:
            accepted.append(accept_suggestion(principal, suggestion_id))
        except PermissionDenied:
            continue
    return accepted


def run_category_suggestion_job(person, job, *, backend, session_id="", on_session=None):
    ids = _ids_from_refs(job.input_refs)
    txns = list(eligible_uncategorized(person, ids).select_related("account"))
    if not txns:
        return ProviderResult(ok=True, answer="", session_id="skipped")
    categories = list(_suggestable_categories(person))
    if not categories:
        return ProviderResult(ok=True, answer="", session_id="skipped")
    if session_id:
        # Resuming: the answer was generated from the transactions as they were then.
        sent = (job.input_refs or {}).get("snapshots") or {}
    else:
        sent = {str(txn.pk): snapshot_hash(txn) for txn in txns}
        job.input_refs = {**(job.input_refs or {}), "snapshots": sent}
        job.save(update_fields=("input_refs", "updated_at"))
    prompt = _build_prompt(txns, categories)
    result = run_structured(
        person,
        prompt,
        feature=FEATURE,
        backend=backend,
        session_id=session_id,
        on_session=on_session,
    )
    if not result.ok:
        return result
    allowed = {item.pk for item in categories}
    by_id = {txn.pk: txn for txn in txns}
    connection, resolved_backend = resolve_ai(person, use_chat=False, requested_backend=backend)
    provider = connection.kind if connection else "agent_harness"
    chosen = backend or resolved_backend
    for txn_id, category_id in _parse_suggestions(result.answer):
        txn = by_id.get(txn_id)
        if txn is None or category_id not in allowed:
            continue
        if sent.get(str(txn.pk)) != snapshot_hash(txn):
            continue
        _store_suggestion(person, txn, category_id, provider=provider, backend=chosen)
    return result


def shared_description_contains(descriptions):
    texts = [re.sub(r"\s+", " ", item or "").strip() for item in descriptions]
    texts = [item for item in texts if item]
    if len(texts) < MIN_RULE_ACCEPTS:
        return None
    shortest = min(texts, key=len)
    best = ""
    for start in range(len(shortest)):
        for end in range(start + MIN_RULE_CONTAINS, len(shortest) + 1):
            piece = shortest[start:end].strip()
            if len(piece) < MIN_RULE_CONTAINS:
                continue
            folded = piece.casefold()
            if all(folded in item.casefold() for item in texts) and len(piece) > len(best):
                best = piece
    return best or None


def _enqueue_ids(person, ids):
    if not ids:
        return []
    in_flight = list(
        AiJob.objects.filter(
            member=person,
            feature=FEATURE,
            status__in=(AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL, AiJob.Status.RUNNING),
        ).order_by("pk")
    )
    already = set()
    for job in in_flight:
        already.update(_ids_from_refs(job.input_refs))
    remaining = [pk for pk in dict.fromkeys(ids) if pk not in already]
    if not remaining:
        return []
    jobs = []
    for job in in_flight:
        if job.status != AiJob.Status.QUEUED:
            continue
        remaining, topped = _top_up_queued_job(job.pk, remaining)
        if topped is not None:
            jobs.append(topped)
        if not remaining:
            return jobs
    for chunk in _chunks(remaining, BATCH_SIZE):
        jobs.append(enqueue_job(person, feature=FEATURE, input_refs={"transaction_ids": list(chunk)}))
    return jobs


def _top_up_queued_job(job_pk, remaining):
    """Add ids to a job only while it is still queued, so the runner never misses them."""
    with transaction.atomic():
        job = (
            AiJob.objects.select_for_update()
            .filter(pk=job_pk, status=AiJob.Status.QUEUED, harness_session_id="")
            .first()
        )
        if job is None:
            # Claimed, or a retry that will resume a session whose prompt is fixed.
            return remaining, None
        current = _ids_from_refs(job.input_refs)
        room = BATCH_SIZE - len(current)
        if room <= 0:
            return remaining, None
        added, rest = remaining[:room], remaining[room:]
        job.input_refs = {**(job.input_refs or {}), "transaction_ids": current + added}
        job.save(update_fields=("input_refs", "updated_at"))
        return rest, job


def _current_suggestion_txn_ids(person):
    ids = []
    rows = CategorySuggestion.objects.filter(member=person).select_related("transaction")
    for row in rows:
        if suggestion_is_current(row, row.transaction):
            ids.append(row.transaction_id)
    return ids


def _suggestable_categories(person):
    return assignable_categories(person).exclude(code=Category.Code.UNCATEGORIZED)


def _build_prompt(txns, categories):
    category_lines = [f"- id={item.pk} name={item.name}" for item in categories]
    txn_lines = [
        (
            f"- id={txn.pk} date={txn.transaction_date.isoformat()} "
            f"amount_minor={txn.amount_minor} currency={txn.currency} "
            f"description={txn.description}"
        )
        for txn in txns
    ]
    return (
        "Suggest a household category for each uncategorized transaction.\n"
        "Reply with JSON only, no markdown: "
        '{"suggestions":[{"transaction_id":1,"category_id":2}]}.\n'
        'Use an existing category id from the list, or "unsure". '
        "Do not invent categories or ids.\n"
        "Categories:\n"
        + "\n".join(category_lines)
        + "\nTransactions:\n"
        + "\n".join(txn_lines)
    )


def _parse_suggestions(answer):
    payload = _extract_json(answer)
    if payload is None:
        return []
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("suggestions")
    if not isinstance(rows, list):
        return []
    parsed = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        txn_id = _as_int(item.get("transaction_id"))
        raw_category = item.get("category_id")
        if txn_id is None:
            continue
        if isinstance(raw_category, str) and raw_category.strip().casefold() == "unsure":
            continue
        category_id = _as_int(raw_category)
        if category_id is None:
            continue
        parsed.append((txn_id, category_id))
    return parsed


def _extract_json(answer):
    text = (answer or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _store_suggestion(person, txn, category_id, *, provider, backend):
    digest = snapshot_hash(txn)
    existing = CategorySuggestion.objects.filter(member=person, transaction=txn).first()
    if existing is not None and existing.snapshot_hash == digest:
        return existing
    defaults = {
        "category_id": category_id,
        "provider": provider,
        "backend": backend,
        "status": CategorySuggestion.Status.PENDING,
        "snapshot_hash": digest,
        "resolved_at": None,
    }
    if existing is None:
        return CategorySuggestion.objects.create(member=person, transaction=txn, **defaults)
    for key, value in defaults.items():
        setattr(existing, key, value)
    existing.save()
    return existing


@transaction.atomic
def _locked_pending(principal, suggestion_id):
    person = _person_for(principal)
    suggestion = (
        CategorySuggestion.objects.visible_to(person)
        .select_related("transaction", "category")
        .filter(pk=suggestion_id, status=CategorySuggestion.Status.PENDING)
        .first()
    )
    if suggestion is None:
        raise PermissionDenied(_DENIED)
    txn = suggestion.transaction
    if not suggestion_is_current(suggestion, txn):
        raise PermissionDenied(_DENIED)
    return person, suggestion, txn


def _may_apply(person, txn):
    if txn.category_source in (
        Transaction.CategorySource.MANUAL,
        Transaction.CategorySource.RULE,
        Transaction.CategorySource.INHERITED,
        Transaction.CategorySource.SPLIT,
    ):
        return False
    if txn.category_id is not None:
        return False
    if txn.kind != Transaction.Kind.CASH_FLOW:
        return False
    if not Transaction.objects.visible_to(person).filter(pk=txn.pk, status=Transaction.Status.ACTIVE).exists():
        return False
    excluded = (
        Transaction.objects.filter(pk=txn.pk)
        .annotate(_excluded=exclusion_exists_for(person))
        .values_list("_excluded", flat=True)
        .first()
    )
    return not excluded


def _ids_from_refs(refs):
    if not isinstance(refs, dict):
        return []
    raw = refs.get("transaction_ids") or []
    ids = []
    for item in raw:
        parsed = _as_int(item)
        if parsed is not None:
            ids.append(parsed)
    return ids


def _chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]
