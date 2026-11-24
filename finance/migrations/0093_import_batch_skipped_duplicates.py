import finance.models.base
from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive (#796): each file import's skipped-duplicate fingerprints, and an `import_batch` history field.

    Existing batches keep an empty object: their files were discarded, so what
    they skipped is unknown and undoing an earlier batch archives as before.
    The history value-shape constraint is replaced to admit the new field name;
    no row is rewritten.
    """

    dependencies = [
        ('finance', '0092_import_batch_owns_simplefin_through'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='transactioncorrectionhistory',
            name='correction_history_value_shape',
        ),
        migrations.AddField(
            model_name='importbatch',
            name='skipped_duplicates',
            field=models.JSONField(blank=True, default=dict, validators=[finance.models.base.validate_json_object]),
        ),
        migrations.AlterField(
            model_name='transactioncorrectionhistory',
            name='field_name',
            field=models.CharField(choices=[('transaction_date', 'Date'), ('description', 'Description'), ('amount_minor', 'Amount'), ('category', 'Category'), ('exclusion', 'Transfer exclusion'), ('refund_link', 'Refund link'), ('note', 'Note'), ('tags', 'Tags'), ('deleted', 'Deleted'), ('untracked', 'Untracked transfer'), ('import_batch', 'Import')], max_length=16),
        ),
        migrations.AddConstraint(
            model_name='transactioncorrectionhistory',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('currency', ''), ('field_name', 'transaction_date'), ('new_amount_minor__isnull', True), ('new_date__isnull', False), ('new_description', ''), ('previous_amount_minor__isnull', True), ('previous_date__isnull', False), ('previous_description', '')), models.Q(('currency', ''), ('field_name__in', ('description', 'category', 'exclusion', 'refund_link', 'note', 'tags', 'deleted', 'untracked', 'import_batch')), ('new_amount_minor__isnull', True), ('new_date__isnull', True), ('previous_amount_minor__isnull', True), ('previous_date__isnull', True)), models.Q(('currency', 'USD'), ('field_name', 'amount_minor'), ('new_amount_minor__isnull', False), ('new_date__isnull', True), ('new_description', ''), ('previous_amount_minor__isnull', False), ('previous_date__isnull', True), ('previous_description', '')), _connector='OR'), name='correction_history_value_shape'),
        ),
    ]
