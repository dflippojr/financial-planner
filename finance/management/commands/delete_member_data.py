from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from finance.lifecycle_services import LENT_CHOICES, delete_member_data, lent_household_accounts
from finance.models import Person


class Command(BaseCommand):
    help = (
        "Permanently delete a member's private data and login. "
        "Household eviction without deleting the login is evict_household_member."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--lent",
            action="append",
            default=[],
            metavar="ID=handover|delete",
            help="Choice for each household account the member has lent.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        typed = input("Type the username to confirm: ").strip()
        if typed != username:
            raise CommandError("Confirmation did not match.")
        try:
            person = Person.objects.select_related("user").get(user__username=username)
        except Person.DoesNotExist as exc:
            raise CommandError("Unknown username.") from exc
        lent_choices = self._lent_choices(options["lent"])
        required = {account.pk for account in lent_household_accounts(person)}
        if set(lent_choices) != required:
            raise CommandError("Every lent account needs a choice.")
        try:
            delete_member_data(person, lent_choices)
        except (PermissionDenied, ValidationError) as exc:
            raise CommandError("Deletion could not be completed.") from exc
        self.stdout.write(self.style.SUCCESS("Deleted the member's data."))

    def _lent_choices(self, raw_items):
        choices = {}
        for item in raw_items:
            if "=" not in item:
                raise CommandError("Every lent account needs a choice.")
            account_id, value = item.split("=", 1)
            try:
                pk = int(account_id)
            except ValueError as exc:
                raise CommandError("Every lent account needs a choice.") from exc
            if value not in LENT_CHOICES:
                raise CommandError("Every lent account needs a choice.")
            choices[pk] = value
        return choices
