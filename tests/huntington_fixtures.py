"""Synthetic Huntington checking export shape (issue #13). Invented values only."""

HUNTINGTON_HEADER = (
    "Date,Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number"
)

NATIVE_ROWS = (
    "09/15/2026,1,SYNTHETIC GROCER,WEEKLY FOOD,-42.15,,100000000000000001",
    "09/16/2026,2,,ATM WITHDRAWAL FEE,-3.00,,100000000000000002",
    "09/17/2026,1,SYNTHETIC PAYROLL,DIRECT DEPOSIT,1500.00,,100000000000000003",
)

OVERLAP_ROWS = (
    "09/16/2026,2,,ATM WITHDRAWAL FEE,-3.00,,100000000000000002",
    "09/18/2026,1,SYNTHETIC CAFE,COFFEE,-5.25,,100000000000000004",
)


def huntington_csv(rows):
    return ("\r\n".join((HUNTINGTON_HEADER, *rows)) + "\r\n").encode("utf-8")


NATIVE_CSV = huntington_csv(NATIVE_ROWS)
OVERLAP_CSV = huntington_csv(OVERLAP_ROWS)
