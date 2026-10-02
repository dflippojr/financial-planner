from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0017_ai_provider"),
    ]

    operations = [
        migrations.AddField(
            model_name="aijob",
            name="harness_session_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
