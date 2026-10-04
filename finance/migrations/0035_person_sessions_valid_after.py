from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0034_member_security_events"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="sessions_valid_after",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
