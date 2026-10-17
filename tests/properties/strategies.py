"""Hypothesis strategies for synthetic statement rows and SimpleFIN payloads (#533).

Every value is invented. Descriptions start with "SYN", carry no digits beyond a
short row index, and identifiers are "SYN-" sequences, so nothing here looks
like a real account number, card number or merchant record.

A `Row` holds one real-world transaction in the ledger's sign convention
(negative is money out). Each `Shape` renders rows as that source's export,
following docs/csv-shapes.md and the fixed profiles in
finance/csv_import/profiles.py, and says what the ledger should store for them.
The expected values here are written from those documents, not computed by
the importer, so a test can compare the two.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from hypothesis import strategies as st

from finance.models import Account

EARLIEST = date(2025, 1, 1)
LATEST = date(2026, 9, 30)
# Up to ten billion dollars: far beyond any household statement, well inside
# the signed 64-bit minor-unit column.
MAX_MINOR = 10**12


@dataclass(frozen=True)
class Row:
    day: date
    amount_minor: int
    payee: str
    memo: str
    source_id: str


def decimal_text(amount_minor):
    """Exact two-place text for a signed minor amount, with no thousands separator."""
    sign = "-" if amount_minor < 0 else ""
    whole, cents = divmod(abs(amount_minor), 100)
    return f"{sign}{whole}.{cents:02d}"


def payee_memo(row):
    """The documented `Payee - Memo` join, using whichever part is present."""
    return f"{row.payee} - {row.memo}" if row.memo else row.payee


def _csv_bytes(header, records):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(records)
    return buffer.getvalue().encode("utf-8")


def _mdy(day):
    return day.strftime("%m/%d/%Y")


def _huntington(rows):
    # Signed Amount, negative is money out; description is Payee Name - Memo.
    return _csv_bytes(
        ("Date", "Reference Number", "Payee Name", "Memo", "Amount", "Category Name", "Transaction Number"),
        [(_mdy(r.day), "1", r.payee, r.memo, decimal_text(r.amount_minor), "", r.source_id) for r in rows],
    )


def _capital_one(rows):
    # Debit is a charge (money out), Credit a payment or refund (money in), both unsigned.
    return _csv_bytes(
        ("Transaction Date", "Posted Date", "Card No.", "Description", "Category", "Debit", "Credit"),
        [
            (
                r.day.isoformat(), r.day.isoformat(), "0000", r.payee, "Synthetic",
                decimal_text(-r.amount_minor) if r.amount_minor < 0 else "",
                decimal_text(r.amount_minor) if r.amount_minor > 0 else "",
            )
            for r in rows
        ],
    )


def _apple_card(rows):
    # Amount (USD) is positive for a purchase and negative for a payment or credit.
    return _csv_bytes(
        ("Transaction Date", "Clearing Date", "Description", "Merchant", "Category", "Type",
         "Amount (USD)", "Purchased By"),
        [
            (
                _mdy(r.day), _mdy(r.day), "SYNTHETIC", r.payee, "Synthetic",
                "Purchase" if r.amount_minor < 0 else "Credit",
                decimal_text(-r.amount_minor), "Synthetic Member",
            )
            for r in rows
        ],
    )


def _sgml(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ofx(rows):
    # TRNAMT is signed as supplied: negative is money out. Name and Memo join as Name - Memo.
    transactions = "".join(
        "<STMTTRN><TRNTYPE>{kind}\n<DTPOSTED>{day:%Y%m%d}\n<TRNAMT>{amount}\n<FITID>{fitid}\n"
        "<NAME>{name}\n{memo}</STMTTRN>\n".format(
            kind="DEBIT" if r.amount_minor < 0 else "CREDIT",
            day=r.day,
            amount=decimal_text(r.amount_minor),
            fitid=r.source_id,
            name=_sgml(r.payee),
            memo=f"<MEMO>{_sgml(r.memo)}\n" if r.memo else "",
        )
        for r in rows
    )
    text = (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\nCHARSET:1252\n"
        "COMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>\n<CURDEF>USD\n"
        "<BANKACCTFROM><BANKID>000000000\n<ACCTID>SYNTHETIC-ONLY\n<ACCTTYPE>CHECKING\n</BANKACCTFROM>\n"
        f"<BANKTRANLIST>\n{transactions}</BANKTRANLIST>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n"
    )
    return text.encode("ascii")


@dataclass(frozen=True)
class Shape:
    profile: str
    account_type: str
    render: object
    stored_description: object

    def __repr__(self):
        return f"Shape({self.profile!r})"


HUNTINGTON = Shape("huntington", Account.Type.CHECKING, _huntington, payee_memo)
CAPITAL_ONE = Shape("capital_one", Account.Type.CREDIT_CARD, _capital_one, lambda row: row.payee)
APPLE_CARD = Shape("apple_card", Account.Type.CREDIT_CARD, _apple_card, lambda row: row.payee)
OFX = Shape("ofx", Account.Type.CHECKING, _ofx, payee_memo)
SHAPES = (HUNTINGTON, CAPITAL_ONE, APPLE_CARD, OFX)

shapes = st.sampled_from(SHAPES)

# Plain words with a little CSV-hostile punctuation (commas, quotes) and an
# SGML entity character, never a leading formula character or digit.
_word = st.from_regex(r"[A-Z][A-Z&,.'\"/]{0,7}", fullmatch=True)
descriptions = st.lists(_word, min_size=1, max_size=3).map(lambda words: "SYN " + " ".join(words))
memos = st.one_of(st.just(""), descriptions)
days = st.dates(EARLIEST, LATEST)
amounts = st.one_of(
    st.integers(1, 99_999), st.integers(1, MAX_MINOR)
).flatmap(lambda size: st.sampled_from((size, -size)))


@st.composite
def rows(draw, *, min_size=1, max_size=8, unique=False, day_strategy=days, amount_strategy=amounts):
    """A list of rows with distinct source ids; `unique` also makes every description distinct."""
    count = draw(st.integers(min_size, max_size))
    result = []
    for index in range(count):
        payee = draw(descriptions)
        if unique:
            payee = f"{payee} N{index}"
        result.append(Row(draw(day_strategy), draw(amount_strategy), payee, draw(memos), f"SYN-{index:05d}"))
    return result


@st.composite
def overlapping_files(draw, pool, *, min_files=2, max_files=4):
    """Files drawn from a row pool: rows may repeat within a file and across files."""
    indexes = st.lists(st.integers(0, len(pool) - 1), min_size=1, max_size=len(pool) + 2)
    return [[pool[i] for i in file] for file in draw(st.lists(indexes, min_size=min_files, max_size=max_files))]


def simplefin_epoch(day):
    """Noon UTC on the day, which is the same calendar day in the app's US time zone."""
    return int(datetime.combine(day, time(12), tzinfo=timezone.utc).timestamp())


def simplefin_payload(items):
    """A SimpleFIN /accounts payload for one synthetic checking account."""
    return {
        "errlist": [],
        "connections": [
            {"conn_id": "CON-1", "name": "Synthetic Bank", "org_id": "ORG-1",
             "sfin_url": "https://bank.example.test/simplefin"},
        ],
        "accounts": [
            {
                "id": "sf-checking",
                "name": "Synthetic Checking",
                "conn_id": "CON-1",
                "currency": "USD",
                "balance": "0.00",
                "balance-date": simplefin_epoch(LATEST),
                "transactions": [
                    {"id": f"SYN-SF-{key}", "posted": simplefin_epoch(row.day),
                     "amount": decimal_text(row.amount_minor), "description": row.payee}
                    for key, row in items
                ],
                "extra": {},
            }
        ],
    }


def shifted(day, offset):
    return day + timedelta(days=offset)
