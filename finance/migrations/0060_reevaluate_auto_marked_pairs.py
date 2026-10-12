import logging
import re

from django.conf import settings
from django.db import migrations

logger = logging.getLogger(__name__)

# Freeze the 0060 evidence rule: migrations must not import live models/services.
_PAYMENT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:payment|payments|pmt|pymt|autopay|epay|e-payment|"
    r"transfer|transfers|xfer|ach|online banking|crcardpmt)(?![a-z0-9])",
    re.IGNORECASE,
)


def _evidence(left, right):
    types = {left.account.account_type, right.account.account_type}
    involves_card = "credit_card" in types and len(types) == 2
    card = left if left.account.account_type == "credit_card" else right
    payer = right if card is left else left
    card_payment = involves_card and payer.amount_minor < 0 and card.amount_minor > 0
    direction = not involves_card or card_payment
    wording = bool(_PAYMENT_PATTERN.search(left.description or "") or _PAYMENT_PATTERN.search(right.description or ""))
    return direction, wording, "card_payment" if card_payment else "transfer"


def _window_days(apps, alias, left, right):
    for account in (left.account, right.account):
        if account.scope == "household" and account.household_id:
            return account.household.transfer_match_window_days
    membership = (
        apps.get_model("finance", "Membership").objects.using(alias)
        .filter(person_id=left.account.owner_id, ended_at__isnull=True)
        .select_related("household")
        .first()
    )
    return membership.household.transfer_match_window_days if membership else settings.TRANSFER_MATCH_WINDOW_DAYS


def _record_history(apps, alias, leg, field, previous, new):
    if previous != new:
        apps.get_model("finance", "TransactionCorrectionHistory").objects.using(alias).create(
            transaction_id=leg.pk,
            actor_id=None,
            field_name=field,
            previous_description=previous,
            new_description=new,
        )


def _restore_category(apps, alias, leg, category, *, inherited=False):
    if not inherited and leg.category_id == (category.pk if category else None):
        return
    previous = leg.category.name if leg.category else "Uncategorized"
    leg.category = category
    fields = ["category", "updated_at"]
    if inherited:
        leg.category_source = "inherited"
        fields.append("category_source")
    leg.save(using=alias, update_fields=fields)
    _record_history(apps, alias, leg, "category", previous, category.name if category else "Uncategorized")


def _restore_refunds(apps, alias, leg):
    category = leg.category
    refunds = (
        apps.get_model("finance", "Transaction").objects.using(alias)
        .select_for_update(of=("self",)).select_related("category")
        .filter(refund_link__original_id=leg.pk, status="active")
        .exclude(category_id=leg.category_id)
        .order_by("pk")
    )
    for refund in refunds:
        _restore_category(apps, alias, refund, category, inherited=True)


def reevaluate(apps, schema_editor):
    # RunPython is atomic on both supported engines; every query uses its connection.
    alias = schema_editor.connection.alias
    pairs = apps.get_model("finance", "TransferPair").objects.using(alias)
    pair_ids = list(pairs.filter(status="auto_marked").order_by("pk").values_list("pk", flat=True))
    if not pair_ids:
        return
    transactions = apps.get_model("finance", "Transaction").objects.using(alias)
    examined, downgraded = len(pair_ids), 0
    for pair_id in pair_ids:
        pair = pairs.select_for_update(of=("self",)).filter(pk=pair_id).first()
        if pair is None or pair.status != "auto_marked":
            continue
        legs = {
            leg.pk: leg for leg in transactions.select_for_update(of=("self",))
            .select_related("account__household", "category")
            .filter(pk__in=(pair.leg_a_id, pair.leg_b_id)).order_by("pk")
        }
        left, right = legs[pair.leg_a_id], legs[pair.leg_b_id]
        if left.status != "active" or right.status != "active":
            continue
        direction, wording, kind = _evidence(left, right)
        if direction and wording:
            continue
        categories = apps.get_model("finance", "Category").objects.using(alias)
        for leg, category_id in ((left, pair.leg_a_category_id_at_mark), (right, pair.leg_b_category_id_at_mark)):
            category = categories.get(pk=category_id) if category_id is not None else None
            _restore_category(apps, alias, leg, category)
        for leg in (left, right):
            _restore_refunds(apps, alias, leg)
        for leg in (left, right):
            _record_history(apps, alias, leg, "exclusion", "excluded", "included")
        pair.status = "suggested"
        pair.confidence = "low"
        pair.kind = kind
        pair.reasons = [
            "exact opposite amounts",
            f"dates within {_window_days(apps, alias, left, right)} days",
            "both accounts visible to the same person",
            "only candidate for both legs in the window",
            "money moves in the expected direction" if direction else
            "card pair runs the wrong way (bank credit with card debit), so it needs confirmation",
            "payment or transfer wording on a leg" if wording else
            "no payment or transfer wording on either leg, so it needs confirmation",
        ]
        pair.save(using=alias, update_fields=("status", "confidence", "kind", "reasons", "updated_at"))
        downgraded += 1
    logger.info("Pair evidence re-evaluation: examined %d auto-marked, downgraded %d.", examined, downgraded)
    print(f"  pair evidence re-evaluation: examined {examined}, downgraded {downgraded}")


class Migration(migrations.Migration):
    dependencies = [("finance", "0059_audit_operations")]
    operations = [migrations.RunPython(reevaluate, migrations.RunPython.noop)]
