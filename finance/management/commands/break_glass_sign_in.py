from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.urls import reverse

from finance.audit_commands import AuditedCommand
from finance.auth_services import normalize_username
from finance.break_glass import BREAK_GLASS_TTL, issue_break_glass_token
from finance.google_auth import google_only_sign_in


class Command(AuditedCommand):
    audit_operation = "break_glass_sign_in"
    help = (
        "Production mode only: print a one-time sign-in link for a member who cannot sign in with Google. "
        "Run it on the host and open the link over the tailnet."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--unlink-google",
            action="store_true",
            help="Also remove the member's Google link, so they can connect another Google account after signing in.",
        )

    def handle(self, *args, **options):
        if not google_only_sign_in():
            raise CommandError(
                "Break-glass exists only in production mode (GOOGLE_ONLY_SIGN_IN=true). "
                "With passwords on, use a recovery code on the Recover account page."
            )
        user = (
            get_user_model()
            .objects.filter(username=normalize_username(options["username"]), person__isnull=False)
            .first()
        )
        if user is None:
            raise CommandError("No member has that username.")
        token = issue_break_glass_token(user, unlink_google=options["unlink_google"])
        minutes = int(BREAK_GLASS_TTL.total_seconds() // 60)
        self.stdout.write(self.style.SUCCESS(f"One-time sign-in link for {user.username}:"))
        self.stdout.write(reverse("break-glass", args=[token]))
        self.stdout.write(
            f"Add it to the app's tailnet address and open it within {minutes} minutes. It works once, "
            "is refused through the public tunnel, and replaces any earlier link for this member."
        )
