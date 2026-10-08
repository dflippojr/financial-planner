from finance.audit_commands import AuditedCommand
from django.core.exceptions import PermissionDenied
from django.core.management.base import CommandError

from finance.lifecycle_services import end_current_membership
from finance.models import Person


class Command(AuditedCommand):
    audit_operation = "evict_household_member"
    help = (
        "End a person's current household membership from the host. "
        "Members cannot remove each other in the application."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    def handle(self, *args, **options):
        username = options["username"]
        try:
            person = Person.objects.select_related("user").get(user__username=username)
        except Person.DoesNotExist as exc:
            raise CommandError("Unknown username.") from exc
        try:
            end_current_membership(person, audit_actor=person)
        except PermissionDenied as exc:
            raise CommandError("That person has no current household membership.") from exc
        self.stdout.write(self.style.SUCCESS(f"Ended current household membership for {username}."))
