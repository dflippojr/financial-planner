import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0026_saved_csv_mapping"),
    ]

    operations = [
        migrations.AlterField(
            model_name="membership",
            name="person",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="memberships",
                to="finance.person",
            ),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.CheckConstraint(
                condition=models.Q(("person__isnull", False), ("ended_at__isnull", False), _connector="OR"),
                name="membership_person_required_while_current",
            ),
        ),
        migrations.AlterField(
            model_name="importbatch",
            name="imported_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="import_batches",
                to="finance.person",
            ),
        ),
        migrations.AlterField(
            model_name="transactioncorrectionhistory",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="transaction_correction_history",
                to="finance.person",
            ),
        ),
        migrations.AlterField(
            model_name="ruleapplication",
            name="applied_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="rule_applications",
                to="finance.person",
            ),
        ),
        migrations.AlterField(
            model_name="invitation",
            name="invited_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="invitations_created",
                to="finance.person",
            ),
        ),
        migrations.AlterField(
            model_name="budgetrolloverreset",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="budget_rollover_resets",
                to="finance.person",
            ),
        ),
    ]
