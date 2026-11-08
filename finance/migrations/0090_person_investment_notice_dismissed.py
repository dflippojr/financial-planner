from django.db import migrations, models


class Migration(migrations.Migration):
    """Each member can dismiss the investment-activity notice (#699); blank means not dismissed."""

    dependencies = [("finance", "0089_simplefin_bank_health")]

    operations = [
        migrations.AddField(
            model_name="person",
            name="investment_notice_dismissed_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
