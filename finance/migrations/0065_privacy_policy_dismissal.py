from django.db import migrations, models
from django.db.models import F
import django.db.models.deletion


def migrate_not_now(apps, schema_editor):
    Person = apps.get_model("finance", "Person")
    Person.objects.using(schema_editor.connection.alias).filter(
        privacy_policy_declined_version__isnull=False,
    ).update(privacy_policy_dismissed_version=F("privacy_policy_declined_version"),
             privacy_policy_declined_version=None)


def restore_not_now(apps, schema_editor):
    Person = apps.get_model("finance", "Person")
    Person.objects.using(schema_editor.connection.alias).filter(
        privacy_policy_declined_version__isnull=True,
        privacy_policy_dismissed_version__isnull=False,
    ).update(privacy_policy_declined_version=F("privacy_policy_dismissed_version"))


class Migration(migrations.Migration):
    dependencies = [("finance", "0064_pending_sign_in_session")]
    operations = [
        migrations.AddField(
            model_name="person",
            name="privacy_policy_dismissed_version",
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="dismissed_by", to="finance.privacypolicyversion"),
        ),
        migrations.RunPython(migrate_not_now, restore_not_now),
    ]
