from django.core.management.base import BaseCommand

from finance.audit_services import purge_old_events
from finance.alert_email import notify_after_alert_run
from finance.alert_services import run_daily_alert_pass
from finance.models import SimpleFinConnection
from finance.simplefin_services import sync_all_connections


class Command(BaseCommand):
    help = "Sync every enabled SimpleFIN connection, then re-evaluate in-app alerts."

    @notify_after_alert_run
    def handle(self, *args, **options):
        purged = purge_old_events()
        self.stdout.write(f"Purged {purged} expired audit event(s).")
        if not SimpleFinConnection.objects.exists():
            self.stdout.write("No SimpleFIN connections.")
        else:
            attempted = sync_all_connections()
            self.stdout.write(self.style.SUCCESS(f"Attempted SimpleFIN sync for {attempted} connection(s)."))
        run_daily_alert_pass()
        self.stdout.write("Processed alerts.")
