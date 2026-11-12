from decimal import Decimal


def format_minor(amount_minor, currency="USD"):
    amount = Decimal(amount_minor) / Decimal(100)
    return f"{amount:,.2f} {currency}"


def format_minor_input(amount_minor):
    """A USD amount as an edit form shows it: `-$1,234.50` (#726).

    MoneyField reads it back to the same amount.
    """
    amount = Decimal(amount_minor) / Decimal(100)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"
