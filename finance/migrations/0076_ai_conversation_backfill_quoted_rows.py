"""Record what conversations from before 0075 may have quoted, failing closed (#569).

Those conversations recorded no planned items or budgets, and their tool output
was never stored, so what they quoted cannot be read back. Each one that ran a
tool is treated as having quoted every planned item and budget its member could
have seen while it was open:

- the household rows of every household the member belonged to since it began;
- any row of a member who shared one of those households with them since it
  began and changed since then, since a row that is private now may have been
  a household row then;
- the budget each of its set-budget proposals named.

A conversation that now records a row its member can no longer see is deleted,
as the purge would have done had the row been recorded. Batches commit one at a
time, so the conversation table is never locked for the whole backfill.
"""

from django.db import migrations, transaction
from django.db.models import Q

BATCH = 500
QUOTED = (("used_planned_item_ids", "PlannedItem"), ("used_budget_ids", "Budget"))


def _visible(row, member_id, current):
    if row.scope == "private":
        return row.owner_id == member_id
    return row.scope == "household" and row.household_id in current


def _backfill_batch(apps, conversations):
    Membership = apps.get_model("finance", "Membership")
    AiProposal = apps.get_model("finance", "AiProposal")
    changed, doomed = [], []
    for conversation in conversations:
        memberships = list(Membership.objects.filter(person_id=conversation.member_id))
        current = {row.household_id for row in memberships if row.ended_at is None}
        seen = {row.household_id for row in memberships
                if row.ended_at is None or row.ended_at >= conversation.created_at}
        others = Membership.objects.filter(
            Q(ended_at__isnull=True) | Q(ended_at__gte=conversation.created_at),
            household_id__in=seen, person__isnull=False,
        ).exclude(person_id=conversation.member_id).values("person_id")
        could_see = Q(scope="household", household_id__in=seen) | Q(
            owner_id__in=others, updated_at__gte=conversation.created_at
        )
        named = {
            "used_planned_item_ids": set(),
            "used_budget_ids": {
                payload.get("budget_id")
                for payload in AiProposal.objects.filter(conversation_id=conversation.pk, kind="set_budget")
                .values_list("payload", flat=True)
            } - {None},
        }
        hidden = False
        for field, model_name in QUOTED:
            recorded = set(getattr(conversation, field) or [])
            rows = list(apps.get_model("finance", model_name).objects.filter(
                could_see | Q(pk__in=recorded | named[field])
            ).only("scope", "owner", "household"))
            hidden = hidden or any(not _visible(row, conversation.member_id, current) for row in rows)
            setattr(conversation, field, sorted(recorded | {row.pk for row in rows}))
        if hidden:
            doomed.append(conversation.pk)
        else:
            changed.append(conversation)
    AiConversation = apps.get_model("finance", "AiConversation")
    AiConversation.objects.bulk_update(changed, [field for field, _model in QUOTED])
    AiConversation.objects.filter(pk__in=doomed).delete()


def backfill_quoted_rows(apps, schema_editor):
    AiConversation = apps.get_model("finance", "AiConversation")
    last = 0
    while True:
        with transaction.atomic():
            batch = list(
                AiConversation.objects.select_for_update()
                .filter(pk__gt=last, tool_call_count__gt=0)
                .order_by("pk")[:BATCH]
            )
            if not batch:
                return
            last = batch[-1].pk
            _backfill_batch(apps, batch)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('finance', '0075_ai_conversation_used_planned_items_and_budgets'),
    ]

    operations = [
        # Reversing keeps the recorded ids: 0075's reverse drops both fields.
        migrations.RunPython(backfill_quoted_rows, migrations.RunPython.noop),
    ]
