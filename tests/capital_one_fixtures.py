"""Synthetic Capital One credit card export shape (issue #37). Invented values only."""

CAPITAL_ONE_HEADER = "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit"

NATIVE_ROWS = (
    "2026-09-15,2026-09-16,1234,SYNTHETIC GROCER,Merchandise,42.15,",
    "2026-09-16,2026-09-17,1234,CAPITAL ONE MOBILE PAYMENT,Payment/Credit,,500.00",
    "2026-09-17,2026-09-18,1234,SYNTHETIC REFUND,Merchandise,,15.00",
    "2026-09-18,2026-09-19,1234,SYNTHETIC CAFE,Dining,5.25,",
    "2026-09-18,2026-09-19,1234,SYNTHETIC CAFE,Dining,5.25,",
    "2026-09-19,2026-09-20,1234,SYNTHETIC BROKEN ROW",
)

OVERLAP_ROWS = (
    "2026-09-18,2026-09-19,1234,SYNTHETIC CAFE,Dining,5.25,",
    "2026-09-18,2026-09-19,1234,SYNTHETIC CAFE,Dining,5.25,",
    "2026-09-20,2026-09-21,1234,SYNTHETIC BOOKSTORE,Merchandise,12.00,",
)


def capital_one_csv(rows):
    return ("\r\n".join((CAPITAL_ONE_HEADER, *rows)) + "\r\n").encode("utf-8")


NATIVE_CSV = capital_one_csv(NATIVE_ROWS)
OVERLAP_CSV = capital_one_csv(OVERLAP_ROWS)
