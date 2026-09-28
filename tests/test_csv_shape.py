import importlib.util
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parent.parent / "scripts" / "csv_shape.py"
    spec = importlib.util.spec_from_file_location("csv_shape", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


csv_shape = _load()

# Values that must never reach the output. All are synthetic.
SECRETS = ("SYNTHETIC COFFEE HOUSE", "4111111111111111", "Jamie Q Example", "987654321", "jamie@example.invalid")

BANK_STYLE = (
    "Posting Date,Description,Debit,Credit,Balance\r\n"
    "09/27/2026,SYNTHETIC COFFEE HOUSE 4111111111111111,12.50,,\"1,234.56\"\r\n"
    "09/28/2026,PAYROLL Jamie Q Example,,2500.00,\"3,734.56\"\r\n"
    "09/29/2026,ONLINE TRANSFER jamie@example.invalid,40.00,,\"3,694.56\"\r\n"
).encode()


def assert_no_secrets(report):
    assert [secret for secret in SECRETS if secret in report] == []


def test_output_never_contains_values_and_shows_headers():
    report = csv_shape.describe_csv(BANK_STYLE, "bank export")

    assert_no_secrets(report)
    assert "Posting Date" in report
    assert "Description" in report
    assert "delimiter: ','" in report
    assert "CRLF" in report


def test_separate_debit_and_credit_columns_are_detected():
    report = csv_shape.describe_csv(BANK_STYLE)

    assert "Debit and Credit" in report


def test_signed_amount_layout_reports_negative_values_and_no_pair():
    raw = (
        "Date,Merchant,Amount\n"
        "2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n"
        "2026-09-28,PAYROLL Jamie Q Example,2500.00\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert_no_secrets(report)
    assert "negative values: 1 of 2" in report
    assert "none detected" in report


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (("27/09/2026", "01/10/2026"), "day first"),
        (("09/27/2026", "10/01/2026"), "month first"),
        (("01/02/2026", "03/04/2026"), "ambiguous"),
    ],
)
def test_day_month_order_is_inferred_from_field_maxima_only(dates, expected):
    raw = ("Date,Amount\n" + "".join(f"{date},1.00\n" for date in dates)).encode()

    report = csv_shape.describe_csv(raw)

    assert expected in report


def test_preamble_lines_are_masked_and_never_echoed():
    raw = (
        "Account: 987654321 Jamie Q Example\n"
        "Generated 2026-09-30\n"
        "\n"
        "Date,Description,Amount\n"
        "2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n"
        "2026-09-28,SYNTHETIC COFFEE HOUSE,-3.25\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert_no_secrets(report)
    assert "rows before it" in report


def test_headerless_file_masks_its_first_row():
    raw = (
        "SYNTHETIC COFFEE HOUSE,2026-09-27,-12.50\n"
        "PAYROLL Jamie Q Example,2026-09-28,2500.00\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert_no_secrets(report)
    assert "none detected" in report
    assert "first row (masked)" in report


def test_semicolon_delimited_quoted_headers_and_decimal_commas():
    raw = (
        '"Date";"Memo";"Amount"\n'
        '"27.09.2026";"SYNTHETIC COFFEE HOUSE";"-12,50"\n'
        '"28.09.2026";"PAYROLL Jamie Q Example";"2500,00"\n'
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert_no_secrets(report)
    assert "delimiter: ';'" in report
    assert "decimal comma" in report


def test_unreadable_or_odd_headers_are_masked_not_printed():
    raw = ("Date,Acct 987654321,Amount\n2026-09-27,x,1.00\n2026-09-28,y,2.00\n").encode()

    report = csv_shape.describe_csv(raw)

    assert "987654321" not in report
    assert "masked" in report


def test_masking_keeps_shape_only():
    assert csv_shape.mask("Jamie 4111-1111") == "Aaaaa 9999-9999"
    assert csv_shape.mask("4111111111111111") == "9{16}"
    assert csv_shape.mask("café 中") == "aaaa a"


def test_leak_guard_refuses_output_containing_a_value():
    rows = [["Date", "Note"], ["2026-09-27", "SYNTHETIC COFFEE HOUSE"]]

    with pytest.raises(csv_shape.LeakError):
        csv_shape.check_no_leak("profile mentions SYNTHETIC COFFEE HOUSE", rows, allowed={"Date", "Note"})


def test_leak_guard_allows_headers_that_were_printed():
    rows = [["Date", "Description"], ["2026-09-27", "x"]]

    csv_shape.check_no_leak("columns: Date, Description", rows, allowed={"Date", "Description"})


def test_command_line_reports_an_unreadable_file_without_a_traceback(tmp_path, capsys):
    exit_code = csv_shape.main([str(tmp_path / "missing.csv")])

    assert exit_code == 1
    assert "cannot read the file" in capsys.readouterr().err


def test_command_line_prints_the_report(tmp_path, capsys):
    path = tmp_path / "export.csv"
    path.write_bytes(BANK_STYLE)

    exit_code = csv_shape.main([str(path), "--label", "checking"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Shape of checking" in output
    assert_no_secrets(output)


BROKERAGE = (
    "Trade Date,Transaction Type,Description,Amount\n"
    "09/27/2026,Dividend,SYNTHETIC FUND ADMIRAL 4111111111111111,12.00\n"
    "09/28/2026,Buy,SYNTHETIC INDEX FUND,-40.00\n"
    "09/29/2026,Dividend,SYNTHETIC MONEY MARKET,3.00\n"
).encode()


def test_values_are_hidden_unless_a_column_is_requested():
    report = csv_shape.describe_csv(BROKERAGE)

    assert "Dividend" not in report
    assert "distinct values: 2" in report


def test_show_values_lists_only_the_requested_columns_vocabulary():
    report = csv_shape.describe_csv(BROKERAGE, show=["transaction type"])

    assert "Buy; Dividend" in report
    assert "SYNTHETIC" not in report
    assert "4111111111111111" not in report


def test_show_values_is_refused_when_the_column_is_not_a_plain_vocabulary():
    report = csv_shape.describe_csv(BROKERAGE, show=["Description"])

    assert "values NOT shown" in report
    assert "SYNTHETIC" not in report


def test_show_values_is_refused_for_a_column_with_too_many_distinct_values():
    rows = "".join(f"09/27/2026,Type {chr(65 + i // 26)}{chr(65 + i % 26)},1.00\n" for i in range(30))
    raw = ("Trade Date,Kind,Amount\n" + rows).encode()

    report = csv_shape.describe_csv(raw, show=["Kind"])

    assert "values NOT shown" in report


def test_show_values_reports_an_unknown_column():
    report = csv_shape.describe_csv(BROKERAGE, show=["No Such Column"])

    assert "--show-values column not found: no such column" in report


def test_command_line_accepts_show_values(tmp_path, capsys):
    path = tmp_path / "brokerage.csv"
    path.write_bytes(BROKERAGE)

    exit_code = csv_shape.main([str(path), "--show-values", "Transaction Type"])

    assert exit_code == 0
    assert "Buy; Dividend" in capsys.readouterr().out


def test_random_files_never_trigger_the_leak_guard():
    # The guard raises if any cell of 4+ characters appears in the report, so
    # a clean run over many random shapes shows the report builder itself never
    # emits values, not merely that the guard would catch them.
    import random

    generator = random.Random(20260928)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -./,$()#@"
    for _ in range(300):
        width = generator.randint(2, 6)
        header = ",".join(f"Label{chr(65 + i)}" for i in range(width))
        lines = [header]
        for _row in range(generator.randint(1, 12)):
            cells = ["".join(generator.choice(alphabet) for _ in range(generator.randint(0, 24))) for _ in range(width)]
            lines.append(",".join('"' + cell.replace('"', '""') + '"' for cell in cells))
        csv_shape.describe_csv("\n".join(lines).encode())
