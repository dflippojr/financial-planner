"""Month-by-month check of cash-flow totals against an exported sheet (#536).

Compares income and spending separately, exactly to the cent. A month that has
not ended is `open`: the sheet's total can't be final yet. A differing month the
owner has declared a known timing difference is `excepted`.
"""

from types import SimpleNamespace

from django.utils import timezone

from .cash_flow import selected_accounts
from .category_services import income_and_spending_totals
from .months import month_end, month_start
from .sheet_comparison import parsed_month_totals

MATCH = "match"
DIFFERS = "differs"
EXCEPTED = "excepted"
OPEN = "open"
STATES = (MATCH, DIFFERS, EXCEPTED, OPEN)


def compare_ledger(person, rows, mapping, *, accounts=None, excepted_months=(), today=None):
    """Return the state of each sheet month: [(first day of month, state)], oldest first.

    `rows` and `mapping` are what `read_sheet_csv` and the mapping columns give.
    `accounts` defaults to the person's cash-flow accounts, as the cash-flow view uses.
    """
    today = today or timezone.localdate()
    if accounts is None:
        accounts = selected_accounts(person, cash_flow_only=True)
    excepted = {month_start(month) for month in excepted_months}
    results = []
    for month, (sheet_income, sheet_spending) in sorted(parsed_month_totals(rows, mapping).items()):
        if month_end(month) >= today:
            results.append((month, OPEN))
            continue
        app = income_and_spending_totals(
            person, date_from=month_start(month), date_to=month_end(month), accounts=accounts
        )
        if app.income_minor == sheet_income and app.spending_minor == sheet_spending:
            state = MATCH
        else:
            state = EXCEPTED if month in excepted else DIFFERS
        results.append((month, state))
    return results


def summary(results):
    """Counts per state and the months, as labels only: no amounts."""
    counts = dict.fromkeys(STATES, 0)
    for _month, state in results:
        counts[state] += 1
    return SimpleNamespace(
        counts=counts,
        months=[{"month": month.strftime("%Y-%m"), "state": state} for month, state in results],
    )
