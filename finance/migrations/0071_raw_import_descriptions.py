from django.db import migrations

FORMULA_STARTS = ("=", "+", "-", "@")


def strip_import_formula_escape(apps, schema_editor):
    """Remove the apostrophe file imports added before formula-like text (#503).

    Only file-import rows carry it: SimpleFIN and manual entries were always
    stored raw. Fingerprints keep the escaped form, which reimports still hash.
    A description whose source cell itself began with the apostrophe is kept.
    """
    Transaction = apps.get_model("finance", "Transaction")
    rows = (
        Transaction.objects.filter(description__startswith="'")
        .exclude(import_batch__source__in=("simplefin", "manual"))
        .only("pk", "description", "original_fields")
    )
    changed = []
    for row in rows.iterator(chunk_size=500):
        source_cells = (row.original_fields or {}).values()
        if row.description[1:].lstrip().startswith(FORMULA_STARTS) and row.description not in source_cells:
            row.description = row.description[1:]
            changed.append(row)
    Transaction.objects.bulk_update(changed, ["description"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0070_alert_source_accounts"),
    ]

    operations = [
        migrations.RunPython(strip_import_formula_escape, migrations.RunPython.noop),
    ]
