import logging

from finance.audit_commands import AuditedCommand
from finance.audit_operations import journal_run

from finance.audit_services import purge_old_events
from finance.alert_email import notify_after_alert_run
from finance.alert_services import run_daily_alert_pass
from finance.models import SimpleFinConnection
from finance.simplefin_services import sync_all_connections

logger = logging.getLogger(__name__)


class Command(AuditedCommand):
    audit_operation = "sync_simplefin"
    help = "Sync every enabled SimpleFIN connection, then re-evaluate in-app alerts."

    @notify_after_alert_run
    def handle(self, *args, **options):
        purged = journal_run("retention_cleanup")(purge_old_events)()
        self.stdout.write(f"Purged {purged} expired audit event(s).")
        try:
            self._sync()
        except Exception as exc:  # The alert pass below must still run.
            logger.error("SimpleFIN sync stage failed (%s); running the alert pass anyway.", type(exc).__name__)
            self.stderr.write("SimpleFIN sync stage failed; alerts are still processed.")
        run_daily_alert_pass()
        self.stdout.write("Processed alerts.")

    def _sync(self):
        if not SimpleFinConnection.objects.exists():
            self.stdout.write("No SimpleFIN connections.")
            return
        attempted = sync_all_connections()
        self.stdout.write(self.style.SUCCESS(f"Attempted SimpleFIN sync for {attempted} connection(s)."))
