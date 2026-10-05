"""Read statement transactions only; never retain account or balance fields."""

import codecs
import re
from html import unescape
from xml.etree import ElementTree as ET

from .parser import MAX_DATA_ROWS, MAX_FILE_BYTES, CsvDocument, CsvInputError, CsvRow
from .ofx_sgml import LEAF_TAGS

OFX_HEADERS = ("Date", "Amount", "Name", "Memo", "FITID", "Type")
MALFORMED = "This is not a valid OFX / QFX statement file."
UNSUPPORTED_CORRECTIONS = (
    "OFX transaction corrections are not supported. "
    "Use the app's transaction correction or import undo controls instead."
)


def _decode(content):
    if len(content) > MAX_FILE_BYTES:
        raise CsvInputError("The statement file exceeds the 5 MB limit.")
    try:
        return content.decode(_encoding(content))
    except (UnicodeError, LookupError):
        raise CsvInputError("The statement file has an invalid or unsupported text encoding.") from None


def _encoding(content):
    # XML's declaration takes precedence even when its bytes happen to be UTF-8.
    if content.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for signature, encoding in (
        (b"\x00\x00\x00<", "utf-32-be"), (b"<\x00\x00\x00", "utf-32-le"),
        (b"\x00<", "utf-16-be"), (b"<\x00", "utf-16-le"),
    ):
        if content.startswith(signature):
            return encoding
    declaration = re.match(
        rb"(?:\xef\xbb\xbf)?<\?xml\s[^>]*\bencoding\s*=\s*['\"]([A-Za-z0-9_.-]+)['\"]",
        content[:1024],
    )
    if declaration:
        name = declaration.group(1).decode("ascii")
        return "utf-8-sig" if name.lower() in ("utf-8", "utf8") else name
    header = content.split(b"<", 1)[0]
    if b"CHARSET:1252" in header or b"CHARSET:WINDOWS-1252" in header:
        return "cp1252"
    return "utf-8-sig"


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
    namespace, _separator, name = root.tag.rpartition("}")
    if name != "OFX":
        raise CsvInputError(MALFORMED)
    if namespace:
        prefix = namespace + "}"
        for element in root.iter():
            if element.tag.startswith(prefix):
                element.tag = element.tag[len(prefix):]
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
    if _value(transaction, "CORRECTFITID") or _value(transaction, "CORRECTACTION"):
        # Corrections require matching provider IDs, which this importer does
        # not do. Treating a replacement as a new fingerprint would double count.
        raise CsvInputError(UNSUPPORTED_CORRECTIONS)
    posted = _value(transaction, "DTPOSTED")
    date = f"{posted[:4]}-{posted[4:6]}-{posted[6:8]}" if re.match(r"^\d{8}", posted) else ""
    name = _value(transaction, "NAME") or _value(transaction, "PAYEE/NAME") or _value(transaction, "PAYEE2/NAME")
    cells = (date, _value(transaction, "TRNAMT"), name,
             *(_value(transaction, tag) for tag in ("MEMO", "FITID", "TRNTYPE")))
    error = _currency_error(statement, transaction)
    if len(cells[4]) > 255:
        error = "FITID exceeds the 255 character limit."
    return CsvRow(number, cells, error)
