from django.db import migrations, models


def backfill_co_owned(apps, schema_editor):
    Account = apps.get_model("finance", "Account")
    Account.objects.filter(scope="household", share_mode__isnull=True).update(share_mode="co_owned")


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0006_recurring_series_is_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="share_mode",
            field=models.CharField(
                blank=True,
                choices=[("co_owned", "Co-owned"), ("lent", "Lent")],
                max_length=8,
                null=True,
            ),
        ),
        migrations.RunPython(backfill_co_owned, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("scope", "private"), ("share_mode__isnull", True))
                    | models.Q(
                        ("scope", "household"),
                        ("share_mode__in", ("co_owned", "lent")),
                        ("share_mode__isnull", False),
                    )
                ),
                name="account_share_mode_matches_scope",
            ),
        ),
    ]
