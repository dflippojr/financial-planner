from .parser import CsvInputError, Mapping

GENERIC = "generic"
HUNTINGTON = "huntington"

PROFILE_CHOICES = (
    (GENERIC, "Generic mapper"),
    (HUNTINGTON, "Huntington"),
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


def normalize_profile(value):
    if value == HUNTINGTON:
        return HUNTINGTON
    return GENERIC


def require_huntington_headers(headers):
    present = set(headers)
    if any(name not in present for name in HUNTINGTON_HEADERS):
        raise CsvInputError(HUNTINGTON_HEADER_ERROR)
