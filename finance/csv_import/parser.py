import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from finance.date_bounds import activity_date_error


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DATA_ROWS = 10_000
MAX_BIGINT = 2**63 - 1

DATE_FORMATS = {
    "mdy_slash_4": ("MM/DD/YYYY", "%m/%d/%Y"),
    "mdy_slash_2": ("MM/DD/YY", "%m/%d/%y"),
    "dmy_slash_4": ("DD/MM/YYYY", "%d/%m/%Y"),
    "iso": ("YYYY-MM-DD", "%Y-%m-%d"),
}

NUMBER_FORMATS = {
    "dot_comma": ("Decimal point, comma thousands (1,234.56)", ".", ","),
    "comma_dot": ("Decimal comma, point thousands (1.234,56)", ",", "."),
    "dot_none": ("Decimal point, no thousands (1234.56)", ".", None),
    "comma_none": ("Decimal comma, no thousands (1234,56)", ",", None),
}


class CsvInputError(ValueError):
    """A safe, user-facing error that never contains uploaded cell data."""


@dataclass(frozen=True)
class CsvRow:
    number: int
    cells: tuple[str, ...]
    structural_error: str | None = None


@dataclass(frozen=True)
class CsvDocument:
    headers: tuple[str, ...]
    rows: tuple[CsvRow, ...]


@dataclass(frozen=True)
class Mapping:
    date_column: str
    description_column: str
    date_format: str
    number_format: str
    amount_mode: str
    amount_column: str = ""
    debit_column: str = ""
    credit_column: str = ""
    currency_column: str = ""
    invert_sign: bool = False
    description_mode: str = "column"
    payee_column: str = ""
    memo_column: str = ""
    source_id_column: str = ""
    excluded_original_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewRow:
    row_number: int
    transaction_date: object | None
    description: str
    amount_minor: int | None
    currency: str
    errors: tuple[str, ...]
    source_transaction_id: str = ""
    overlap_status: str | None = None

    @property
    def is_valid(self):
        return not self.errors

    def classified(self, overlap_status):
        return PreviewRow(
            self.row_number,
            self.transaction_date,
            self.description,
            self.amount_minor,
            self.currency,
            self.errors,
            self.source_transaction_id,
            overlap_status,
        )

    @property
    def amount_display(self):
        if self.amount_minor is None:
            return ""
        sign = "-" if self.amount_minor < 0 else ""
        absolute = abs(self.amount_minor)
        return f"{sign}{absolute // 100}.{absolute % 100:02d}"


@dataclass(frozen=True)
class Preview:
    rows: tuple[PreviewRow, ...]

    @property
    def valid_count(self):
        return sum(row.is_valid for row in self.rows)

    @property
    def invalid_count(self):
        return len(self.rows) - self.valid_count

    @property
    def new_count(self):
        return sum(row.overlap_status == "new" for row in self.rows)

    @property
    def duplicate_count(self):
        return sum(row.overlap_status == "duplicate" for row in self.rows)


def _decode(content: bytes) -> str:
    if not content:
        raise CsvInputError("The CSV file is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise CsvInputError("The CSV file exceeds the 5 MB limit.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvInputError("The CSV file must use UTF-8 encoding.") from exc
    if not text.strip():
        raise CsvInputError("The CSV file is empty.")
    return text


def _choose_delimiter(text):
    """Return (delimiter, raw_headers), preferring the sniffed delimiter.

    A strict reader raises on a header row that the wrong delimiter cannot
    parse (for example quoted headers under ";"), so each candidate is tried
    on its own and only the ones that parse are considered.
    """
    try:
        sniffed = csv.Sniffer().sniff(text[:8192], delimiters=",;").delimiter
    except csv.Error:
        sniffed = None
    usable = []
    for candidate in (",", ";"):
        try:
            headers = next(csv.reader(io.StringIO(text, newline=""), delimiter=candidate, strict=True))
        except csv.Error:
            continue
        if len(headers) >= 2:
            usable.append((len(headers), candidate, headers))
    if not usable:
        raise csv.Error
    preferred = next((item for item in usable if item[1] == sniffed), None)
    _count, delimiter, headers = preferred or max(usable, key=lambda item: item[0])
    return delimiter, headers


def _open_records(text):
    try:
        delimiter, headers = _choose_delimiter(text)
        records = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        next(records)
    except (csv.Error, StopIteration) as exc:
        raise CsvInputError("The file is not a valid comma- or semicolon-delimited CSV.") from exc
    return headers, records


def _clean_headers(headers):
    cleaned = tuple(header.strip() for header in headers)
    if not cleaned or any(not header for header in cleaned):
        raise CsvInputError("Every CSV column must have a header.")
    if len(set(cleaned)) != len(cleaned):
        raise CsvInputError("CSV column headers must be unique.")
    return cleaned


def _collect_rows(records, column_count, max_rows):
    rows = []
    try:
        for number, cells in enumerate(records, start=2):
            if len(rows) >= max_rows:
                raise CsvInputError(f"The CSV file exceeds the {max_rows:,} row limit.")
            structural_error = None
            if len(cells) != column_count:
                structural_error = "The row has a different number of columns than the header."
            rows.append(CsvRow(number, tuple(cells), structural_error))
    except csv.Error as exc:
        raise CsvInputError("The CSV contains malformed quoting.") from exc
    return tuple(rows)


def read_csv(content: bytes, *, max_rows=MAX_DATA_ROWS) -> CsvDocument:
    headers, records = _open_records(_decode(content))
    cleaned_headers = _clean_headers(headers)
    return CsvDocument(cleaned_headers, _collect_rows(records, len(cleaned_headers), max_rows))


def _parse_money(value, number_format):
    _label, decimal_separator, thousands_separator = NUMBER_FORMATS[number_format]
    normalized = value.strip()
    if not normalized:
        raise ValueError
    decimal_pattern = re.escape(decimal_separator)
    if thousands_separator:
        thousands_pattern = re.escape(thousands_separator)
        integer_pattern = rf"(?:\d+|\d{{1,3}}(?:{thousands_pattern}\d{{3}})+)"
    else:
        integer_pattern = r"\d+"
    if not re.fullmatch(rf"[+-]?{integer_pattern}(?:{decimal_pattern}\d{{1,2}})?", normalized):
        raise ValueError
    if thousands_separator:
        normalized = normalized.replace(thousands_separator, "")
    normalized = normalized.replace(decimal_separator, ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError from exc
    if not amount.is_finite():
        raise ValueError
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError
    minor_int = int(minor)
    if abs(minor_int) > MAX_BIGINT:
        raise OverflowError
    return minor_int


def _safe_description(value):
    # Keep previews and any future spreadsheet export from interpreting text
    # as a formula. The original source remains untouched in the staged file.
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _cell(row, indexes, column):
    index = indexes[column]
    return row.cells[index] if index < len(row.cells) else ""


def _required_columns(mapping):
    if mapping.description_mode == "payee_memo":
        required = [mapping.date_column, mapping.payee_column, mapping.memo_column]
    else:
        required = [mapping.date_column, mapping.description_column]
    if mapping.amount_mode == "signed":
        required.append(mapping.amount_column)
    else:
        required.extend((mapping.debit_column, mapping.credit_column))
    if mapping.currency_column:
        required.append(mapping.currency_column)
    if mapping.source_id_column:
        required.append(mapping.source_id_column)
    return required


def _validate_mapping(document, mapping):
    if mapping.date_format not in DATE_FORMATS or mapping.number_format not in NUMBER_FORMATS:
        raise CsvInputError("Choose supported date and number formats.")
    if mapping.amount_mode not in ("signed", "separate"):
        raise CsvInputError("Choose a supported amount mapping.")
    if mapping.description_mode not in ("column", "payee_memo"):
        raise CsvInputError("Choose a supported description mapping.")
    if mapping.description_mode == "payee_memo" and not (mapping.payee_column and mapping.memo_column):
        raise CsvInputError("Choose the payee and memo columns.")
    if any(column not in document.headers for column in _required_columns(mapping)):
        raise CsvInputError("One or more mapped columns are not present in the CSV.")


def _parse_date(row, indexes, mapping):
    """Return (date, error); exactly one of them is None."""
    pattern = DATE_FORMATS[mapping.date_format][1]
    try:
        parsed = datetime.strptime(_cell(row, indexes, mapping.date_column).strip(), pattern).date()
    except ValueError:
        return None, "Date does not match the selected format."
    error = activity_date_error(parsed)
    return (None, error) if error else (parsed, None)


def _signed_amount(row, indexes, mapping):
    amount_minor = _parse_money(_cell(row, indexes, mapping.amount_column), mapping.number_format)
    return -amount_minor if mapping.invert_sign else amount_minor


def _separate_amount(row, indexes, mapping):
    debit = _cell(row, indexes, mapping.debit_column).strip()
    credit = _cell(row, indexes, mapping.credit_column).strip()
    if bool(debit) == bool(credit):
        raise ValueError
    amount_minor = _parse_money(debit or credit, mapping.number_format)
    if amount_minor < 0:
        raise ValueError
    return -amount_minor if debit else amount_minor


def _parse_amount(row, indexes, mapping):
    """Return (amount_minor, error); exactly one of them is None."""
    parser = _signed_amount if mapping.amount_mode == "signed" else _separate_amount
    try:
        return parser(row, indexes, mapping), None
    except OverflowError:
        return None, "Amount is outside the supported range."
    except ValueError:
        return None, "Amount is not valid for the selected mapping and number format."


def _parse_currency(row, indexes, mapping):
    """Return (currency, error). Only USD is supported in v1."""
    if not mapping.currency_column:
        return "USD", None
    currency = _cell(row, indexes, mapping.currency_column).strip().upper()
    return currency, (None if currency == "USD" else "Currency must be USD.")


def _join_payee_memo(payee, memo):
    payee = payee.strip()
    memo = memo.strip()
    if payee and memo:
        return f"{payee} - {memo}"
    return payee or memo


def _description(row, indexes, mapping):
    if mapping.description_mode == "payee_memo":
        raw = _join_payee_memo(
            _cell(row, indexes, mapping.payee_column),
            _cell(row, indexes, mapping.memo_column),
        )
    else:
        raw = _cell(row, indexes, mapping.description_column)
    return _safe_description(raw)


def _source_transaction_id(row, indexes, mapping):
    if not mapping.source_id_column:
        return ""
    return _cell(row, indexes, mapping.source_id_column).strip()


def _preview_row(row, indexes, mapping):
    parsed_date, date_error = _parse_date(row, indexes, mapping)
    description = _description(row, indexes, mapping)
    amount_minor, amount_error = _parse_amount(row, indexes, mapping)
    currency, currency_error = _parse_currency(row, indexes, mapping)
    errors = (
        row.structural_error,
        date_error,
        None if description.strip() else "Description is required.",
        amount_error,
        currency_error,
    )
    return PreviewRow(
        row.number,
        parsed_date,
        description,
        amount_minor,
        currency,
        tuple(error for error in errors if error),
        _source_transaction_id(row, indexes, mapping),
    )


def preview_csv(document: CsvDocument, mapping: Mapping) -> Preview:
    _validate_mapping(document, mapping)
    indexes = {header: index for index, header in enumerate(document.headers)}
    return Preview(tuple(_preview_row(row, indexes, mapping) for row in document.rows))
