from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0027_recurring_review"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringseries",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="MonthlyReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("month", models.DateField()),
                ("visibility_key", models.CharField(max_length=64)),
                ("facts", models.JSONField()),
                ("generated_at", models.DateTimeField()),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="monthly_reviews",
                        to="finance.person",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="monthlyreview",
            constraint=models.UniqueConstraint(fields=("person", "month"), name="monthly_review_person_month"),
        ),
        migrations.AddConstraint(
            model_name="monthlyreview",
            constraint=models.CheckConstraint(condition=models.Q(("month__day", 1)), name="monthly_review_month_start"),
        ),
    ]
