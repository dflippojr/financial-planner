from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0050_ai_api_key_connections"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiproviderconnection",
            name="offer_local_chat",
            field=models.BooleanField(default=False),
        ),
    ]
