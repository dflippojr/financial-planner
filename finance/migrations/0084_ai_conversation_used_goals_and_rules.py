"""Record the savings goals and categorization rules a conversation quoted (#662).

No backfill: no tool quoted a goal or rule before this migration, so every
existing conversation starts with empty lists.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0083_ai_proposal_item_cards"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiconversation",
            name="used_goal_ids",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="aiconversation",
            name="used_rule_ids",
            field=models.JSONField(default=list),
        ),
    ]
