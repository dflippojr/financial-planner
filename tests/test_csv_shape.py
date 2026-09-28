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


def test_a_preamble_line_is_never_mistaken_for_the_header():
    # A full-width, label-like preamble row (here a holder's name) sits above the
    # real header. Taking the first such row as the header printed it verbatim
    # and exempted it from the leak guard.
    raw = (
        "Jamie Q Example,Checking account,USD\n"
        "Date,Description,Amount\n"
        "2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n"
        "2026-09-28,SYNTHETIC BOOK STORE,-3.25\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert "Jamie Q Example" not in report
    assert "Checking account" not in report
    assert "header row: line 2" in report
    assert "- Amount: money" in report


def test_a_headerless_file_with_a_label_like_first_row_shows_no_labels():
    raw = (
        "Jamie Q Example,Checking account,USD\n"
        "2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n"
        "2026-09-28,SYNTHETIC BOOK STORE,-3.25\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert "Jamie Q Example" not in report
    assert "Checking account" not in report
    assert "masked:" in report


@pytest.mark.parametrize(
    "amounts",
    [
        ("$-12.50", "$-3.25"),
        ("-$12.50", "-$3.25"),
        ("($12.50)", "($3.25)"),
        ("-12.50", "-3.25"),
        ("12.50-", "3.25-"),
    ],
)
def test_negative_amounts_are_counted_however_the_sign_is_written(amounts):
    raw = ("Date,Memo,Amount\n" + "".join(f"2026-09-{27 + i},x,{amount}\n" for i, amount in enumerate(amounts))).encode()

    report = csv_shape.describe_csv(raw)

    assert "negative values: 2 of 2" in report


def test_positive_amounts_are_not_counted_as_negative():
    raw = b"Date,Memo,Amount\n2026-09-27,x,$12.50\n2026-09-28,x,3.25\n"

    report = csv_shape.describe_csv(raw)

    assert "negative values: 0 of 2" in report


def test_rows_from_another_section_are_described_masked():
    raw = (
        "Date,Type,Amount\n"
        "2026-09-27,Buy,-10.00\n"
        "2026-09-28,Sell,20.00\n"
        "Total holdings SYNTHETIC FUND 4111111111111111\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert "SYNTHETIC FUND" not in report
    assert "4111111111111111" not in report
    assert "rows with 1 columns (masked, first 1)" in report


def test_mask_headers_masks_labels_too():
    report = csv_shape.describe_csv(BANK_STYLE, mask_headers=True)

    assert "Posting Date" not in report
    assert "masked" in report


def test_command_line_accepts_mask_headers(tmp_path, capsys):
    path = tmp_path / "export.csv"
    path.write_bytes(BANK_STYLE)

    exit_code = csv_shape.main([str(path), "--mask-headers"])

    assert exit_code == 0
    assert "Posting Date" not in capsys.readouterr().out


def test_unusual_header_words_are_masked_unless_headers_are_trusted():
    raw = b"Date,Frobnicate Level,Amount\n2026-09-27,x,1.00\n2026-09-28,y,2.00\n"

    default = csv_shape.describe_csv(raw)
    trusted = csv_shape.describe_csv(raw, trust_headers=True)

    assert "Frobnicate" not in default
    assert "masked" in default
    assert "Frobnicate Level" in trusted


@pytest.mark.parametrize(
    "header",
    [
        "Trade Date,Settlement Date,Transaction Type,Transaction Description,Investment Name,Symbol,Shares,Share Price,Principal Amount,Commissions and Fees,Net Amount,Accrued Interest,Account Number",
        "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By",
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit",
        "Date,Description,Check Number,Amount,Balance",
    ],
)
def test_typical_bank_and_brokerage_headers_are_readable_by_default(header):
    width = len(header.split(","))
    row = ",".join(["2026-09-27"] + ["x"] * (width - 1))
    raw = f"{header}\n{row}\n{row}\n".encode()

    report = csv_shape.describe_csv(raw)

    assert "masked:" not in report.split("columns:")[1].split("separate debit")[0].replace("(masked", "")
    for label in header.split(","):
        assert label in report


def test_a_names_words_are_not_generic_labels():
    assert csv_shape.is_generic_label("Jamie Q Example") is False
    assert csv_shape.is_generic_label("Checking account") is False
    assert csv_shape.is_generic_label("Transaction Type") is True


def test_show_values_can_select_a_masked_column_by_the_header_text_or_position():
    raw = b"Date,Frobnicate Level,Amount\n2026-09-27,Alpha,1.00\n2026-09-28,Beta,2.00\n"

    by_text = csv_shape.describe_csv(raw, show=["frobnicate level"])
    by_position = csv_shape.describe_csv(raw, show=["col 2"])

    assert "Alpha; Beta" in by_text
    assert "Alpha; Beta" in by_position
    assert "Frobnicate" not in by_text


@pytest.mark.parametrize(
    "value",
    ["debit", "credit", "money", "text", "date", "empty", "patterns", "columns", "9999", "AAAA", "delimiter"],
)
def test_cell_values_that_match_the_reports_own_wording_do_not_break_it(value):
    # The report contains fixed words such as "debit/credit" and "money"; an
    # ordinary cell with the same text used to make the guard refuse the file.
    raw = f"Date,Memo,Amount\n2026-09-27,{value},-1.00\n2026-09-28,{value},2.00\n".encode()

    report = csv_shape.describe_csv(raw)

    assert "- Memo:" in report


def test_a_cell_that_is_also_a_word_in_a_printed_header_does_not_break_it():
    raw = b"Date,Type,Commissions and Fees\n2026-09-27,Fees,-1.00\n2026-09-28,Fees,2.00\n"

    report = csv_shape.describe_csv(raw)

    assert "Commissions and Fees" in report


def test_a_row_is_printed_as_written_only_if_every_cell_is_a_generic_label():
    # "Price" alone is ordinary column vocabulary, but next to "Checking account"
    # this is a preamble line, so no part of it may print.
    raw = (
        "Price,Checking account,USD\n"
        "2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n"
        "2026-09-28,SYNTHETIC BOOK STORE,-3.25\n"
    ).encode()

    report = csv_shape.describe_csv(raw)

    assert "Price" not in report
    assert "Checking" not in report
    assert "masked:" in report


def test_the_guard_still_refuses_when_masking_is_broken(monkeypatch):
    # The guard exists for the day masking has a bug, so it must not rely on it.
    monkeypatch.setattr(csv_shape, "mask", lambda value: value)
    raw = b"Date,Merchant,Amount\n2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n2026-09-28,SYNTHETIC BOOK STORE,-3.25\n"

    with pytest.raises(csv_shape.LeakError):
        csv_shape.describe_csv(raw)


def test_only_tagged_text_is_searched_when_asked():
    rows = [["Date", "Note"], ["2026-09-27", "money"]]
    report = "kind: money\n" + csv_shape.data_text("patterns: aaaaa")

    csv_shape.check_no_leak(report, rows, allowed={"Date", "Note"}, dynamic_only=True)


def test_shape_only_values_are_recognized_without_using_the_mask():
    assert csv_shape.is_shape_only("9999") is True
    assert csv_shape.is_shape_only("Aaaa 99/99") is True
    assert csv_shape.is_shape_only("9{16}") is True
    assert csv_shape.is_shape_only("4111111111111111") is False
    assert csv_shape.is_shape_only("Jamie") is False


def test_tag_characters_in_a_cell_cannot_confuse_the_guard():
    raw = b"Date,Memo,Amount\n2026-09-27,\x00SYNTHETIC COFFEE HOUSE\x01,-1.00\n2026-09-28,x,2.00\n"

    report = csv_shape.describe_csv(raw)

    assert "SYNTHETIC" not in report
    assert "\x00" not in report
    assert "\x01" not in report


def test_utf16_exports_are_decoded_by_their_byte_order_mark():
    # Excel's "Unicode text" export is UTF-16, which contains NUL bytes that
    # used to crash the csv module.
    text = "Date,Memo,Amount\n2026-09-27,SYNTHETIC COFFEE HOUSE,-12.50\n2026-09-28,x,2.00\n"

    report = csv_shape.describe_csv(text.encode("utf-16"))

    assert "encoding: utf-16 (BOM)" in report
    assert "SYNTHETIC" not in report
    assert "- Amount: money" in report


def test_a_nul_byte_in_the_middle_of_a_file_does_not_crash_the_tool():
    raw = b"Date,Memo,Amount\n2026-09-27,ab\x00cd,-1.00\n2026-09-28,x,2.00\n"

    report = csv_shape.describe_csv(raw)

    assert "- Amount: money" in report
