from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0029_monthly_review"),
    ]

    operations = [
        migrations.AddField(
            model_name="alertsettings",
            name="monthly_review_ai_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="monthlyreview",
            name="ai_backend",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="monthlyreview",
            name="ai_paragraph",
            field=models.TextField(blank=True, default=""),
        ),
    ]
