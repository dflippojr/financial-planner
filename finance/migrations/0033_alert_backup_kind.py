from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0032_monthly_review_ai"),
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
                    )
                ),
                name="alert_kind_valid",
            ),
        ),
    ]
