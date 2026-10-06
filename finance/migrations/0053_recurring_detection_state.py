from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0052_ai_plan_links'),
    ]

    operations = [
        migrations.AddField(
            model_name='person',
            name='recurring_inputs_signature',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='person',
            name='recurring_skipped_merchants',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
