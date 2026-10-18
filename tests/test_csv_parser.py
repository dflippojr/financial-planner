import pytest

from finance.csv_import.parser import CsvInputError, Mapping, preview_csv, read_csv


def mapping(**changes):
    values = {
        "date_column": "When",
        "description_column": "Memo",
        "date_format": "mdy_slash_4",
        "number_format": "dot_comma",
        "amount_mode": "signed",
        "amount_column": "Amount",
    }
    values.update(changes)
    return Mapping(**values)


def test_signed_amount_preview_is_exact_and_can_invert_sign():
    document = read_csv(b'When,Memo,Amount\n09/27/2026,SYNTHETIC GROCER,"1,234.56"\n')

    result = preview_csv(document, mapping(invert_sign=True))

    assert result.valid_count == 1
    assert result.rows[0].amount_minor == -123456
    assert result.rows[0].amount_display == "-1234.56"


@pytest.mark.parametrize(
    ("date_format", "date_value"),
    [
        ("mdy_slash_4", "09/27/2026"),
        ("mdy_slash_2", "09/27/26"),
        ("dmy_slash_4", "27/09/2026"),
        ("iso", "2026-09-27"),
    ],
)
def test_every_date_format(date_format, date_value):
    result = preview_csv(
        read_csv(f"When,Memo,Amount\n{date_value},SYNTHETIC ITEM,1.00\n".encode()),
        mapping(date_format=date_format),
    )
    assert result.valid_count == 1


@pytest.mark.parametrize(
    ("number_format", "value"),
    [
        ("dot_comma", '"1,234.56"'),
        ("comma_dot", "1.234,56"),
        ("dot_none", "1234.56"),
        ("comma_none", "1234,56"),
    ],
)
def test_every_number_format(number_format, value):
    delimiter = ";" if "," in value else ","
    content = f"When{delimiter}Memo{delimiter}Amount\n09/27/2026{delimiter}SYNTHETIC ITEM{delimiter}{value}\n"
    result = preview_csv(read_csv(content.encode()), mapping(number_format=number_format))
    assert result.rows[0].amount_minor == 123456


def test_separate_debit_and_credit_columns_apply_stored_sign():
    document = read_csv(
        b"When,Memo,Debit,Credit\n09/27/2026,SYNTHETIC FOOD,12.34,\n09/28/2026,SYNTHETIC PAY,,50.00\n"
    )
    result = preview_csv(
        document,
        mapping(amount_mode="separate", amount_column="", debit_column="Debit", credit_column="Credit"),
    )
    assert [row.amount_minor for row in result.rows] == [-1234, 5000]


def test_row_errors_do_not_echo_values_and_non_usd_is_rejected():
    secret = "PRIVATE-SOURCE-VALUE"
    document = read_csv(
        f"When,Memo,Amount,Currency\nnot-a-date,{secret},not-money,EUR\n".encode()
    )
    result = preview_csv(document, mapping(currency_column="Currency"))
    row = result.rows[0]
    assert result.invalid_count == 1
    assert len(row.errors) == 3
    assert secret not in " ".join(row.errors)
    assert "not-a-date" not in " ".join(row.errors)
    assert "not-money" not in " ".join(row.errors)


def test_formula_like_description_is_kept_raw():
    document = read_csv(b"When,Memo,Amount\n09/27/2026,=2+2,1.00\n")
    assert preview_csv(document, mapping()).rows[0].description == "=2+2"


def test_utf8_bom_semicolon_quoted_newline_and_negative_zero():
    document = read_csv(
        "\ufeffWhen;Memo;Amount\n09/27/2026;\"SYNTHETIC LINE 1\nLINE 2\";-0.00\n".encode()
    )
    result = preview_csv(document, mapping(number_format="dot_none"))
    assert result.valid_count == 1
    assert result.rows[0].amount_minor == 0
    assert "LINE 2" in result.rows[0].description


def test_semicolon_delimiter_wins_when_a_quoted_header_contains_a_comma():
    document = read_csv(
        b'When;"Memo, full";Amount\n09/27/2026;SYNTHETIC ITEM;12.34\n'
    )
    result = preview_csv(document, mapping(description_column="Memo, full", number_format="dot_none"))
    assert result.valid_count == 1


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (b"\xff\xfe", "UTF-8"),
        (b"A,B\n1,2\n\"unterminated", "malformed quoting"),
        (b"A||B\n1||2\n", "comma- or semicolon-delimited"),
    ],
)
def test_malformed_files_get_safe_errors(content, message):
    with pytest.raises(CsvInputError, match=message):
        read_csv(content)


def test_header_only_file_has_zero_valid_and_invalid_rows():
    result = preview_csv(read_csv(b"When,Memo,Amount\n"), mapping())
    assert (result.valid_count, result.invalid_count) == (0, 0)


def test_ragged_rows_are_invalid_without_crashing():
    result = preview_csv(read_csv(b"When,Memo,Amount\n09/27/2026,SYNTHETIC\n"), mapping())
    assert result.invalid_count == 1
    assert "different number of columns" in result.rows[0].errors[0]


@pytest.mark.parametrize("content", [
    b"When,Memo,Amount\n09/16/2026,SYNTHETIC A,-1.00\n\n09/17/2026,SYNTHETIC B,-2.00\n",
    b"When,Memo,Amount\n09/16/2026,SYNTHETIC A,-1.00\n09/17/2026,SYNTHETIC B,-2.00\n\n",
    b"When,Memo,Amount\r\n09/16/2026,SYNTHETIC A,-1.00\r\n09/17/2026,SYNTHETIC B,-2.00\r\n\r\n",
    b"When,Memo,Amount\n09/16/2026,SYNTHETIC A,-1.00\n,,\n09/17/2026,SYNTHETIC B,-2.00\n",
    b"When,Memo,Amount\n09/16/2026,SYNTHETIC A,-1.00\n , ,\n09/17/2026,SYNTHETIC B,-2.00\n",
])
def test_empty_and_delimiter_only_records_are_skipped(content):
    result = preview_csv(read_csv(content), mapping())
    assert (result.valid_count, result.invalid_count) == (2, 0)
    assert result.rows[0].row_number == 2


def test_rows_after_a_skipped_record_keep_their_file_line():
    content = b"When,Memo,Amount\n\n09/16/2026,SYNTHETIC A,-1.00\n,,\n09/17/2026,SYNTHETIC B\n"
    result = preview_csv(read_csv(content), mapping())
    assert [row.row_number for row in result.rows] == [3, 5]
    assert result.rows[0].is_valid
    assert "different number of columns" in result.rows[1].errors[0]


def test_skipped_records_do_not_count_toward_the_row_cap():
    assert len(read_csv(b"When,Memo,Amount\n\n,,\n1,2,3\n\n", max_rows=1).rows) == 1


def test_row_and_file_caps_are_enforced():
    with pytest.raises(CsvInputError, match="row limit"):
        read_csv(b"When,Memo,Amount\n1,2,3\n1,2,3\n", max_rows=1)
    with pytest.raises(CsvInputError, match="5 MB"):
        read_csv(b"x" * (5 * 1024 * 1024 + 1))


@pytest.mark.parametrize("value", ["92233720368547758.08", "0.001"])
def test_amounts_outside_exact_minor_unit_range_are_invalid(value):
    result = preview_csv(
        read_csv(f"When,Memo,Amount\n09/27/2026,SYNTHETIC,{value}\n".encode()), mapping()
    )
    assert result.invalid_count == 1


@pytest.mark.parametrize("value", ["12,34.56", "1e2", "$12.34", "1.234"])
def test_invalid_grouping_notation_and_sub_cent_values_are_rejected(value):
    result = preview_csv(
        read_csv(f'When,Memo,Amount\n09/27/2026,SYNTHETIC,"{value}"\n'.encode()), mapping()
    )
    assert result.invalid_count == 1


@pytest.mark.parametrize(
    ("content", "first_row"),
    [
        (b'"When","Memo","Amount"\n"1/1/2026","x","1.00"\n', ("1/1/2026", "x", "1.00")),
        (b'"When";"Memo";"Amount"\n"1/1/2026";"x";"1,00"\n', ("1/1/2026", "x", "1,00")),
        (b"When,Memo,Amount\n1/1/2026,x,1.00\n", ("1/1/2026", "x", "1.00")),
        (b"When;Memo;Amount\n1/1/2026;x;1,00\n", ("1/1/2026", "x", "1,00")),
        # The other delimiter appears inside a quoted header; it must not win.
        (b'"When","Memo; note","Amount"\n"1/1/2026","x","1.00"\n', ("1/1/2026", "x", "1.00")),
        (b'"When";"Memo, note";"Amount"\n"1/1/2026";"x";"1,00"\n', ("1/1/2026", "x", "1,00")),
    ],
)
def test_delimiter_is_detected_for_quoted_and_unquoted_headers(content, first_row):
    document = read_csv(content)

    assert len(document.headers) == 3
    assert document.rows[0].cells == first_row
    assert document.rows[0].structural_error is None


@pytest.mark.parametrize(
    "content",
    [
        b"When,Memo,Amount\n09/27/2026,SYNTHETIC\x00GROCER,-12.34\n",
        b"When,Me\x00mo,Amount\n09/27/2026,SYNTHETIC GROCER,-12.34\n",
        b"When,Memo,Amount\n09/27/2026,SYNTHETIC GROCER,-12.34\x00\n",
    ],
    ids=["cell", "header", "amount"],
)
def test_nul_bytes_are_rejected_before_any_row_or_header_is_read(content):
    with pytest.raises(CsvInputError, match="NUL bytes"):
        read_csv(content)


def source_id_preview(source_id):
    header = "When,Memo,Amount,Reference\n"
    document = read_csv(f"{header}09/27/2026,SYNTHETIC GROCER,-1.00,{source_id}\n".encode())
    return preview_csv(document, mapping(source_id_column="Reference")).rows[0]


def test_source_id_at_the_column_limit_is_valid():
    row = source_id_preview("9" * 255)

    assert row.is_valid
    assert len(row.source_transaction_id) == 255


def test_source_id_over_the_column_limit_is_a_row_error():
    row = source_id_preview("9" * 256)

    assert not row.is_valid
    assert row.errors == ("Transaction ID exceeds the 255 character limit.",)


def test_huntington_transaction_number_over_255_chars_is_a_row_error_not_a_db_error():
    from finance.csv_import.profiles import HUNTINGTON_MAPPING

    header = "Date,Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number\n"
    content = (header + "09/27/2026,1,SYNTHETIC,MEMO,-1.00,," + "9" * 300 + "\n").encode()

    row = preview_csv(read_csv(content), HUNTINGTON_MAPPING).rows[0]

    assert not row.is_valid
