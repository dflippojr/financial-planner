from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0008_simplefin_connections"),
    ]

    operations = [
        migrations.AddField(
            model_name="importbatch",
            name="simplefin_account_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
