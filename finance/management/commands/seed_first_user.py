import getpass

from django.contrib.auth import password_validation
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from finance.auth_services import seed_first_household


class Command(BaseCommand):
    help = "Create the first user and household, printing one-time recovery codes."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--display-name", required=True)
        parser.add_argument("--household", required=True)

    def handle(self, *args, **options):
        # Match the strip=True default on the web sign-in/join/recovery forms'
        # CharField password inputs, so a CLI-seeded password with accidental
        # surrounding whitespace does not become unenterable through the UI.
        password = getpass.getpass("Password: ").strip()
        confirmation = getpass.getpass("Password (again): ").strip()
        if password != confirmation:
            raise CommandError("Passwords do not match.")
        try:
            password_validation.validate_password(password, get_user_model()(username=options["username"]))
            _user, codes = seed_first_household(
                options["username"],
                options["display_name"],
                options["household"],
                password,
            )
        except (ValueError, ValidationError) as exc:
            messages = getattr(exc, "messages", None)
            raise CommandError(" ".join(messages) if messages else str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("First household member created."))
        self.stdout.write("Save these one-time recovery codes now; they will not be shown again:")
        for code in codes:
            self.stdout.write(code)
