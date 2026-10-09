import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def reevaluate(apps, schema_editor):
    pairs = apps.get_model("finance", "TransferPair")
    if not pairs.objects.filter(status="auto_marked").exists():
        return
    # Uses the live services so the downgrade follows the same undo path as a manual undo.
    from finance.category_services import downgrade_unevidenced_auto_marked_pairs

    examined, downgraded = downgrade_unevidenced_auto_marked_pairs()
    logger.info("Pair evidence re-evaluation: examined %d auto-marked, downgraded %d.", examined, downgraded)
    print(f"  pair evidence re-evaluation: examined {examined}, downgraded {downgraded}")


class Migration(migrations.Migration):
    dependencies = [("finance", "0059_audit_operations")]
    operations = [migrations.RunPython(reevaluate, migrations.RunPython.noop)]
