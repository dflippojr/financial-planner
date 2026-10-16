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
    A description the source cells already produce with its apostrophe is kept.
    """
    Transaction = apps.get_model("finance", "Transaction")
    rows = (
        Transaction.objects.filter(description__startswith="'")
        .exclude(import_batch__source__in=("simplefin", "manual"))
        .only("pk", "description", "original_fields")
    )
    changed = []
    for row in rows.iterator(chunk_size=BATCH_SIZE):
        if not row.description[1:].lstrip().startswith(FORMULA_STARTS):
            continue
        if row.description in _source_texts(row.original_fields):
            continue
        row.description = row.description[1:]
        changed.append(row)
        if len(changed) == BATCH_SIZE:
            Transaction.objects.bulk_update(changed, ["description"])
            changed = []
    Transaction.objects.bulk_update(changed, ["description"])


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0070_alert_source_accounts"),
    ]

    operations = [
        migrations.RunPython(strip_import_formula_escape, migrations.RunPython.noop),
    ]
