"""Drive the real import, sync and report paths for property tests, and read the ledger back.

Each Hypothesis example runs inside `isolated()`, a savepoint that is rolled
back afterwards, so examples never see each other's people or rows.
"""

import base64
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from finance.cash_flow import cash_flow_report
from finance.category_services import ensure_household_categories, income_and_spending_totals
from finance.csv_import.ofx import read_ofx
from finance.csv_import.parser import preview_csv, read_csv
from finance.csv_import.services import categorize_imported_batch, commit_csv_import
from finance.csv_import.views import FIXED_PROFILES
from finance.models import Account, Household, Membership, Person, Transaction
from finance.simplefin_services import claim_connection, save_account_links, sync_connection

from .strategies import EARLIEST, simplefin_payload

REPORT_FROM = EARLIEST
REPORT_TO = date(2026, 10, 31)
ACCESS_URL = "https://demo:synthetic-access-secret@bridge.example.test/simplefin"
CLAIM_URL = "https://bridge.example.test/simplefin/claim/synthetic-demo"


@contextmanager
def isolated():
    with transaction.atomic():
        yield
        transaction.set_rollback(True)


def make_member(username="owner"):
    user = get_user_model().objects.create_user(username=username)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, account_type, *, name="Synthetic Account", household=None):
    shared = household is not None
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD if shared else Account.Scope.PRIVATE,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if shared else "",
    )


def import_file(person, account, content, *, profile=None, mapping=None, source=None):
    """Commit one file the way the import page does, then match transfers and apply rules.

    A fixed profile supplies the header check, mapping and source; the generic
    mapper passes its own `mapping` and `source`. Returns None for a file with
    no rows, which the page would refuse before commit.
    """
    if profile is not None:
        fixed = FIXED_PROFILES[profile]
        document = read_ofx(content) if profile == "ofx" else read_csv(content)
        fixed.require_headers(document.headers)
        mapping, source = fixed.mapping, fixed.source
    else:
        document = read_csv(content)
    dates = [row.transaction_date for row in preview_csv(document, mapping).rows if row.transaction_date]
    if not dates:
        return None
    result = commit_csv_import(
        person, account.pk, content=content, document=document, mapping=mapping, source=source,
        date_range_start=min(dates), date_range_end=max(dates),
    )
    categorize_imported_batch(person, result.batch)
    return result


def import_rows(person, account, shape, rows):
    if not rows:
        return None
    return import_file(person, account, shape.render(rows), profile=shape.profile)


def link_simplefin(owner, account, cutover):
    """Claim a synthetic SimpleFIN connection and link it to `account` in transactions mode."""
    token = base64.b64encode(CLAIM_URL.encode("utf-8")).decode("ascii")
    with patch("finance.simplefin_services.claim_access_url", return_value=ACCESS_URL):
        connection = claim_connection(owner, token)
    with patch("finance.simplefin_services.fetch_accounts", return_value=simplefin_payload([])):
        save_account_links(owner, connection.pk, [{
            "simplefin_account_id": "CON-1:sf-checking", "action": "link",
            "account_id": account.pk, "cutover_date": cutover,
        }])
    return connection


def sync(owner, connection, items):
    """Run one sync whose provider feed holds `items`, (key, Row) pairs with stable ids."""
    with patch("finance.simplefin_services.fetch_accounts", return_value=simplefin_payload(items)):
        return sync_connection(owner, connection.pk, ignore_rate_limit=True)


def active_rows(accounts):
    return Transaction.objects.filter(account__in=accounts, status=Transaction.Status.ACTIVE)


def content_counter(accounts):
    """Active transactions as a multiset of what a member sees: date, amount, currency, description."""
    return Counter(active_rows(accounts).values_list("transaction_date", "amount_minor", "currency", "description"))


def per_account_sums(accounts):
    sums = Counter({account.pk: 0 for account in accounts})
    for account_id, amount in active_rows(accounts).values_list("account_id", "amount_minor"):
        sums[account_id] += amount
    return dict(sums)


@dataclass(frozen=True)
class Totals:
    income: int
    spending: int
    net: int


def totals(person, accounts=None):
    result = income_and_spending_totals(person, date_from=REPORT_FROM, date_to=REPORT_TO, accounts=accounts)
    return Totals(result.income_minor, result.spending_minor, result.net_minor)


def report_periods(person, accounts=None):
    report = cash_flow_report(
        person, date_from=REPORT_FROM, date_to=REPORT_TO, accounts=accounts, today=timezone.localdate(),
    )
    return [(period.income_minor, period.spending_minor) for period in report.periods]


def report_income_and_spending(person, accounts=None):
    """Income and spending summed over every period of the cash-flow report."""
    periods = report_periods(person, accounts)
    return sum(income for income, _ in periods), sum(spending for _, spending in periods)


def ledger_state(person, accounts):
    """Everything a reimport must leave unchanged: rows, sums, totals and the cash-flow report."""
    return {
        "rows": content_counter(accounts),
        "sums": per_account_sums(accounts),
        "totals": totals(person, accounts),
        "report": report_periods(person, accounts),
    }


def expected_totals(amounts):
    income = sum(amount for amount in amounts if amount > 0)
    spending = -sum(amount for amount in amounts if amount < 0)
    return Totals(income, spending, income - spending)
