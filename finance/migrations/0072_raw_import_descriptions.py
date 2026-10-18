from django.db import migrations

FORMULA_STARTS = ("=", "+", "-", "@")
BATCH_SIZE = 500


def _source_texts(original_fields):
    """Every description an import could have built from these source cells.

    A description comes from one cell as is, or from a payee and a memo cell,
    each stripped, joined with " - " (or either one alone).
    """
    cells = [value for value in (original_fields or {}).values() if isinstance(value, str)]
    stripped = [value.strip() for value in cells]
    texts = set(cells) | set(stripped)
    texts.update(f"{payee} - {memo}" for payee in stripped for memo in stripped if payee and memo)
    return texts


def strip_import_formula_escape(apps, schema_editor):
    """Remove the apostrophe file imports added before formula-like text (#503).

    Only file-import rows carry it: SimpleFIN and manual entries were always
    stored raw. Fingerprints keep the escaped form, which reimports still hash.
    A row is changed only when its source cells produce the unescaped text
    and not the stored one.
    """
    Transaction = apps.get_model("finance", "Transaction")
    rows = (
        Transaction.objects.filter(description__startswith="'")
        .exclude(import_batch__source__in=("simplefin", "manual"))
        .only("pk", "description", "original_fields")
    )
    changed = []
    for row in rows.iterator(chunk_size=BATCH_SIZE):
        raw = row.description[1:]
        if not raw.lstrip().startswith(FORMULA_STARTS):
            continue
        # Strip only what the import clearly added: the rest must be what the
        # source cells produce, and the stored text must not be. Member edits,
        # literal apostrophes and rows that could be either stay as they are.
        texts = _source_texts(row.original_fields)
        if raw not in texts or row.description in texts:
            continue
        row.description = raw
        changed.append(row)
        if len(changed) == BATCH_SIZE:
            Transaction.objects.bulk_update(changed, ["description"])
            changed = []
    Transaction.objects.bulk_update(changed, ["description"])


def strip_rule_formula_escape(apps, schema_editor):
    """Drop the import apostrophe from rules copied from escaped descriptions (#503).

    "Create rule" prefilled a rule's text from the stored description, so a
    rule may read "'-SYNTHETIC ACH". Rules match by case-folded containment,
    and every description containing "'-X" also contains "-X", so the stripped
    rule still matches every row it matched before and now matches the raw
    descriptions too. The rule stays confirmed: the member chose this match.
    """
    CategoryRule = apps.get_model("finance", "CategoryRule")
    changed = []
    for rule in CategoryRule.objects.filter(description_contains__startswith="'").only("pk", "description_contains"):
        raw = rule.description_contains[1:]
        if raw.strip() and raw.lstrip().startswith(FORMULA_STARTS):
            rule.description_contains = raw
            changed.append(rule)
    CategoryRule.objects.bulk_update(changed, ["description_contains"], batch_size=BATCH_SIZE)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0071_recurring_price_acknowledgements"),
    ]

    operations = [
        migrations.RunPython(strip_import_formula_escape, migrations.RunPython.noop),
        migrations.RunPython(strip_rule_formula_escape, migrations.RunPython.noop),
    ]
