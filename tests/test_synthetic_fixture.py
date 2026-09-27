import pytest
from django.core.management import call_command
from django.db.models import F

from finance.models import Account, ImportBatch, Person, Transaction


@pytest.mark.django_db
def test_synthetic_demo_fixture_loads_and_relations_match():
    call_command("loaddata", "synthetic_demo", verbosity=0)

    assert Person.objects.count() == 1
    assert Account.objects.count() == 2
    assert ImportBatch.objects.count() == 2
    assert Transaction.objects.count() == 3
    assert not Transaction.objects.exclude(account_id=F("import_batch__account_id")).exists()
