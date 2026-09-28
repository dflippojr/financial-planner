#!/usr/bin/env python3
"""Describe the shape of a CSV export without printing its values.

Run this on your own machine against a real export and share only its output.
It prints the delimiter, encoding, line endings, column headers, and masked
value patterns (letters become a/A, digits become 9). It never prints a cell
value unless you ask for one column's distinct values with --show-values, and
it refuses to print anything if a check finds an unrequested value in its
own output.

    python scripts/csv_shape.py path/to/export.csv
    python scripts/csv_shape.py export.csv --show-headers
    python scripts/csv_shape.py export.csv --show-headers --show-values "Transaction Type"

Two options print text from the file, and you choose them because only you
can look at your file and know they are safe:

  --show-headers   print the header row's labels as written. Use it only after
                   opening the file and confirming its first lines are nothing
                   but column names. The tool cannot tell a header from a line
                   that names a person or account (even "Price,Account,USD"),
                   so by default every label is masked.
  --show-values C  list the distinct values of column C, for a fixed vocabulary
                   such as a transaction type. Never use it for descriptions or
                   anything that names a person, merchant, or account.

Standard library only. Review the output before sharing it.
"""
import argparse
import codecs
import csv
import io
import re
import sys
from collections import Counter

MAX_ROWS = 200_000
MAX_LISTED_PATTERNS = 4
MAX_DISTINCT_TO_LIST = 12
MAX_SHOWN_VALUES = 25
FIELD_LIMIT = 16 * 1024 * 1024
DELIMITERS = (",", ";", "\t", "|")
KEPT_PUNCTUATION = set(" /-.,$()+:;@#&%'\"*_")

# Optional time of day so a datetime stamp still counts as a date (issue #1).
_TIME = r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)?"
DATE_LIKE = re.compile(r"\d{1,4}([/.-])\d{1,2}\1\d{1,4}" + _TIME)
# Spaces and the non-breaking spaces some exports use as thousands separators are
# allowed inside an amount; a newline is not, or a multi-line text field of digits
# such as "12" newline "34" would be classified as money.
# ASCII hyphen-minus and U+2212 MINUS SIGN (spreadsheet / Excel exports).
MONEY_LIKE = re.compile(r"[-\u2212+(]?[$€£]? ?[-\u2212+(]?\d[\d,. \xa0 ]*[)\u2212-]?")
LABEL = re.compile(r"[A-Za-z][A-Za-z /$%#&()._'-]{0,39}")
SAFE_VOCABULARY = re.compile(r"[A-Za-z][A-Za-z /&()._'-]{0,39}")
LONG_RUN = re.compile(r"(.)\1{5,}")

START, END = "\x00", "\x01"


def data_text(text):
    """Tag text that came from the file so the leak guard checks it and nothing else."""
    return f"{START}{text}{END}"


class LeakError(RuntimeError):
    """Raised when the output would contain a value from the file."""


def mask(value):
    """Reduce a value to its shape: letters -> a/A, digits -> 9, long runs collapsed."""
    out = []
    for character in value:
        if character.isdigit():
            out.append("9")
        elif character.isalpha():
            out.append("A" if character.isupper() else "a")
        else:
            out.append(character if character in KEPT_PUNCTUATION else "?")
    return LONG_RUN.sub(lambda match: f"{match.group(1)}{{{len(match.group(0))}}}", "".join(out))


def _decode(raw):
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16"), "utf-16 (BOM)"
        except UnicodeDecodeError:
            pass
    bom = " with BOM" if raw.startswith(codecs.BOM_UTF8) else ""
    for encoding, label in (("utf-8-sig", "utf-8" + bom), ("cp1252", "windows-1252")):
        try:
            return raw.decode(encoding), label
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def decode_text(raw):
    """Return (text, encoding label): UTF-16 by its BOM, else UTF-8, Windows-1252, or Latin-1.

    NUL bytes make the csv module fail, and the report's own tag characters must
    not appear in the data, so all of them are removed here.
    """
    text, label = _decode(raw)
    return text.replace("\x00", "").replace(START, "").replace(END, ""), label


def line_endings(text):
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    if crlf and not lone_lf:
        return f"CRLF x{crlf}"
    if lone_lf and not crlf:
        return f"LF x{lone_lf}"
    return f"mixed (CRLF {crlf}, LF {lone_lf})"


def _plausibility(cells, delimiter):
    """Share of cells that look like a sensible field under this delimiter.

    A field is sensible if it is a date, an amount, or text that does not still
    contain one of the other candidate delimiters. A wrong split leaves stray
    delimiters inside fields (a tab inside "2026-09-27<TAB>1") and cuts amounts
    into fragments, so it scores lower than the split that yields clean columns.
    """
    others = set(DELIMITERS) - {delimiter}
    clean = sum(
        bool(DATE_LIKE.fullmatch(cell) or MONEY_LIKE.fullmatch(cell) or not (set(cell) & others))
        for cell in cells
    )
    return clean / len(cells) if cells else 0


def _delimiter_score(sample, delimiter):
    """(consistency, plausibility, width), or (0, 0, 0) if it does not split rows into 2+ columns.

    The right delimiter gives every row the same number of columns, so that comes
    first: scoring by how many fields the punctuation produces would let commas
    inside amounts such as 1,234,567.89 outvote a tab that really separates
    columns. When two delimiters are equally consistent (any file without a
    header line can be), plausibility decides, and width only breaks a further tie.
    """
    lengths = Counter()
    cells = []
    try:
        for row in _reader(sample, delimiter):
            if row:
                lengths[len(row)] += 1
                cells.extend(cell.strip() for cell in row if cell.strip())
    except csv.Error:
        pass
    if not lengths:
        return 0, 0, 0
    modal, count = max(lengths.items(), key=lambda item: (item[1], item[0]))
    if modal < 2:
        return 0, 0, 0
    # Not rounded: with hundreds of rows a single header line that the wrong
    # delimiter splits differently is a tiny fraction, and rounding it away
    # turns a clear preference for the right delimiter into a tie.
    return count / sum(lengths.values()), _plausibility(cells, delimiter), modal


def _reader(text, delimiter):
    """A csv reader over text with its line endings intact.

    The csv module needs the newline characters to keep a newline inside a
    quoted field; handing it text.splitlines() removed them (so "12", newline,
    "34" became 1234 and looked like money) and also split on Unicode line
    separators that are not row breaks.
    """
    csv.field_size_limit(FIELD_LIMIT)
    return csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)


def choose_delimiter(text):
    sample = "".join(text.splitlines(keepends=True)[:500])
    best_score, best = max(((_delimiter_score(sample, d), d) for d in DELIMITERS), key=lambda item: item[0])
    return best if best_score > (0, 0, 0) else ","


def read_rows(text, delimiter):
    """Return (rows kept, Counter of column counts for rows past the cap, warning or None).

    Only the first MAX_ROWS rows are kept for profiling, but the rest are still
    counted by column count so totals and the layout stay accurate; a differently
    shaped summary row at the end of a very long file must not vanish. If the csv
    module raises (for example a field over FIELD_LIMIT), parsing stops there and
    the warning says so, because a profile that silently omits everything after
    that row would look complete.
    """
    rows = []
    beyond = Counter()
    warning = None
    try:
        for row in _reader(text, delimiter):
            if not row:
                continue
            if len(rows) >= MAX_ROWS:
                beyond[len(row)] += 1
            else:
                rows.append(row)
    except csv.Error as error:
        warning = (
            f"warning: parsing stopped after {len(rows) + sum(beyond.values())} rows because of a CSV error "
            f"({str(error)[:60]}); the rows after that point were not read, so this profile is incomplete"
        )
    return rows, beyond, warning


def looks_like_header(row):
    """At least two labels, every non-empty cell label-like and distinct.

    Empty cells are allowed: many exports end every line with a comma, which
    gives the header an empty last cell, or start with an unnamed index column.
    """
    labels = [cell.strip() for cell in row if cell.strip()]
    return len(labels) >= 2 and all(LABEL.fullmatch(label) for label in labels) and len(set(labels)) == len(labels)


def _has_date(row, width):
    return len(row) == width and any(DATE_LIKE.fullmatch(cell.strip()) for cell in row)


def _dated_runs(rows, width):
    """(start, end) spans of consecutive full-width rows that each contain a date."""
    runs = []
    start = None
    for index, row in enumerate(rows):
        if _has_date(row, width):
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(rows)))
    return runs


def _is_filler(row, width):
    """A non-dated or ragged row that is not itself a header."""
    return not _has_date(row, width) and not (len(row) == width and looks_like_header(row))


def _dated_blocks(rows, width):
    """Dated runs merged across filler rows (opening balance, subtotal, ragged).

    A label-like full-width row between runs is a new section, not filler, so a
    preamble table and the transaction table stay separate.
    """
    runs = _dated_runs(rows, width)
    if not runs:
        return []
    blocks = [runs[0]]
    for start, end in runs[1:]:
        prev_start, prev_end = blocks[-1]
        gap = rows[prev_end:start]
        if gap and all(_is_filler(row, width) for row in gap):
            blocks[-1] = (prev_start, end)
        else:
            blocks.append((start, end))
    return blocks


def _run_has_money(rows, start, end):
    return any(
        MONEY_LIKE.fullmatch(cell.strip())
        for row in rows[start:end]
        for cell in row
        if cell.strip()
    )


def _header_before(rows, start, width):
    """Nearest full-width header above start, walking back past filler rows."""
    for index in range(start - 1, -1, -1):
        row = rows[index]
        if len(row) == width and looks_like_header(row):
            return index
        if not _is_filler(row, width):
            return None
    return None


def find_header(rows, width):
    """(header index or None, winning dated block (start, end) or None).

    The header is the nearest label-like full-width row above the main
    transaction block: dated, amount-bearing rows, with filler such as an
    opening-balance or subtotal line skipped when walking back. Rank a
    labeled, money-bearing block that continues to the end of the file
    above a longer earlier table (balance history), then any other
    labeled money-bearing block, then any labeled dated block, then the
    later start. Length is not the primary score: a short transaction
    table must beat a long preamble. A dated table with no amounts still
    counts if it is the only labeled block.
    Taking the row before the first date in the file would treat a
    same-width preamble export-date line as data and print the
    account-holder line as headers.
    """
    best = None
    best_header = None
    best_block = None
    for start, end in _dated_blocks(rows, width):
        header = _header_before(rows, start, width)
        has_label = header is not None
        is_tx = has_label and _run_has_money(rows, start, end)
        continues_to_eof = end == len(rows)
        score = (is_tx and continues_to_eof, is_tx, has_label, start)
        if best is None or score > best:
            best = score
            best_header = header
            best_block = (start, end)
    if best is None or not best[2]:
        return None, None
    return best_header, best_block


def label_for(cell, index, show_headers):
    """(printed title, True if that title is copied from the file).

    Nothing in the file distinguishes a header from a line that names a person
    or account and happens to have the same number of cells (even "Price,
    Account,USD"), and any word list of "safe" labels has a counterexample. So
    labels are masked unless the owner, who can see their own file, passes
    --show-headers. Generated titles such as "col N (no header text)" are not
    file values; tagging them would let the leak guard collide with ordinary
    cells like "text" or "header".
    """
    text = cell.strip()
    if not text:
        return f"col {index + 1} (no header text)", False
    if show_headers and LABEL.fullmatch(text):
        return text, True
    return f"col {index + 1} (masked: {mask(text)})", False


def _shown_name(name, from_file):
    """Tag only titles copied from the file so the leak guard can check them."""
    return data_text(name) if from_file else name


def classify(values):
    filled = [value for value in values if value]
    if not filled:
        return "empty"
    if sum(bool(DATE_LIKE.fullmatch(value)) for value in filled) >= 0.9 * len(filled):
        return "date"
    if sum(bool(MONEY_LIKE.fullmatch(value)) for value in filled) >= 0.9 * len(filled):
        return "money"
    return "text"


def date_order(values):
    """Say whether dd/mm or mm/dd is implied, from field maxima only."""
    firsts, seconds = [], []
    for value in values:
        match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}" + _TIME, value)
        if match:
            firsts.append(int(match.group(1)))
            seconds.append(int(match.group(2)))
    if not firsts:
        return "not a day/month/year layout"
    if max(firsts) > 12:
        return "day first (first field exceeds 12)"
    if max(seconds) > 12:
        return "month first (second field exceeds 12)"
    return "ambiguous (no field exceeds 12)"


def sign_prefix(value):
    """Everything before the first digit, where a leading sign or opening parenthesis lives."""
    return re.split(r"\d", value, maxsplit=1)[0]


def is_negative(value):
    """A minus or opening parenthesis before the digits (also after a currency symbol), or a trailing minus.

    Spreadsheet exports use U+2212 MINUS SIGN as well as ASCII '-' .
    """
    prefix = sign_prefix(value)
    return "-" in prefix or "\u2212" in prefix or "(" in prefix or value.rstrip().endswith(("-", "\u2212"))


def money_facts(values):
    filled = [value for value in values if value]
    negative = sum(is_negative(value) for value in filled)
    facts = [f"negative values: {negative} of {len(filled)}"]
    stripped = [value.rstrip(")\u2212-") for value in filled]
    if any(re.search(r"\d,\d{3}", value) for value in filled):
        facts.append("thousands separator ','")
    if any(re.search(r"\d\.\d{1,2}$", value) for value in stripped):
        facts.append("decimal point")
    elif any(re.search(r"\d,\d{1,2}$", value) for value in stripped):
        facts.append("decimal comma")
    if any(any(symbol in sign_prefix(value) for symbol in "$€£") for value in filled):
        facts.append("currency symbol present")
    if any("(" in sign_prefix(value) for value in filled):
        facts.append("parentheses mean negative")
    if any(value.rstrip().endswith(("-", "\u2212")) for value in filled):
        facts.append("trailing minus means negative")
    return facts


def safe_vocabulary(values):
    """The sorted distinct values if they are a small, plain vocabulary, else None."""
    distinct = sorted({value for value in values if value})
    if not distinct or len(distinct) > MAX_SHOWN_VALUES:
        return None
    return distinct if all(SAFE_VOCABULARY.fullmatch(value) for value in distinct) else None


def describe_column(name, values, show_values=False, from_file=False):
    """Report lines for one column, plus the raw values shown (for the leak guard)."""
    kind = classify(values)
    filled = [value for value in values if value]
    lines = [f"- {_shown_name(name, from_file)}: {kind}, empty in {len(values) - len(filled)} of {len(values)} rows"]
    shown = []
    patterns = Counter(mask(value) for value in filled)
    if patterns and len(patterns) <= MAX_DISTINCT_TO_LIST:
        listed = ", ".join(f"{pattern} x{count}" for pattern, count in patterns.most_common(MAX_LISTED_PATTERNS))
        lines.append(f"    patterns: {data_text(listed)}")
    elif patterns:
        lengths = [len(value) for value in filled]
        lines.append(f"    {len(patterns)} distinct patterns; length {min(lengths)} to {max(lengths)}")
    if kind == "text":
        lines.append(f"    distinct values: {len(set(filled))}")
    if kind == "date":
        lines.append(f"    order: {date_order(filled)}")
    if kind == "money":
        lines.append("    " + "; ".join(money_facts(filled)))
    if show_values:
        vocabulary = safe_vocabulary(values)
        if vocabulary is None:
            lines.append(f"    values NOT shown: more than {MAX_SHOWN_VALUES} distinct or not a plain vocabulary")
        else:
            shown = vocabulary
            lines.append("    values (shown because you asked): " + data_text("; ".join(vocabulary)))
    return lines, shown


def debit_credit_pairs(columns, names, from_file=None):
    """Names of money-column pairs where each row fills exactly one of the two."""
    origin = from_file or [False] * len(names)
    money = [index for index, values in enumerate(columns) if classify(values) == "money"]
    pairs = []
    for position, first in enumerate(money):
        for second in money[position + 1:]:
            either = sum(bool(a) or bool(b) for a, b in zip(columns[first], columns[second]))
            exactly_one = sum(bool(a) != bool(b) for a, b in zip(columns[first], columns[second]))
            if either and exactly_one >= 0.9 * either and any(columns[first]) and any(columns[second]):
                pairs.append(
                    f"{_shown_name(names[first], origin[first])} and {_shown_name(names[second], origin[second])}"
                )
    return pairs


def _layout(rows, beyond):
    lengths = Counter(len(row) for row in rows) + beyond
    width = max(lengths.items(), key=lambda item: (item[1], item[0]))[0]
    header_index, block = find_header(rows, width)
    if block is not None:
        start, end = block
        # Only the dated rows of the winning block. A same-width Total /
        # Subtotal / category trailer is not a date, and mixing it in drops
        # classify() below the 90% date threshold.
        data = [row for row in rows[start:end] if _has_date(row, width)]
    else:
        origin = 0 if header_index is None else header_index + 1
        data = [row for row in rows[origin:] if len(row) == width]
    return lengths, width, header_index, data


def _file_lines(label, text, encoding, delimiter, rows, lengths, header_index, data):
    delimiter_name = "TAB" if delimiter == "\t" else repr(delimiter)
    header_text = f"line {header_index + 1}" if header_index is not None else "none detected"
    total = sum(lengths.values())
    lines = [
        f"Shape of {label}  (masked patterns only unless you asked for more; review before sharing)",
        f"encoding: {encoding}; line endings: {line_endings(text)}; delimiter: {delimiter_name}",
        f"rows: {total} total; column counts: " + ", ".join(f"{n} columns x{c}" for n, c in sorted(lengths.items())),
        f"header row: {header_text}; rows before it: {header_index or 0}",
        f"data rows profiled: {len(data)}",
    ]
    if total > len(rows):
        lines.append(
            f"note: only the first {len(rows)} rows were profiled and masked examples come from them; "
            f"the other {total - len(rows)} were counted by column count only"
        )
    return lines


def _masked(row):
    return data_text(" | ".join(mask(cell.strip()) for cell in row))


def _tail_lines(rows, header_index, width):
    """Masked examples of everything that is not a data row: preamble and other sections."""
    lines = []
    if header_index is None:
        lines.append("first row (masked): " + _masked(rows[0]))
    elif header_index:
        lines.append("rows before the header (masked): " + " || ".join(_masked(row) for row in rows[: min(header_index, 5)]))
    for other in sorted({len(row) for row in rows} - {width}):
        examples = [row for row in rows if len(row) == other][:3]
        lines.append(f"rows with {other} columns (masked, first {len(examples)}): " + " || ".join(_masked(row) for row in examples))
    return lines


def describe_csv(raw, label="file", show=(), show_headers=False):
    """The whole report for one file's bytes, or LeakError if it would expose a value."""
    text, encoding = decode_text(raw)
    delimiter = choose_delimiter(text)
    rows, beyond, warning = read_rows(text, delimiter)
    if not rows:
        return f"{label}: no readable rows" + (f"\n{warning}" if warning else "")
    lengths, width, header_index, data = _layout(rows, beyond)
    header = rows[header_index] if header_index is not None else []
    labeled = [label_for(cell, i, show_headers) for i, cell in enumerate(header)]
    if labeled:
        names = [name for name, _ in labeled]
        from_file = [flag for _, flag in labeled]
    else:
        names = [f"col {i + 1}" for i in range(width)]
        from_file = [False] * width
    columns = [[row[i].strip() for row in data] for i in range(width)]
    wanted = {name.strip().lower() for name in show}

    out = _file_lines(label, text, encoding, delimiter, rows, lengths, header_index, data)
    out += [warning] if warning else []
    out += ["", "columns:"]
    allowed = {cell.strip() for cell, name in zip(header, names) if name == cell.strip()}
    matched = set()
    for index, (name, values) in enumerate(zip(names, columns)):
        # A requested column matches its printed name, "col N", or the header text you typed.
        keys = {name.lower(), f"col {index + 1}"} | ({header[index].strip().lower()} if index < len(header) else set())
        hit = keys & wanted
        matched |= hit
        lines, shown = describe_column(name, values, show_values=bool(hit), from_file=from_file[index])
        out.extend(lines)
        allowed.update(shown)
    missing = wanted - matched
    pairs = debit_credit_pairs(columns, names, from_file)
    out += ["", "separate debit/credit style pairs: " + ("; ".join(pairs) if pairs else "none detected")]
    out += [f"--show-values column not found: {name}" for name in sorted(missing)]
    out += _tail_lines(rows, header_index, width)
    if header_index is not None and not show_headers:
        out.append("header labels are masked; if the first lines of your file are only column names, re-run with --show-headers")
    report = "\n".join(out)
    check_no_leak(report, rows, allowed, dynamic_only=True)
    return report.replace(START, "").replace(END, "")


SHAPE_ONLY = re.compile(r"[Aa9 /\-.,$()+:;@#&%'\"*_]+")


def is_shape_only(value):
    """True for a value made only of mask characters (A, a, 9, punctuation), such as "9999".

    Such a value reveals nothing beyond its shape, so finding it in the report is
    not a leak. This is deliberately independent of mask(): the guard must still
    work if masking is ever broken.
    """
    return bool(SHAPE_ONLY.fullmatch(re.sub(r"\{\d+\}", "", value)))


def check_no_leak(report, rows, allowed, dynamic_only=False):
    """Refuse to return a report that contains any value from the file.

    With dynamic_only, only the text tagged by data_text() is searched. The
    report's own fixed wording ("money", "debit/credit", "empty in") would
    otherwise collide with ordinary cell values and make the tool refuse
    valid files. Printed header labels and requested vocabularies are removed
    first, since printing those is intended, and a value identical to its own
    mask reveals nothing beyond its shape.
    """
    text = "\n".join(re.findall(f"{START}(.*?){END}", report, flags=re.S)) if dynamic_only else report
    for item in sorted(allowed, key=len, reverse=True):
        text = text.replace(item, "")
    values = {cell.strip() for row in rows for cell in row}
    leaked = [
        value for value in values
        if len(value) >= 4 and any(c.isalnum() for c in value) and not is_shape_only(value) and value in text
    ]
    if leaked:
        raise LeakError(f"{len(leaked)} value(s) from the file would appear in the output; nothing printed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Describe a CSV's shape without printing its values.")
    parser.add_argument("path", help="CSV export to describe")
    parser.add_argument("--label", default="the file", help="name to show in the report")
    parser.add_argument("--show-values", action="append", default=[], metavar="COLUMN",
                        help="also list the distinct values of this column (only for a fixed vocabulary such as a transaction type)")
    parser.add_argument("--show-headers", action="store_true",
                        help="print the header labels as written; use only after checking that the first lines of the file are just column names")
    args = parser.parse_args(argv)
    try:
        with open(args.path, "rb") as handle:
            raw = handle.read()
        print(describe_csv(raw, args.label, args.show_values, args.show_headers))
    except OSError as error:
        print(f"cannot read the file: {error.strerror}", file=sys.stderr)
        return 1
    except LeakError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
