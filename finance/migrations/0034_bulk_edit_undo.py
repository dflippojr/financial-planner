import uuid

import django.db.models.deletion
from django.db import migrations, models

import finance.models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0033_alert_backup_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="transactioncorrectionhistory",
            name="field_name",
            field=models.CharField(
                choices=[
                    ("transaction_date", "Date"),
                    ("description", "Description"),
                    ("amount_minor", "Amount"),
                    ("category", "Category"),
                    ("exclusion", "Transfer exclusion"),
                    ("refund_link", "Refund link"),
                    ("note", "Note"),
                    ("tags", "Tags"),
                ],
                max_length=16,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="transactioncorrectionhistory",
            name="correction_history_value_shape",
        ),
        migrations.AddConstraint(
            model_name="transactioncorrectionhistory",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("currency", ""),
                        ("field_name", "transaction_date"),
                        ("new_amount_minor__isnull", True),
                        ("new_date__isnull", False),
                        ("new_description", ""),
                        ("previous_amount_minor__isnull", True),
                        ("previous_date__isnull", False),
                        ("previous_description", ""),
                    ),
                    models.Q(
                        ("currency", ""),
                        (
                            "field_name__in",
                            ("description", "category", "exclusion", "refund_link", "note", "tags"),
                        ),
                        ("new_amount_minor__isnull", True),
                        ("new_date__isnull", True),
                        ("previous_amount_minor__isnull", True),
                        ("previous_date__isnull", True),
                    ),
                    models.Q(
                        ("currency", "USD"),
                        ("field_name", "amount_minor"),
                        ("new_amount_minor__isnull", False),
                        ("new_date__isnull", True),
                        ("new_description", ""),
                        ("previous_amount_minor__isnull", False),
                        ("previous_date__isnull", True),
                        ("previous_description", ""),
                    ),
                    _connector="OR",
                ),
                name="correction_history_value_shape",
            ),
        ),
        migrations.CreateModel(
            name="BulkEditUndo",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("undone_at", models.DateTimeField(blank=True, null=True)),
                ("snapshot", models.JSONField(validators=(finance.models.validate_bulk_edit_snapshot,))),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bulk_edit_undos",
                        to="finance.person",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="bulkeditundo",
            index=models.Index(fields=["actor", "expires_at"], name="bulk_edit_undo_actor_exp_idx"),
        ),
    ]
