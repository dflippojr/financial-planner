"""Read statement transactions only; never retain account or balance fields."""

import re
from html import unescape
from xml.etree import ElementTree as ET

from .parser import MAX_DATA_ROWS, MAX_FILE_BYTES, CsvDocument, CsvInputError, CsvRow

OFX_HEADERS = ("Date", "Amount", "Name", "Memo", "FITID", "Type")
MALFORMED = "This is not a valid OFX / QFX statement file."
LEAF_TAGS = {"DTPOSTED", "TRNAMT", "NAME", "MEMO", "FITID", "TRNTYPE", "CURDEF", "CURSYM"}


def _decode(content):
    if len(content) > MAX_FILE_BYTES:
        raise CsvInputError("The statement file exceeds the 5 MB limit.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # OFX 1.x commonly declares Windows code page 1252.
        header = content.split(b"<", 1)[0]
        if b"CHARSET:1252" in header or b"CHARSET:WINDOWS-1252" in header:
            try:
                return content.decode("cp1252")
            except UnicodeDecodeError:
                pass
        raise CsvInputError("The statement file must use UTF-8 or declared Windows-1252 encoding.") from None


def _sgml_tree(text):
    """OFX 1.x permits omitted scalar end tags, but requires aggregate end tags."""
    root = ET.Element("DOCUMENT")
    stack = [root]
    for token in re.split(r"(<[^<>]*>)", text):
        if not token.startswith("<"):
            if token.strip():
                if len(stack) == 1:
                    raise CsvInputError(MALFORMED)
                stack[-1].text = (stack[-1].text or "") + unescape(token.strip())
            continue
        _sgml_tag(stack, token)
    if len(stack) != 1 or len(root) != 1 or root[0].tag != "OFX":
        raise CsvInputError(MALFORMED)
    return root[0]


def _sgml_tag(stack, token):
    match = re.fullmatch(r"<(/?)([A-Z][A-Z0-9_.:-]*)\s*>", token)
    if not match:
        raise CsvInputError(MALFORMED)
    closing, tag = match.groups()
    if len(stack) > 1 and (stack[-1].text is not None or stack[-1].tag in LEAF_TAGS):
        leaf = stack.pop()
        if closing and leaf.tag == tag:
            return
    if closing:
        if len(stack) == 1 or stack[-1].tag != tag:
            raise CsvInputError(MALFORMED)
        stack.pop()
    else:
        if len(stack) > 64:
            raise CsvInputError(MALFORMED)
        stack.append(ET.SubElement(stack[-1], tag))


def _tree(text):
    if "\x00" in text:
        raise CsvInputError(MALFORMED)
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise CsvInputError("OFX declarations of document types or entities are not supported.")
    if text.lstrip().startswith("OFXHEADER:"):
        start = text.find("<OFX>")
        if start < 0:
            raise CsvInputError(MALFORMED)
        return _sgml_tree(text[start:])
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise CsvInputError(MALFORMED) from None
    if root.tag != "OFX":
        raise CsvInputError(MALFORMED)
    return root


def _value(element, tag):
    return (element.findtext(tag) or "").strip()


def _currency_error(statement, transaction):
    currencies = [_value(statement, "CURDEF")]
    for container in (statement, transaction):
        for currency in container.findall("CURRENCY"):
            currencies.append(_value(currency, "CURSYM") or (currency.text or "").strip())
    if any(value.upper() != "USD" for value in currencies if value):
        return "Currency must be USD."
    return None


def read_ofx(content: bytes, *, max_rows=MAX_DATA_ROWS) -> CsvDocument:
    root = _tree(_decode(content))
    rows = []
    for statement_tag in ("STMTRS", "CCSTMTRS"):
        for statement in root.iter(statement_tag):
            for transaction in statement.findall("BANKTRANLIST/STMTTRN"):
                if len(rows) >= max_rows:
                    raise CsvInputError(f"The statement file exceeds the {max_rows:,} row limit.")
                rows.append(_row(statement, transaction, len(rows) + 2))
    if not rows:
        raise CsvInputError("This file has no bank or card transactions")
    return CsvDocument(OFX_HEADERS, tuple(rows))


def _row(statement, transaction, number):
    posted = _value(transaction, "DTPOSTED")
    date = f"{posted[:4]}-{posted[4:6]}-{posted[6:8]}" if re.match(r"^\d{8}", posted) else ""
    cells = (date, *(_value(transaction, tag) for tag in ("TRNAMT", "NAME", "MEMO", "FITID", "TRNTYPE")))
    error = _currency_error(statement, transaction)
    if len(cells[4]) > 255:
        error = "FITID exceeds the 255 character limit."
    return CsvRow(number, cells, error)
