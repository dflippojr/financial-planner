"""Explicit maintenance rebuild of transfer suggestions for current members."""
from finance.audit_operations import outcome
from finance.audit_commands import AuditedCommand
from django.core.management.base import CommandError

from finance.category_services import refresh_transfer_pairs
from finance.models import Person


class Command(AuditedCommand):
    audit_operation = "rebuild_transfer_pairs"
    help = "Rebuild transfer matching for one member, or all members (maintenance only)."

    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--all", action="store_true", dest="all_members")

    def handle(self, *args, **options):
        if bool(options["username"]) == options["all_members"]:
            raise CommandError("Choose --username or --all.")
        people = Person.objects.order_by("pk")
        if options["username"]:
            people = people.filter(user__username=options["username"])
            if not people.exists():
                raise CommandError("Member not found.")
        for person in people.iterator():
            refresh_transfer_pairs(person)
            outcome(person, "transfer_rebuild")
        self.stdout.write(self.style.SUCCESS("Transfer matching rebuilt."))
