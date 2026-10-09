import datetime
from django.db import migrations, models


def clamp_legacy_starts(apps, schema_editor):
    Budget = apps.get_model("finance", "Budget")
    Budget.objects.using(schema_editor.connection.alias).filter(
        rollover_started_month__lt=datetime.date(2000, 1, 1)
    ).update(rollover_started_month=datetime.date(2000, 1, 1))


class Migration(migrations.Migration):
    dependencies = [("finance", "0061_savings_goal_wishlist")]
    operations = [
        migrations.RunPython(clamp_legacy_starts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="budget",
            constraint=models.CheckConstraint(
                condition=models.Q(rollover_started_month__isnull=True)
                | models.Q(rollover_started_month__gte=datetime.date(2000, 1, 1)),
                name="budget_rollover_month_floor",
            ),
        ),
        migrations.AlterField(
            model_name="transaction", name="description", field=models.TextField(max_length=500),
        ),
    ]
