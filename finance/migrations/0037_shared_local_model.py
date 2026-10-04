from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0036_bulk_edit_undo"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiusageevent",
            name="resumed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="aiproviderconnection",
            name="offer_local_to_household",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="person",
            name="use_shared_local_background",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="person",
            name="use_shared_local_chat",
            field=models.BooleanField(default=False),
        ),
    ]
