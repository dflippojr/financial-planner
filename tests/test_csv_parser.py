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


def test_formula_like_description_is_neutralized_for_display():
    document = read_csv(b"When,Memo,Amount\n09/27/2026,=2+2,1.00\n")
    assert preview_csv(document, mapping()).rows[0].description == "'=2+2"


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
