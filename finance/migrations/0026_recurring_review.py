from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0025_alert"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringseries",
            name="cancelled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recurringseries",
            name="acknowledged_amount_minor",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
