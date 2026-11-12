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
from finance.models import Account, BalanceSnapshot, Household, Membership, Person, Transaction
from finance.simplefin_services import SimpleFinError, claim_connection, save_account_links, sync_connection

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


def claim_simplefin(owner):
    """Claim a synthetic SimpleFIN connection with no accounts linked yet."""
    token = base64.b64encode(CLAIM_URL.encode("utf-8")).decode("ascii")
    with patch("finance.simplefin_services.claim_access_url", return_value=ACCESS_URL):
        return claim_connection(owner, token)


def save_link(owner, connection, account, cutover):
    """Link (or re-link) `account` in transactions mode the way the form does.

    Returns False when the save is refused, which leaves every link as it was.
    """
    try:
        save_account_links(owner, connection.pk, [{
            "simplefin_account_id": "CON-1:sf-checking", "action": "link",
            "account_id": account.pk, "cutover_date": cutover,
        }])
    except SimpleFinError:
        return False
    return True


def link_simplefin(owner, account, cutover):
    """Claim a synthetic SimpleFIN connection and link it to `account` in transactions mode."""
    connection = claim_simplefin(owner)
    with patch("finance.simplefin_services.fetch_accounts", return_value=simplefin_payload([])):
        save_account_links(owner, connection.pk, [{
            "simplefin_account_id": "CON-1:sf-checking", "action": "link",
            "account_id": account.pk, "cutover_date": cutover,
        }])
    return connection


def sync(owner, connection, items, **account_fields):
    """Run one sync whose provider feed holds `items`, (key, Row) pairs with stable ids.

    `account_fields` (balance, currency) override the feed's account object.
    """
    payload = simplefin_payload(items, **account_fields)
    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        return sync_connection(owner, connection.pk, ignore_rate_limit=True)


# Bridge cuts a longer window to the last 90 days, counted back from its end,
# and says so with this notice (#629, #640).
BRIDGE_LIMIT_SECONDS = 90 * 86400
BRIDGE_CAPPED_NOTICE = {"code": "gen.api", "msg": "Requested date range exceeds limit of 90 days and was capped."}


def sync_with_bridge_cap(owner, connection, items, *, now):
    """Run one sync at `now` against a feed that serves only the window Bridge would.

    Returns (the first instant the feed served, in epoch seconds, or None when
    it was asked for no start; whether it cut the window).
    """
    served = []

    def fetch(access_url, *, start_date=None, end_date=None, balances_only=False):
        payload = simplefin_payload(items)
        start = start_date
        if start_date is not None and end_date - start_date > BRIDGE_LIMIT_SECONDS:
            start = end_date - BRIDGE_LIMIT_SECONDS
            payload["errlist"].append(dict(BRIDGE_CAPPED_NOTICE))
        served.append((start, start != start_date))
        remote = payload["accounts"][0]
        remote["transactions"] = [
            item for item in remote["transactions"] if start is None or start <= item["posted"] < end_date
        ]
        return payload

    with patch("finance.simplefin_services.fetch_accounts", fetch), \
            patch("django.utils.timezone.now", return_value=now):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)
    return served[-1]


def simplefin_balances(account):
    """The account's SimpleFIN balance snapshots, oldest day first, as minor amounts."""
    return list(BalanceSnapshot.objects.filter(account=account, source=BalanceSnapshot.Source.SIMPLEFIN)
                .order_by("snapshot_date").values_list("amount_minor", flat=True))


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
