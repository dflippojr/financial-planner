"""Read-only check for stored rows dated outside the plausible activity window."""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from finance.date_bounds import EARLIEST_ACTIVITY_DATE, latest_activity_date
from finance.models import BalanceSnapshot, Transaction


class Command(BaseCommand):
    help = (
        "List transactions and balance entries dated outside the plausible window. "
        "Prints counts and row IDs only, never amounts, descriptions or account names."
    )

    def handle(self, *args, **options):
        latest = latest_activity_date(timezone.localdate())
        checks = (
            ("transaction", Transaction.objects, "transaction_date"),
            ("balance entry", BalanceSnapshot.objects, "snapshot_date"),
        )
        found = 0
        for label, manager, field in checks:
            outside = Q(**{f"{field}__lt": EARLIEST_ACTIVITY_DATE}) | Q(**{f"{field}__gt": latest})
            ids = list(manager.filter(outside).order_by("pk").values_list("pk", flat=True))
            found += len(ids)
            self.stdout.write(f"{len(ids)} {label} row(s) dated outside {EARLIEST_ACTIVITY_DATE} to {latest}.")
            if ids:
                self.stdout.write("  IDs: " + ", ".join(str(pk) for pk in ids))
        if not found:
            self.stdout.write(self.style.SUCCESS("All stored dates are within the plausible window."))
