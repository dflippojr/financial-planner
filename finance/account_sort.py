"""Sorting the balance-by-account lists on Net worth and Accounts (#655).

Each member keeps one saved order per page in AccountSortSettings. A blank
order keeps the list as the page builds it. Balances are net-worth values:
an amount owed is negative, so "high to low" ends with the largest debt.
"""

from .models import AccountSortSettings

Order = AccountSortSettings.Order

NET_WORTH = "net_worth"
ACCOUNTS = "accounts"
# The page names a form may send, mapped to the settings field that stores each.
PAGE_FIELDS = {NET_WORTH: "net_worth_sort", ACCOUNTS: "accounts_sort"}
DEFAULT_LABEL = "Default order"
CHOICES = (("", DEFAULT_LABEL), *Order.choices)


def saved_order(person, page):
    """The member's saved order for a page, or "" when they never chose one."""
    if person is None:
        return ""
    field = PAGE_FIELDS[page]
    value = AccountSortSettings.objects.filter(person=person).values_list(field, flat=True).first()
    return value or ""


def save_order(person, page, order):
    """Store one page's order for this member; the other page's choice stays as it was."""
    if order not in ("", *Order.values):
        raise ValueError("Unknown sort order.")
    AccountSortSettings.objects.update_or_create(person=person, defaults={PAGE_FIELDS[page]: order})


def sort_rows(rows, order, *, name, balance):
    """Return rows in the chosen order; name(row) gives its name, balance(row) its signed minor amount or None.

    Name sorts ignore case and keep rows with the same name in their given
    order. Balance sorts break ties by name A to Z and put rows with no
    balance last in both directions, themselves by name.
    """
    rows = list(rows)
    if order in (Order.NAME_ASC, Order.NAME_DESC):
        return sorted(rows, key=lambda row: name(row).casefold(), reverse=order == Order.NAME_DESC)
    if order in (Order.BALANCE_DESC, Order.BALANCE_ASC):
        by_name = sorted(rows, key=lambda row: name(row).casefold())
        known = [row for row in by_name if balance(row) is not None]
        missing = [row for row in by_name if balance(row) is None]
        # sorted() is stable, so equal balances keep the name order from by_name.
        known = sorted(known, key=balance, reverse=order == Order.BALANCE_DESC)
        return known + missing
    return rows
