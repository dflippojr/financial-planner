import json

from django.core.management.base import BaseCommand, CommandError

from finance.audit_services import events_for
from finance.audit_views import AuditFilterForm
from finance.models import Person


class Command(BaseCommand):
    help = "Read audit metadata as a member; operator status grants no broader audience."

    def add_arguments(self, parser):
        parser.add_argument("--member", type=int, required=True)
        for field in ("action", "actor", "source", "date-from", "date-to"):
            parser.add_argument(f"--{field}", default="")
        parser.add_argument("--page", type=int, default=1)

    def handle(self, *args, **options):
        from django.core.paginator import Paginator

        member = Person.objects.filter(pk=options["member"]).first()
        if member is None:
            raise CommandError("Member is unavailable.")
        form = AuditFilterForm({key: options[key] for key in AuditFilterForm.base_fields})
        if not form.is_valid() or options["page"] < 1:
            raise CommandError("Invalid audit filters or page.")
        page = Paginator(events_for(member, **form.cleaned_data), 50).get_page(options["page"])
        self.stdout.write(json.dumps({"count": page.paginator.count, "page": page.number, "events": [
            {"id": str(row.pk), "occurred_at": row.occurred_at.isoformat(), "action": row.action,
             "outcome": row.outcome, "actor_kind": row.actor_kind, "actor_id": row.actor_id,
             "effective_member_id": row.effective_member_id, "affected_member_id": row.affected_member_id, "target_type": row.target_type,
             "target_id": row.target_id, "source": row.source, "correlation_id": str(row.correlation_id),
             "changed_fields": row.changed_fields, "metadata": row.metadata, "checksum_valid": row.checksum == row.calculated_checksum()}
            for row in page
        ]}))
