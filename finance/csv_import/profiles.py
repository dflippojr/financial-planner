from .parser import CsvInputError, Mapping

GENERIC = "generic"
HUNTINGTON = "huntington"
CAPITAL_ONE = "capital_one"
APPLE_CARD = "apple_card"

PROFILE_CHOICES = (
    (GENERIC, "Generic mapper"),
    (HUNTINGTON, "Huntington"),
    (CAPITAL_ONE, "Capital One"),
    (APPLE_CARD, "Apple Card"),
)

HUNTINGTON_HEADERS = (
    "Date",
    "Reference Number",
    "Payee Name",
    "Memo",
    "Amount",
    "Category Name",
    "Transaction Number",
)

HUNTINGTON_MAPPING = Mapping(
    date_column="Date",
    description_column="Memo",
    date_format="mdy_slash_4",
    number_format="dot_none",
    amount_mode="signed",
    amount_column="Amount",
    description_mode="payee_memo",
    payee_column="Payee Name",
    memo_column="Memo",
    source_id_column="Transaction Number",
)

HUNTINGTON_HEADER_ERROR = (
    "This file does not match the Huntington checking export. "
    "Expected columns Date, Reference Number, Payee Name, Memo, Amount, "
    "Category Name, and Transaction Number."
)

APPLE_CARD_HEADERS = (
    "Transaction Date",
    "Clearing Date",
    "Description",
    "Merchant",
    "Category",
    "Type",
    "Amount (USD)",
    "Purchased By",
)

APPLE_CARD_MAPPING = Mapping(
    date_column="Transaction Date",
    description_column="Merchant",
    date_format="mdy_slash_4",
    number_format="dot_none",
    amount_mode="signed",
    amount_column="Amount (USD)",
    invert_sign=True,
)

APPLE_CARD_HEADER_ERROR = (
    "This file does not match the Apple Card export. "
    "Expected columns Transaction Date, Clearing Date, Description, Merchant, "
    "Category, Type, Amount (USD), and Purchased By."
)


CAPITAL_ONE_HEADERS = (
    "Transaction Date",
    "Posted Date",
    "Card No.",
    "Description",
    "Category",
    "Debit",
    "Credit",
)

CAPITAL_ONE_MAPPING = Mapping(
    date_column="Transaction Date",
    description_column="Description",
    date_format="iso",
    number_format="dot_none",
    amount_mode="separate",
    debit_column="Debit",
    credit_column="Credit",
    excluded_original_columns=("Card No.",),
)

CAPITAL_ONE_HEADER_ERROR = (
    "This file does not match the Capital One credit card export. "
    "Expected columns Transaction Date, Posted Date, Card No., Description, "
    "Category, Debit, and Credit."
)


def normalize_profile(value):
    if value in (HUNTINGTON, CAPITAL_ONE, APPLE_CARD):
        return value
    return GENERIC


def require_huntington_headers(headers):
    present = set(headers)
    if any(name not in present for name in HUNTINGTON_HEADERS):
        raise CsvInputError(HUNTINGTON_HEADER_ERROR)


def require_capital_one_headers(headers):
    present = set(headers)
    if any(name not in present for name in CAPITAL_ONE_HEADERS):
        raise CsvInputError(CAPITAL_ONE_HEADER_ERROR)


def require_apple_card_headers(headers):
    present = set(headers)
    if any(name not in present for name in APPLE_CARD_HEADERS):
        raise CsvInputError(APPLE_CARD_HEADER_ERROR)
