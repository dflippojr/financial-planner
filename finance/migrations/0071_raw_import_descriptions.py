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


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0070_alert_source_accounts"),
    ]

    operations = [
        migrations.RunPython(strip_import_formula_escape, migrations.RunPython.noop),
    ]
