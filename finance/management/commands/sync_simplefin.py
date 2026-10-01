from django.core.management.base import BaseCommand

from finance.models import SimpleFinConnection
from finance.simplefin_services import sync_all_connections


class Command(BaseCommand):
    help = "Sync every enabled SimpleFIN connection. Skips cleanly when none exist."

    def handle(self, *args, **options):
        if not SimpleFinConnection.objects.exists():
            self.stdout.write("No SimpleFIN connections.")
            return
        attempted = sync_all_connections()
        self.stdout.write(self.style.SUCCESS(f"Attempted SimpleFIN sync for {attempted} connection(s)."))
