from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0005_recurring_charges"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringseries",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
