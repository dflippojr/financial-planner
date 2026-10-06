from finance.models import Transaction
Transaction.objects.filter(fingerprint__gte=f"{10**7:064x}").delete()
from django.db.models.signals import post_init
from django.contrib.auth import get_user_model
from django.test import Client
from django.conf import settings
settings.STORAGES = {**settings.STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
n = [0]
post_init.connect(lambda **k: n.__setitem__(0, n[0] + 1), sender=Transaction)
c = Client(HTTP_HOST="localhost"); c.force_login(get_user_model().objects.get(username="perf_a"))
for path in ("/", "/planning/year-end/", "/spending/category/3/"):
    c.get(path); n[0] = 0; c.get(path); print(path, "Transaction objects:", n[0])
