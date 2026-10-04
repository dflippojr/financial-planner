from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0035_person_sessions_valid_after"),
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
                    ("backup", "Backup"),
                    ("expected_balance", "Expected balance"),
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
                        "backup",
                        "expected_balance",
                    )
                ),
                name="alert_kind_valid",
            ),
        ),
        migrations.AddField(
            model_name="alertsettings",
            name="expected_balance_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="BillsCalendarSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("threshold_minor", models.BigIntegerField(blank=True, null=True)),
                (
                    "accounts",
                    models.ManyToManyField(blank=True, related_name="bills_calendar_settings", to="finance.account"),
                ),
                (
                    "person",
                    models.OneToOneField(
                        on_delete=models.deletion.PROTECT,
                        related_name="bills_calendar_settings",
                        to="finance.person",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="billscalendarsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("threshold_minor__isnull", True), ("threshold_minor__gte", 0), _connector="OR"),
                name="bills_calendar_threshold_non_negative",
            ),
        ),
    ]
