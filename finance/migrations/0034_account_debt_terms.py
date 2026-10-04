from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0033_alert_backup_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="apr_percent",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="account",
            name="minimum_payment_minor",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="account",
            name="payment_day",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(("apr_percent__isnull", True), ("apr_percent__gte", 0), _connector="OR"),
                name="account_apr_percent_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("minimum_payment_minor__isnull", True),
                    ("minimum_payment_minor__gte", 0),
                    _connector="OR",
                ),
                name="account_minimum_payment_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("payment_day__isnull", True),
                    models.Q(("payment_day__gte", 1), ("payment_day__lte", 31)),
                    _connector="OR",
                ),
                name="account_payment_day_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(("payment_day__isnull", True), ("account_type", "loan"), _connector="OR"),
                name="account_payment_day_requires_loan",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("account_type__in", ("loan", "credit_card")),
                    models.Q(("apr_percent__isnull", True), ("minimum_payment_minor__isnull", True)),
                    _connector="OR",
                ),
                name="account_debt_terms_require_liability",
            ),
        ),
    ]
