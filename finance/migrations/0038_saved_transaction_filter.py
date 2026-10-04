import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

import finance.models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0037_account_debt_terms"),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedTransactionFilter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80)),
                ("query", models.JSONField(validators=[finance.models.validate_saved_filter_query])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_transaction_filters",
                        to="finance.person",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="savedtransactionfilter",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                models.F("member"),
                name="saved_txn_filter_unique_name_per_member",
            ),
        ),
    ]
