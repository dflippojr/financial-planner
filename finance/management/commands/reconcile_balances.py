"""Read-only check of stored transactions against bank-reported balances (#535)."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from finance.reconciliation import INSUFFICIENT, MISMATCH, STATES, eligible_accounts, machine_summary, reconcile


class Command(BaseCommand):
    help = (
        "Compare each cash account's balance changes with its transactions between consecutive "
        "balance snapshots. Prints account IDs, dates and window states only, never amounts or "
        "account names. Exits non-zero when any window is a mismatch."
    )

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, help="Reconcile only this account ID.")
        parser.add_argument("--since", help="Report only windows ending on or after this date (YYYY-MM-DD).")
        parser.add_argument("--json", action="store_true", help="Print counts and window states as JSON.")

    def handle(self, *args, **options):
        since = None
        if options["since"]:
            since = parse_date(options["since"])
            if since is None:
                raise CommandError("--since must be a date in YYYY-MM-DD form.")
        accounts = eligible_accounts()
        if options["account"] is not None:
            accounts = accounts.filter(pk=options["account"])
            if not accounts.exists():
                raise CommandError(
                    "That account ID is not reconciled: it must be an active checking, savings or credit card "
                    "account with a SimpleFIN transactions link or at least two balance entries."
                )
        results = reconcile(accounts, since=since)
        summary = machine_summary(results)
        if options["json"]:
            self.stdout.write(json.dumps(summary, sort_keys=True))
        else:
            self._write_text(results, summary)
        mismatches = summary["counts"][MISMATCH]
        if mismatches:
            raise CommandError(f"{mismatches} window(s) do not reconcile.", returncode=1)

    def _write_text(self, results, summary):
        for account_id, windows in results.items():
            counts = dict.fromkeys(STATES, 0)
            for window in windows:
                counts[window.state] += 1
            parts = ", ".join(f"{counts[state]} {state}" for state in STATES)
            self.stdout.write(f"Account {account_id}: {len(windows)} window(s): {parts}")
            for window in windows:
                if window.state in (MISMATCH, INSUFFICIENT):
                    self.stdout.write(f"  {window.state} after {window.start.isoformat()} through {window.end.isoformat()}")
        total = ", ".join(f"{summary['counts'][state]} {state}" for state in STATES)
        message = f"{summary['accounts']} account(s) reconciled: {total}."
        if summary["counts"][MISMATCH]:
            self.stdout.write(message)
        else:
            self.stdout.write(self.style.SUCCESS(message))

