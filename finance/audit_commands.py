"""Operator commands declare identity truthfully and correlate domain events."""
from django.core.management.base import BaseCommand, CommandError

from ops.backup.audit_journal import append
from .audit_operations import execution, operation
from .models import AuditEvent, Person


class AuditedCommand(BaseCommand):
    audit_operation = None

    def create_parser(self, *args, **kwargs):
        parser = super().create_parser(*args, **kwargs)
        parser.add_argument("--operator-member", type=int, help="Declared app member ID, not authenticated identity.")
        return parser

    def execute(self, *args, **options):
        current = execution.get()
        declared = None
        if options.get("operator_member") is not None:
            declared = Person.objects.filter(pk=options["operator_member"]).first()
            if declared is None or current is not None:
                raise CommandError("Invalid operator member declaration.")
        if current is not None:
            return self._execute_audited(current, args, options)
        with operation(actor_kind=AuditEvent.ActorKind.OPERATOR, source=AuditEvent.Source.CLI,
                       declared_operator=declared) as context:
            return self._execute_audited(context, args, options)

    def _execute_audited(self, context, args, options):
        append(self.audit_operation, "started", context.run_id, actor=context.actor_kind)
        try:
            result = super().execute(*args, **options)
        except Exception:
            append(self.audit_operation, "failed", context.run_id, actor=context.actor_kind)
            raise
        append(self.audit_operation, "succeeded", context.run_id, actor=context.actor_kind)
        return result
