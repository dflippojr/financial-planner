from finance.audit_commands import AuditedCommand
from django.utils import timezone

from finance.monthly_review import generate_due_monthly_reviews, latest_closed_month


class Command(AuditedCommand):
    audit_operation = "generate_monthly_reviews"
    help = "Generate stored monthly reviews for the latest closed month."

    def handle(self, *args, **options):
        today = timezone.localdate()
        month = latest_closed_month(today)
        created = generate_due_monthly_reviews(today=today)
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated {len(created)} review(s) for {month.isoformat()[:7]}."
            )
        )
