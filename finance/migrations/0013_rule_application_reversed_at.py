from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0012_category_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleapplication",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ruleapplicationentry",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="categoryrule",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
