import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation


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


@dataclass(frozen=True)
class PreviewRow:
    row_number: int
    transaction_date: object | None
    description: str
    amount_minor: int | None
    currency: str
    errors: tuple[str, ...]

    @property
    def is_valid(self):
        return not self.errors

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


def read_csv(content: bytes, *, max_rows=MAX_DATA_ROWS) -> CsvDocument:
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

    try:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;").delimiter
        except csv.Error:
            delimiter = None
        candidates = []
        for candidate in (",", ";"):
            candidate_headers = next(
                csv.reader(io.StringIO(text, newline=""), delimiter=candidate, strict=True)
            )
            candidates.append((len(candidate_headers), candidate, candidate_headers))
        if delimiter:
            column_count, _candidate, headers = next(item for item in candidates if item[1] == delimiter)
        else:
            column_count, delimiter, headers = max(candidates, key=lambda item: item[0])
        if column_count < 2:
            raise csv.Error
        records = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        next(records)
    except (csv.Error, StopIteration) as exc:
        raise CsvInputError("The file is not a valid comma- or semicolon-delimited CSV.") from exc

    cleaned_headers = tuple(header.strip() for header in headers)
    if not cleaned_headers or any(not header for header in cleaned_headers):
        raise CsvInputError("Every CSV column must have a header.")
    if len(set(cleaned_headers)) != len(cleaned_headers):
        raise CsvInputError("CSV column headers must be unique.")

    rows = []
    try:
        for number, cells in enumerate(records, start=2):
            if len(rows) >= max_rows:
                raise CsvInputError(f"The CSV file exceeds the {max_rows:,} row limit.")
            structural_error = None
            if len(cells) != len(cleaned_headers):
                structural_error = "The row has a different number of columns than the header."
            rows.append(CsvRow(number, tuple(cells), structural_error))
    except csv.Error as exc:
        raise CsvInputError("The CSV contains malformed quoting.") from exc
    return CsvDocument(cleaned_headers, tuple(rows))


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


def preview_csv(document: CsvDocument, mapping: Mapping) -> Preview:
    if mapping.date_format not in DATE_FORMATS or mapping.number_format not in NUMBER_FORMATS:
        raise CsvInputError("Choose supported date and number formats.")
    if mapping.amount_mode not in ("signed", "separate"):
        raise CsvInputError("Choose a supported amount mapping.")

    required = [mapping.date_column, mapping.description_column]
    if mapping.amount_mode == "signed":
        required.append(mapping.amount_column)
    else:
        required.extend((mapping.debit_column, mapping.credit_column))
    if mapping.currency_column:
        required.append(mapping.currency_column)
    if any(column not in document.headers for column in required):
        raise CsvInputError("One or more mapped columns are not present in the CSV.")

    indexes = {header: index for index, header in enumerate(document.headers)}
    date_pattern = DATE_FORMATS[mapping.date_format][1]
    preview_rows = []
    for row in document.rows:
        errors = []
        if row.structural_error:
            errors.append(row.structural_error)

        parsed_date = None
        try:
            parsed_date = datetime.strptime(_cell(row, indexes, mapping.date_column).strip(), date_pattern).date()
        except ValueError:
            errors.append("Date does not match the selected format.")

        description = _safe_description(_cell(row, indexes, mapping.description_column))
        if not description.strip():
            errors.append("Description is required.")

        amount_minor = None
        try:
            if mapping.amount_mode == "signed":
                amount_minor = _parse_money(_cell(row, indexes, mapping.amount_column), mapping.number_format)
                if mapping.invert_sign:
                    amount_minor = -amount_minor
            else:
                debit = _cell(row, indexes, mapping.debit_column).strip()
                credit = _cell(row, indexes, mapping.credit_column).strip()
                if bool(debit) == bool(credit):
                    raise ValueError
                amount_minor = _parse_money(debit or credit, mapping.number_format)
                if amount_minor < 0:
                    raise ValueError
                if debit:
                    amount_minor = -amount_minor
        except OverflowError:
            errors.append("Amount is outside the supported range.")
        except ValueError:
            errors.append("Amount is not valid for the selected mapping and number format.")

        currency = "USD"
        if mapping.currency_column:
            currency = _cell(row, indexes, mapping.currency_column).strip().upper()
            if currency != "USD":
                errors.append("Currency must be USD.")

        preview_rows.append(
            PreviewRow(row.number, parsed_date, description, amount_minor, currency, tuple(errors))
        )
    return Preview(tuple(preview_rows))
