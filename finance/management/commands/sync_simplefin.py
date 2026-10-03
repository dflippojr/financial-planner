from django.core.management.base import BaseCommand

from finance.alert_services import run_daily_alert_pass
from finance.models import SimpleFinConnection
from finance.simplefin_services import sync_all_connections


class Command(BaseCommand):
    help = "Sync every enabled SimpleFIN connection, then re-evaluate in-app alerts."

    def handle(self, *args, **options):
        if not SimpleFinConnection.objects.exists():
            self.stdout.write("No SimpleFIN connections.")
        else:
            attempted = sync_all_connections()
            self.stdout.write(self.style.SUCCESS(f"Attempted SimpleFIN sync for {attempted} connection(s)."))
        run_daily_alert_pass()
        self.stdout.write("Processed alerts.")
