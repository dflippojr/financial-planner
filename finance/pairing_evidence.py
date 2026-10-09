"""Description evidence required before a transfer or card payment is auto-marked."""
import re

# Case-insensitive words and phrases that mark a leg as a payment or transfer.
# A term must stand alone (not sit inside a longer word); payment and transfer
# also match their plurals. Keep this list short and extend it deliberately.
PAYMENT_VOCABULARY = (
    "payment",
    "payments",
    "pmt",
    "pymt",
    "autopay",
    "epay",
    "e-payment",
    "transfer",
    "transfers",
    "xfer",
    "ach",
    "online banking",
    "crcardpmt",
)

_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(term) for term in PAYMENT_VOCABULARY) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def has_payment_wording(description):
    return bool(_PATTERN.search(description or ""))
