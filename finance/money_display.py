from decimal import Decimal


def format_minor(amount_minor, currency="USD"):
    amount = Decimal(amount_minor) / Decimal(100)
    return f"{amount:,.2f} {currency}"
