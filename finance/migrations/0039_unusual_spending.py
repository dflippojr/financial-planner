from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0038_shared_local_model"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="alert",
            name="alert_kind_valid",
        ),
        migrations.AlterField(
            model_name="alert",
            name="kind",
            field=models.CharField(
                choices=[
                    ("sync", "Sync"),
                    ("recurring_price", "Recurring price change"),
                    ("recurring_missed", "Missed recurring charge"),
                    ("budget", "Budget"),
                    ("large_transaction", "Large transaction"),
                    ("monthly_review", "Monthly review"),
                    ("unusual_spending", "Unusual spending"),
                    ("backup", "Backup"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="alert",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    kind__in=(
                        "sync",
                        "recurring_price",
                        "recurring_missed",
                        "budget",
                        "large_transaction",
                        "monthly_review",
                        "unusual_spending",
                        "backup",
                    )
                ),
                name="alert_kind_valid",
            ),
        ),
        migrations.AddField(
            model_name="alertsettings",
            name="unusual_spending_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="alertsettings",
            name="unusual_spending_ai_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="alertsettings",
            name="unusual_category_percent",
            field=models.PositiveIntegerField(default=50),
        ),
        migrations.AddField(
            model_name="alertsettings",
            name="unusual_category_floor_minor",
            field=models.BigIntegerField(default=5000),
        ),
        migrations.AddConstraint(
            model_name="alertsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(unusual_category_percent__gte=1, unusual_category_percent__lte=1000),
                name="alert_settings_unusual_percent_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="alertsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(unusual_category_floor_minor__gte=0),
                name="alert_settings_unusual_floor_non_negative",
            ),
        ),
        migrations.AddField(
            model_name="monthlyreview",
            name="unusual_ai_backend",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="monthlyreview",
            name="unusual_ai_paragraph",
            field=models.TextField(blank=True, default=""),
        ),
    ]
