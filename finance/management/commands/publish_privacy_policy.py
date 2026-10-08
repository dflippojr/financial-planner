from finance.audit_commands import AuditedCommand
from django.core.management.base import CommandError

from finance.policy_services import PolicySourceError, publish_policy


class Command(AuditedCommand):
    audit_operation = "publish_privacy_policy"
    help = (
        "Publish the configured privacy-policy text as a new version. "
        "Pass --material when members must accept again before using AI."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--material",
            action="store_true",
            help="Mark this version as material (members must accept again).",
        )

    def handle(self, *args, **options):
        try:
            version = publish_policy(material=options["material"])
        except PolicySourceError as exc:
            raise CommandError(str(exc)) from exc
        kind = "material" if version.is_material else "non-material"
        self.stdout.write(
            self.style.SUCCESS(f"Published privacy policy v{version.version} ({kind}).")
        )
