"""Blank records in spreadsheet CSVs are skipped the same way everywhere (#674).

Spreadsheet exports end with blank lines or delimiter-only rows. The month-total
sheet reader and the transaction importer must agree on which records are blank,
and a blank record must never change a total or count toward a row limit. A
generated sheet with blank records inserted anywhere is compared with the same
sheet without them. The class of bug is one reader refusing or counting a blank
record that the other skips, which these properties catch whichever reader drifts.
"""

from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finance.csv_import.parser import read_csv
from finance.sheet_comparison import parsed_month_totals, read_sheet_csv

pytestmark = pytest.mark.property

MAPPING = SimpleNamespace(
    month_column="Month",
    income_column="Income",
    spending_column="Spending",
    spending_sign="unsigned",
)
HEADER = b"Month,Income,Spending\n"
# Records a spreadsheet export can leave behind: empty, delimiter-only and
# whitespace-only lines, and a quoted empty cell.
BLANK_RECORDS = st.sampled_from([b"", b",,", b" , ", b",  ,,", b'""', b"\t", b" "])
MONTHS = st.lists(
    st.tuples(
        st.integers(2000, 2030),
        st.integers(1, 12),
        st.integers(0, 999_999),
        st.integers(0, 999_999),
    ),
    min_size=1,
    max_size=8,
    unique_by=lambda item: (item[0], item[1]),
)
INSERTS = st.lists(st.tuples(st.integers(0, 8), BLANK_RECORDS), max_size=12)


def _month_lines(months):
    return [
        f"{year}-{month:02d},{income // 100}.{income % 100:02d},{spending // 100}.{spending % 100:02d}".encode()
        for year, month, income, spending in months
    ]


def _with_blanks(lines, inserts):
    out = list(lines)
    for position, blank in sorted(inserts, key=lambda item: item[0], reverse=True):
        out.insert(min(position, len(out)), blank)
    return out


def _sheet(lines):
    return HEADER + b"\n".join(lines) + b"\n"


@given(months=MONTHS, inserts=INSERTS)
def test_blank_records_never_change_sheet_totals(months, inserts):
    clean_lines = _month_lines(months)
    _headers, clean_rows = read_sheet_csv(_sheet(clean_lines))
    _headers, noisy_rows = read_sheet_csv(_sheet(_with_blanks(clean_lines, inserts)))

    assert noisy_rows == clean_rows
    assert parsed_month_totals(noisy_rows, MAPPING) == parsed_month_totals(clean_rows, MAPPING)


@given(months=MONTHS, inserts=INSERTS)
def test_sheet_and_transaction_readers_skip_the_same_blank_records(months, inserts):
    lines = _with_blanks(_month_lines(months), inserts)
    expected_cells = [tuple(line.decode().split(",")) for line in _month_lines(months)]

    _headers, sheet_rows = read_sheet_csv(_sheet(lines))
    document = read_csv(_sheet(lines))

    assert len(sheet_rows) == len(months)
    assert len(document.rows) == len(months)
    assert [row.cells for row in document.rows] == expected_cells
