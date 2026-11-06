"""Automatic draw-down of a sinking fund when its planned expense posts (#670).

A transaction pays an occurrence of the fund's linked planned item when it:
- is an active cash-flow outflow on an active account the fund may read: a
  household fund reads only its household's accounts, a private fund only the
  accounts its owner can see; it is not a transfer leg or an untracked transfer;
- is dated on or after the day the item was linked, and within the cadence
  tolerance recurring detection uses (3 days weekly to monthly, 4 quarterly,
  5 annual and one-time) of one of the item's occurrences;
- is within 25% of the item's amount, the recurring amount band;
- and shares a sign of identity with the item: the item's category, the
  merchant of the recurring series the item replaces, or the item's name
  inside the description (compared as merchant keys).

Each occurrence of the linked item draws at most once (a fund relinked to
another item starts afresh) and each transaction at most once ever. The
draw-down is the transaction's amount, capped at what the fund holds, so a fund
never goes below zero. A draw-down whose transaction is later archived (an
undone import) stops counting; a re-imported copy can then draw the occurrence.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .access import visible_rows
from .audit_services import append_event, owned_audience
from .category_services import exclusion_exists_for
from .funds import assigned_minor, linked_item
from .models import (
    Account,
    AuditEvent,
    FundEntry,
    Membership,
    PlannedItem,
    SavingsGoal,
    Transaction,
)
from .projection import occurrence_dates
from .recurring_services import _in_confirmed_amount_band, cadence_tolerance_days, merchant_key

logger = logging.getLogger(__name__)
ONE_TIME_TOLERANCE_CADENCE = "annual"


def occurrence_tolerance_days(cadence):
    """Days either side of an occurrence a payment may post: the recurring cadence tolerance."""
    if cadence == PlannedItem.Cadence.ONE_TIME:
        cadence = ONE_TIME_TOLERANCE_CADENCE
    return cadence_tolerance_days(cadence)


def _reader(goal):
    """The member whose view judges transfer pairs, and whether only household legs count."""
    if goal.scope == SavingsGoal.Scope.HOUSEHOLD:
        membership = (
            Membership.objects.filter(household_id=goal.household_id, ended_at__isnull=True, person__isnull=False)
            .select_related("person")
            .order_by("joined_at", "pk")
            .first()
        )
        return (membership.person if membership else None), True
    return goal.owner, False


def _candidate_transactions(goal, reader, household_only):
    if household_only:
        accounts = Account.objects.filter(scope=Account.Scope.HOUSEHOLD, household_id=goal.household_id)
    else:
        accounts = visible_rows(Account, reader)
    return (
        Transaction.objects.filter(
            account__in=accounts,
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            account__status=Account.Status.ACTIVE,
            account__archived_at__isnull=True,
            amount_minor__lt=0,
        )
        .annotate(_excluded=exclusion_exists_for(reader, household_only=household_only))
        .filter(_excluded=False)
        .exclude(Exists(FundEntry.objects.filter(transaction_id=OuterRef("pk"))))
    )


def _identity_matches(item, txn, *, name_key, series_key):
    if item.category_id is not None and txn.category_id == item.category_id:
        return True
    description_key = f" {merchant_key(txn.description)} "
    if series_key and description_key.strip() == series_key:
        return True
    return bool(name_key) and f" {name_key} " in description_key


def _taken_occurrences(goal, item):
    """The item's occurrence dates that already hold a counted draw-down from this fund."""
    return set(
        FundEntry.objects.filter(goal=goal, planned_item=item, kind=FundEntry.Kind.DRAW_DOWN)
        .filter(FundEntry.counted_filter())
        .values_list("occurrence_date", flat=True)
    )


def _retire_stale(goal, item, occurrence):
    """A live draw-down for this occurrence whose transaction was archived gives way to a new one."""
    FundEntry.objects.filter(
        goal=goal, planned_item=item, kind=FundEntry.Kind.DRAW_DOWN, occurrence_date=occurrence,
        undone_at__isnull=True
    ).filter(transaction__status=Transaction.Status.ARCHIVED).update(undone_at=timezone.now())


def _record_draw_down(goal, reader, txn):
    append_event(
        action=AuditEvent.Action.RECORD_EDITED,
        actor=None,
        actor_kind=AuditEvent.ActorKind.SCHEDULER,
        effective_member=reader,
        target_type=AuditEvent.TargetType.GOAL,
        target_id=goal.pk,
        changed_fields=("assigned",),
        metadata={"transaction_id": txn.pk},
        source=AuditEvent.Source.JOB,
        verified=True,
        **owned_audience(goal),
    )


def draw_down_goal(goal, *, today, transaction_ids=None):
    """Draw one fund down for each newly posted occurrence of its planned item; returns new entries."""
    item = linked_item(goal)
    if item is None or goal.planned_item_linked_on is None:
        return []
    reader, household_only = _reader(goal)
    if reader is None:
        return []
    tolerance = timedelta(days=occurrence_tolerance_days(item.cadence))
    earliest = goal.planned_item_linked_on
    occurrences = occurrence_dates(
        item.start_date, item.end_date, item.cadence, earliest - tolerance, today + tolerance
    )
    if not occurrences:
        return []
    candidates = _candidate_transactions(goal, reader, household_only).filter(
        transaction_date__gte=max(earliest, occurrences[0] - tolerance),
        transaction_date__lte=min(today, occurrences[-1] + tolerance),
    )
    if transaction_ids is not None:
        candidates = candidates.filter(pk__in=list(transaction_ids))
    rows = [
        txn for txn in candidates.order_by("transaction_date", "pk")
        if _in_confirmed_amount_band(item.amount_minor, txn.amount_minor)
    ]
    if not rows:
        return []
    name_key = merchant_key(item.name)
    series_key = item.replaces_series.merchant_key if item.replaces_series_id and item.replaces_series else ""
    rows = [txn for txn in rows if _identity_matches(item, txn, name_key=name_key, series_key=series_key)]
    taken = _taken_occurrences(goal, item)
    used = set()
    created = []
    for occurrence in occurrences:
        if occurrence in taken:
            continue
        window = [
            txn for txn in rows
            if txn.pk not in used and abs((txn.transaction_date - occurrence).days) <= tolerance.days
        ]
        if not window:
            continue
        txn = min(
            window,
            key=lambda row: (
                abs((row.transaction_date - occurrence).days),
                abs(abs(row.amount_minor) - item.amount_minor),
                row.pk,
            ),
        )
        held = assigned_minor(goal)
        if held <= 0:
            break
        _retire_stale(goal, item, occurrence)
        entry = FundEntry.objects.create(
            goal=goal,
            kind=FundEntry.Kind.DRAW_DOWN,
            reason=FundEntry.Reason.PLANNED,
            amount_minor=-min(abs(txn.amount_minor), held),
            entry_date=txn.transaction_date,
            transaction=txn,
            planned_item=item,
            occurrence_date=occurrence,
        )
        _record_draw_down(goal, reader, txn)
        used.add(txn.pk)
        created.append(entry)
    return created


def apply_fund_draw_downs(*, today=None, transaction_ids=None):
    """Run draw-downs for every fund with a linked planned item, each fund in its own transaction.

    `transaction_ids` narrows the search to given rows. One fund's
    failure is logged by id and error type only and the rest still run.
    """
    today = today or timezone.localdate()
    goal_ids = list(
        SavingsGoal.objects.filter(
            share_of_account=True,
            funds_planned_item__isnull=False,
            status=SavingsGoal.Status.ACTIVE,
        )
        .filter(Q(funds_planned_item__enabled=True))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    created = []
    for goal_id in goal_ids:
        try:
            with transaction.atomic():
                goal = (
                    SavingsGoal.objects.select_for_update(of=("self",))
                    .select_related("funds_planned_item__replaces_series", "owner")
                    .filter(pk=goal_id)
                    .first()
                )
                if goal is None:
                    continue
                created.extend(draw_down_goal(goal, today=today, transaction_ids=transaction_ids))
        except Exception as exc:  # One fund must not stop the others.
            from .pass_isolation import database_unavailable

            if database_unavailable(exc):
                raise
            logger.error("Fund draw-down for goal %s failed (%s).", goal_id, type(exc).__name__)
    return created
