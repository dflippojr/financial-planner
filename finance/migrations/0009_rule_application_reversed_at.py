from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0008_category_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleapplication",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
