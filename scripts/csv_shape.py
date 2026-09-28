#!/usr/bin/env python3
"""Describe the shape of a CSV export without printing its values.

Run this on your own machine against a real export and share only its output.
It prints the delimiter, encoding, line endings, column headers, and masked
value patterns (letters become a/A, digits become 9). It never prints a cell
value unless you ask for one column's distinct values with --show-values, and
it refuses to print anything if a check finds an unrequested value in its
own output.

    python scripts/csv_shape.py path/to/export.csv
    python scripts/csv_shape.py export.csv --show-values "Transaction Type"

Use --show-values only for a column whose values are a fixed vocabulary, such
as a transaction type, never for descriptions or anything that names a person,
merchant, or account.

Header labels are shown as written only when every word is an ordinary column
label word (date, amount, description, symbol, and so on); anything else is
masked, so a name in a preamble line can never be printed. If a real header is
masked and you have checked that it is safe, --trust-headers prints plain-label
headers as written; --mask-headers masks them all.

Standard library only. Review the output before sharing it.
"""
import argparse
import codecs
import csv
import re
import sys
from collections import Counter

MAX_ROWS = 200_000
MAX_LISTED_PATTERNS = 4
MAX_DISTINCT_TO_LIST = 12
MAX_SHOWN_VALUES = 25
DELIMITERS = (",", ";", "\t", "|")
KEPT_PUNCTUATION = set(" /-.,$()+:;@#&%'\"*_")

DATE_LIKE = re.compile(r"\d{1,4}([/.-])\d{1,2}\1\d{1,4}")
MONEY_LIKE = re.compile(r"[-+(]?[$€£]?\s?[-+(]?\d[\d,.\s]*[)-]?")
LABEL = re.compile(r"[A-Za-z][A-Za-z /#&()._'-]{0,39}")
SAFE_VOCABULARY = re.compile(r"[A-Za-z][A-Za-z /&()._'-]{0,39}")
LONG_RUN = re.compile(r"(.)\1{5,}")

# Ordinary words found in bank and brokerage column headers. A header made only
# of these can be printed as written because nothing identifying can be built
# from them; anything else is masked unless the owner passes --trust-headers.
HEADER_WORDS = frozenset("""
account accrued action activity actual additional address amount and available balance basis branch by
card category charge charges check cheque city class clearing cleared closing code commission commissions
cost country credit currency date day deposit deposits desc description detail details debit effective
fee fees for from fund gain id in interest investment ledger location loss market memo merchant month name
narrative net no note notes num number of on opening original out payee payment payments pending post
posted posting price principal purchase purchased purchases quantity realized ref reference running
security sequence serial settle settlement share shares split state status subcategory symbol tags tax
taxes ticker time to total trade trans transaction transactions type units usd value withdrawal
withdrawals withheld year
""".split())


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


def decode_text(raw):
    """Return (text, encoding label). UTF-8 first, then Windows-1252, then Latin-1."""
    bom = " with BOM" if raw.startswith(codecs.BOM_UTF8) else ""
    for encoding, label in (("utf-8-sig", "utf-8" + bom), ("cp1252", "windows-1252")):
        try:
            return raw.decode(encoding), label
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def line_endings(text):
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    if crlf and not lone_lf:
        return f"CRLF x{crlf}"
    if lone_lf and not crlf:
        return f"LF x{lone_lf}"
    return f"mixed (CRLF {crlf}, LF {lone_lf})"


def _delimiter_score(sample, delimiter):
    lengths = Counter(len(row) for row in csv.reader(sample, delimiter=delimiter) if row)
    if not lengths:
        return 0
    modal, count = max(lengths.items(), key=lambda item: (item[1], item[0]))
    return count * modal if modal >= 2 else 0


def choose_delimiter(text):
    sample = text.splitlines()[:500]
    best_score, best = max(((_delimiter_score(sample, d), d) for d in DELIMITERS), key=lambda item: item[0])
    return best if best_score > 0 else ","


def read_rows(text, delimiter):
    rows = []
    try:
        for row in csv.reader(text.splitlines(), delimiter=delimiter):
            if len(rows) >= MAX_ROWS:
                break
            if row:
                rows.append(row)
    except csv.Error:
        pass
    return rows


def looks_like_header(row):
    cells = [cell.strip() for cell in row]
    return all(cell and LABEL.fullmatch(cell) for cell in cells) and len(set(cells)) == len(cells)


def find_header(rows, width):
    """Index of the header row, else None.

    The header is the row immediately before the first full-width row that
    contains a date, and only if it is itself full-width and label-like. Taking
    the first label-like row instead would let a preamble line such as an
    account holder's name be mistaken for the header and printed as written.
    """
    for index, row in enumerate(rows):
        if len(row) == width and any(DATE_LIKE.fullmatch(cell.strip()) for cell in row):
            previous = rows[index - 1] if index else None
            if previous is not None and len(previous) == width and looks_like_header(previous):
                return index - 1
            return None
    return None


def is_generic_label(text):
    """True when every word is ordinary column-label vocabulary, so it cannot be a name."""
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and all(word in HEADER_WORDS for word in words)


def label_for(cell, index, mask_headers=False, trust_headers=False):
    """The header as written when it is a generic label (or trusted), else its mask.

    Structure alone cannot tell a header from a preamble line such as an account
    holder's name that has the same number of cells, so only labels made of
    ordinary column-label words are ever printed as written.
    """
    text = cell.strip()
    if not mask_headers and LABEL.fullmatch(text) and (trust_headers or is_generic_label(text)):
        return text
    return f"col {index + 1} (masked: {mask(text)})"


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
        match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", value)
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
    """A minus or opening parenthesis before the digits (also after a currency symbol), or a trailing minus."""
    prefix = sign_prefix(value)
    return "-" in prefix or "(" in prefix or value.rstrip().endswith("-")


def money_facts(values):
    filled = [value for value in values if value]
    negative = sum(is_negative(value) for value in filled)
    facts = [f"negative values: {negative} of {len(filled)}"]
    stripped = [value.rstrip(")-") for value in filled]
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
    if any(value.rstrip().endswith("-") for value in filled):
        facts.append("trailing minus means negative")
    return facts


def safe_vocabulary(values):
    """The sorted distinct values if they are a small, plain vocabulary, else None."""
    distinct = sorted({value for value in values if value})
    if not distinct or len(distinct) > MAX_SHOWN_VALUES:
        return None
    return distinct if all(SAFE_VOCABULARY.fullmatch(value) for value in distinct) else None


def describe_column(name, values, show_values=False):
    """Report lines for one column, plus the raw values shown (for the leak guard)."""
    kind = classify(values)
    filled = [value for value in values if value]
    lines = [f"- {name}: {kind}, empty in {len(values) - len(filled)} of {len(values)} rows"]
    shown = []
    patterns = Counter(mask(value) for value in filled)
    if patterns and len(patterns) <= MAX_DISTINCT_TO_LIST:
        listed = ", ".join(f"{pattern} x{count}" for pattern, count in patterns.most_common(MAX_LISTED_PATTERNS))
        lines.append(f"    patterns: {listed}")
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
            lines.append("    values (shown because you asked): " + "; ".join(vocabulary))
    return lines, shown


def debit_credit_pairs(columns, names):
    """Names of money-column pairs where each row fills exactly one of the two."""
    money = [index for index, values in enumerate(columns) if classify(values) == "money"]
    pairs = []
    for position, first in enumerate(money):
        for second in money[position + 1:]:
            either = sum(bool(a) or bool(b) for a, b in zip(columns[first], columns[second]))
            exactly_one = sum(bool(a) != bool(b) for a, b in zip(columns[first], columns[second]))
            if either and exactly_one >= 0.9 * either and any(columns[first]) and any(columns[second]):
                pairs.append(f"{names[first]} and {names[second]}")
    return pairs


def _layout(rows):
    lengths = Counter(len(row) for row in rows)
    width = max(lengths.items(), key=lambda item: (item[1], item[0]))[0]
    header_index = find_header(rows, width)
    start = 0 if header_index is None else header_index + 1
    data = [row for row in rows[start:] if len(row) == width]
    return lengths, width, header_index, data


def _file_lines(label, text, encoding, delimiter, rows, lengths, header_index, data):
    delimiter_name = "TAB" if delimiter == "\t" else repr(delimiter)
    header_text = f"line {header_index + 1}" if header_index is not None else "none detected"
    return [
        f"Shape of {label}  (headers and masked patterns only; review before sharing)",
        f"encoding: {encoding}; line endings: {line_endings(text)}; delimiter: {delimiter_name}",
        f"rows: {len(rows)} total; column counts: " + ", ".join(f"{n} columns x{c}" for n, c in sorted(lengths.items())),
        f"header row: {header_text}; rows before it: {header_index or 0}",
        f"data rows profiled: {len(data)}",
    ]


def _masked(row):
    return " | ".join(mask(cell.strip()) for cell in row)


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


def describe_csv(raw, label="file", show=(), mask_headers=False, trust_headers=False):
    """The whole report for one file's bytes, or LeakError if it would expose a value."""
    text, encoding = decode_text(raw)
    delimiter = choose_delimiter(text)
    rows = read_rows(text, delimiter)
    if not rows:
        return f"{label}: no readable rows"
    lengths, width, header_index, data = _layout(rows)
    header = rows[header_index] if header_index is not None else []
    names = [label_for(cell, i, mask_headers, trust_headers) for i, cell in enumerate(header)] or [f"col {i + 1}" for i in range(width)]
    columns = [[row[i].strip() for row in data] for i in range(width)]
    wanted = {name.strip().lower() for name in show}

    out = _file_lines(label, text, encoding, delimiter, rows, lengths, header_index, data) + ["", "columns:"]
    allowed = {cell.strip() for cell, name in zip(header, names) if name == cell.strip()}
    matched = set()
    for index, (name, values) in enumerate(zip(names, columns)):
        # A requested column matches its printed name, "col N", or the header text you typed.
        keys = {name.lower(), f"col {index + 1}"} | ({header[index].strip().lower()} if index < len(header) else set())
        hit = keys & wanted
        matched |= hit
        lines, shown = describe_column(name, values, show_values=bool(hit))
        out.extend(lines)
        allowed.update(shown)
    missing = wanted - matched
    pairs = debit_credit_pairs(columns, names)
    out += ["", "separate debit/credit style pairs: " + ("; ".join(pairs) if pairs else "none detected")]
    out += [f"--show-values column not found: {name}" for name in sorted(missing)]
    out += _tail_lines(rows, header_index, width)
    report = "\n".join(out)
    check_no_leak(report, rows, allowed)
    return report


def check_no_leak(report, rows, allowed):
    """Refuse to return a report that contains any value from the file."""
    values = {cell.strip() for row in rows for cell in row}
    leaked = [
        value for value in values
        if len(value) >= 4 and any(c.isalnum() for c in value) and value not in allowed and value in report
    ]
    if leaked:
        raise LeakError(f"{len(leaked)} value(s) from the file would appear in the output; nothing printed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Describe a CSV's shape without printing its values.")
    parser.add_argument("path", help="CSV export to describe")
    parser.add_argument("--label", default="the file", help="name to show in the report")
    parser.add_argument("--show-values", action="append", default=[], metavar="COLUMN",
                        help="also list the distinct values of this column (only for a fixed vocabulary such as a transaction type)")
    parser.add_argument("--mask-headers", action="store_true",
                        help="mask the header labels too (use if a header row could contain a name or account detail)")
    parser.add_argument("--trust-headers", action="store_true",
                        help="print any plain-label header as written, not only generic column words (after you have checked the file)")
    args = parser.parse_args(argv)
    try:
        with open(args.path, "rb") as handle:
            raw = handle.read()
        print(describe_csv(raw, args.label, args.show_values, args.mask_headers, args.trust_headers))
    except OSError as error:
        print(f"cannot read the file: {error.strerror}", file=sys.stderr)
        return 1
    except LeakError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
