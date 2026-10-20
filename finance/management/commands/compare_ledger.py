"""Local-only monthly comparison of cash-flow totals with the owner's spreadsheet (#536)."""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError

from finance.auth_services import normalize_username
from finance.cash_flow import selected_accounts
from finance.ledger_comparison import DIFFERS, STATES, compare_ledger, summary
from finance.models import Person, SheetComparisonSettings
from finance.sheet_comparison import (
    MAX_FILE_BYTES,
    SheetCsvError,
    mapping_matches_headers,
    parse_sheet_month,
    read_sheet_csv,
    settings_for,
)

MONTH_LABEL = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_label(value):
    try:
        if not MONTH_LABEL.fullmatch(value):
            raise ValueError
        return parse_sheet_month(value)
    except ValueError:
        # A well-shaped label can still be no calendar month, such as year 0000.
        raise CommandError("--known-timing must be a month in YYYY-MM form.") from None


class Command(BaseCommand):
    help = (
        "Compare each month's income and spending in the cash-flow view with a CSV exported from your "
        "spreadsheet, exactly to the cent. Prints month labels and match/differs states only, never amounts, "
        "descriptions or account names. Reads the file and the database; stores nothing. Exits non-zero when "
        "any ended month differs."
    )

    def add_arguments(self, parser):
        parser.add_argument("--sheet", required=True, help="Path to the exported CSV, kept outside the repository, or - to read it from standard input.")
        parser.add_argument("--username", required=True, help="Member whose visible accounts are compared.")
        parser.add_argument("--account", type=int, help="Compare only this account ID.")
        parser.add_argument("--month-column", help="Sheet column holding the month. Default: saved mapping, else Month.")
        parser.add_argument("--income-column", help="Sheet column holding income. Default: saved mapping, else Income.")
        parser.add_argument(
            "--spending-column", help="Sheet column holding spending. Default: saved mapping, else Spending."
        )
        parser.add_argument(
            "--spending-sign",
            choices=SheetComparisonSettings.SpendingSign.values,
            help="Whether spending is positive (unsigned) or negative (signed). Default: saved mapping, else unsigned.",
        )
        parser.add_argument(
            "--known-timing",
            action="append",
            default=[],
            metavar="YYYY-MM",
            help="A month with a known timing difference; reported as excepted when it differs. Repeatable.",
        )
        parser.add_argument("--json", action="store_true", help="Print counts and month states as JSON.")

    def handle(self, *args, **options):
        person = Person.objects.filter(user__username=normalize_username(options["username"])).first()
        if person is None:
            raise CommandError("Member not found.")
        excepted = [_month_label(value) for value in options["known_timing"]]
        raw = self._read_file(options["sheet"])
        try:
            headers, rows = read_sheet_csv(raw)
        except SheetCsvError as exc:
            raise CommandError(exc.message) from exc
        mapping = self._mapping(person, options)
        if not mapping_matches_headers(mapping, headers):
            raise CommandError("The month, income and spending columns must all be headers in the sheet.")
        accounts = selected_accounts(person, cash_flow_only=True)
        if options["account"] is not None:
            accounts = [account for account in accounts if account.pk == options["account"]]
            if not accounts:
                raise CommandError("That account ID is not a cash-flow account you can see.")
        try:
            results = compare_ledger(person, rows, mapping, accounts=accounts, excepted_months=excepted)
        except SheetCsvError as exc:
            raise CommandError(exc.message) from exc
        report = summary(results)
        if options["json"]:
            self.stdout.write(json.dumps({"version": 1, "counts": report.counts, "months": report.months}, sort_keys=True))
        else:
            self._write_text(report)
        if report.counts[DIFFERS]:
            raise CommandError(f"{report.counts[DIFFERS]} month(s) differ.", returncode=1)

    def _read_file(self, path):
        try:
            if path == "-":
                raw = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
            else:
                with Path(path).open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise CommandError("The sheet file could not be read.") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise CommandError("Choose a CSV file of at most 5 MB.")
        return raw

    def _mapping(self, person, options):
        saved = settings_for(person)
        return SimpleNamespace(
            month_column=options["month_column"] or (saved.month_column if saved else "Month"),
            income_column=options["income_column"] or (saved.income_column if saved else "Income"),
            spending_column=options["spending_column"] or (saved.spending_column if saved else "Spending"),
            spending_sign=options["spending_sign"]
            or (saved.spending_sign if saved else SheetComparisonSettings.SpendingSign.UNSIGNED),
        )

    def _write_text(self, report):
        for entry in report.months:
            if entry["state"] != "match":
                self.stdout.write(f"{entry['month']} {entry['state']}")
        counts = ", ".join(f"{report.counts[state]} {state}" for state in STATES)
        message = f"{len(report.months)} month(s) compared: {counts}."
        self.stdout.write(message if report.counts[DIFFERS] else self.style.SUCCESS(message))
