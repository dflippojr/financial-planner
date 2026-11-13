from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive only (#743): a nullable date; existing batches stay null and no row is rewritten."""

    dependencies = [
        ('finance', '0091_import_batch_requested_from'),
    ]

    operations = [
        migrations.AddField(
            model_name='importbatch',
            name='owns_simplefin_through',
            field=models.DateField(blank=True, null=True),
        ),
    ]
