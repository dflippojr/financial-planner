"""Parse an uploaded chat attachment into text once, at upload (#644).

Every file is hostile until parsed. The type comes from the bytes, never from the
name or the browser's content type (#517): PDF by its header, XLSX by its zip
layout, CSV only for a .csv name whose bytes decode as text. Size, sheet, row,
column, page and character limits are enforced while reading, and a file over a
limit is rejected with a member-facing message; nothing is stored for it.

XLSX is read with the standard library: cached cell values only, never
formulas, macros (vbaProject.bin) or external links, from a zip whose member
count and sizes are checked before any member is read. PDF text is extracted by
chat_attachment_pdf in a child process with a time, memory and page cap.
Nothing here logs names or contents.
"""

from __future__ import annotations

import csv
import io
import json
import math
import posixpath
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

from django.conf import settings

CSV = "csv"
XLSX = "xlsx"
PDF = "pdf"
KIND_LABELS = {CSV: "CSV", XLSX: "XLSX", PDF: "PDF"}

MAX_SHEETS = 20
MAX_ROWS = 5_000
MAX_COLUMNS = 100
MAX_CELL_CHARS = 1_000
MAX_TEXT_CHARS = 2_000_000
MAX_PAGES = 100
MAX_NAME_CHARS = 255
MAX_SHEET_NAME_CHARS = 100
# XLSX zip guards: a zip bomb is refused from its directory before anything is inflated,
# and every read is bounded in case a member's declared size is a lie.
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024

TOO_LARGE = "That file is larger than 10 MB. Attach a smaller file."
UNSUPPORTED = "Attach a CSV, XLSX or PDF file. Other file types can't be read."
LEGACY_XLS = "That looks like an older .xls workbook. Save it as .xlsx or .csv and attach it again."
PASSWORD_PROTECTED = "That file is password-protected. Remove the password, save it again and attach it."  # noqa: S105 - not a secret
UNREADABLE = "That file could not be read. Check that it opens, save it again as CSV, XLSX or PDF and attach it."
EMPTY = "That file has no cells or text to read."
NO_PDF_TEXT = "No text could be read from that PDF. Scanned pages without a text layer can't be read."
PDF_TIMEOUT = "That PDF took too long to read. Try a smaller file."
TOO_MANY_SHEETS = f"That workbook has more than {MAX_SHEETS} sheets. Remove some and attach it again."
TOO_MANY_ROWS = f"A sheet in that file has more than {MAX_ROWS:,} rows. Remove rows or split it and attach it again."
TOO_MANY_COLUMNS = f"A sheet in that file uses more than {MAX_COLUMNS} columns. Remove columns and attach it again."
TOO_MUCH_TEXT = "That file has too much text to read. Split it and attach the parts you need."
TOO_MANY_PAGES = f"That PDF has more than {MAX_PAGES} pages. Attach only the pages you need."

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# An encrypted Office file is an OLE container holding an EncryptionInfo stream.
_OLE_ENCRYPTION = "EncryptionInfo".encode("utf-16-le")
_DTD = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# The DTD search reads raw bytes, so it holds only for UTF-8 (or ASCII) parts: anything
# else (UTF-16 by BOM, by its NUL bytes or by declaration) is refused before parsing.
_XML_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_UTF8_NAMES = frozenset({b"utf-8", b"utf8", b"us-ascii", b"ascii"})
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff")
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORKSHEET_REL = "/worksheet"
_CELL_REF = re.compile(r"^([A-Za-z]{1,3})(\d+)$")
# Built-in number formats that show a date or time (ECMA-376 18.8.30).
_DATE_FORMAT_IDS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})
_FORMAT_LITERALS = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.')
_EXCEL_EPOCH = datetime(1899, 12, 30)
_EXCEL_1904_EPOCH = datetime(1904, 1, 1)
_MAX_SERIAL = 2_958_465  # 9999-12-31


class AttachmentRejected(Exception):
    """A member-facing reason the file was refused. Nothing was stored."""


@dataclass
class ParsedAttachment:
    name: str
    kind: str
    data: bytes
    content: dict
    summary: dict
    size_bytes: int = field(init=False)

    def __post_init__(self):
        self.size_bytes = len(self.data)


def parse_upload(uploaded_file) -> ParsedAttachment:
    """Read, sniff and parse an uploaded file, or raise AttachmentRejected."""
    data = _read_upload(uploaded_file)
    name = safe_name(getattr(uploaded_file, "name", "") or "")
    kind = sniff(data, name)
    if kind == PDF:
        content, summary = _parse_pdf(data)
    elif kind == XLSX:
        content, summary = _parse_xlsx(data)
    else:
        content, summary = _parse_csv(data)
    return ParsedAttachment(name=name, kind=kind, data=data, content=content, summary=summary)


def safe_name(name: str) -> str:
    """The file's base name with control characters removed, for display and model context."""
    cleaned = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = clean_label(cleaned, MAX_NAME_CHARS)
    return cleaned if cleaned not in {"", ".", ".."} else "attachment"


def clean_label(value: str, limit: int) -> str:
    """One line of plain text: control characters become spaces, runs collapse, then truncate."""
    text = "".join(" " if (ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 or ch in "  ") else ch for ch in str(value))
    return " ".join(text.split())[:limit]


def sniff(data: bytes, name: str) -> str:
    if data.startswith(_OLE_MAGIC):
        raise AttachmentRejected(PASSWORD_PROTECTED if _OLE_ENCRYPTION in data else LEGACY_XLS)
    if data.startswith(_PDF_MAGIC):
        return PDF
    if data.startswith(_ZIP_MAGIC):
        return XLSX
    if name.lower().endswith(".csv") and b"\x00" not in data:
        return CSV
    raise AttachmentRejected(UNSUPPORTED)


def _read_upload(uploaded_file) -> bytes:
    limit = int(settings.CHAT_ATTACHMENT_MAX_BYTES)
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > limit:
        raise AttachmentRejected(TOO_LARGE)
    uploaded_file.seek(0)
    data = uploaded_file.read(limit + 1)
    if len(data) > limit:
        raise AttachmentRejected(TOO_LARGE)
    if not data:
        raise AttachmentRejected(EMPTY)
    return data


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _parse_csv(data: bytes):
    text = _decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = _Rows("CSV")
    try:
        for number, record in enumerate(csv.reader(io.StringIO(text, newline=""), dialect), start=1):
            rows.add(number, record)
    except csv.Error:
        raise AttachmentRejected(UNREADABLE) from None
    return _sheet_result([rows])


# ---------------------------------------------------------------------------
# Rows shared by CSV and XLSX
# ---------------------------------------------------------------------------


class _Rows:
    """Non-empty rows of one sheet as (row number, cell texts), with the limits enforced."""

    def __init__(self, name):
        self.name = clean_label(name, MAX_SHEET_NAME_CHARS) or "Sheet"
        self.rows = []
        self.columns = 0
        self.chars = 0

    def add(self, number, cells):
        cells = [_cell_text(cell) for cell in cells]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            return
        if len(cells) > MAX_COLUMNS:
            raise AttachmentRejected(TOO_MANY_COLUMNS)
        if len(self.rows) >= MAX_ROWS:
            raise AttachmentRejected(TOO_MANY_ROWS)
        self.chars += sum(len(cell) for cell in cells)
        if self.chars > MAX_TEXT_CHARS:
            raise AttachmentRejected(TOO_MUCH_TEXT)
        self.columns = max(self.columns, len(cells))
        self.rows.append([number, cells])


def _cell_text(value) -> str:
    text = str(value if value is not None else "").replace("\x00", "")
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 1] + "…"
    return text.strip()


def _sheet_result(sheets):
    if not any(sheet.rows for sheet in sheets):
        raise AttachmentRejected(EMPTY)
    if sum(sheet.chars for sheet in sheets) > MAX_TEXT_CHARS:
        raise AttachmentRejected(TOO_MUCH_TEXT)
    content = {"sheets": [{"name": sheet.name, "rows": sheet.rows} for sheet in sheets]}
    summary = {"sheets": [{"name": sheet.name, "rows": len(sheet.rows), "columns": sheet.columns} for sheet in sheets]}
    return content, summary


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _parse_xlsx(data: bytes):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise AttachmentRejected(UNREADABLE) from None
    with archive:
        members = _checked_members(archive)
        if "xl/workbook.xml" not in members or "[Content_Types].xml" not in members:
            # Another zip format (a .docx, .ods or a plain archive).
            raise AttachmentRejected(UNSUPPORTED)
        try:
            return _read_workbook(archive, members)
        except AttachmentRejected:
            raise
        except (ElementTree.ParseError, zipfile.BadZipFile, KeyError, TypeError, ValueError, OSError, EOFError,
                NotImplementedError, RuntimeError):
            raise AttachmentRejected(UNREADABLE) from None


def _checked_members(archive):
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise AttachmentRejected(UNREADABLE)
    total = 0
    members = {}
    for info in infos:
        if info.flag_bits & 0x1:
            # An encrypted zip member: Excel never writes one, so treat it as protected.
            raise AttachmentRejected(PASSWORD_PROTECTED)
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise AttachmentRejected(TOO_MUCH_TEXT)
        total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            raise AttachmentRejected(TOO_MUCH_TEXT)
        members[info.filename] = info
    return members


def _read_member(archive, members, path):
    """A member's bytes, read with a hard cap whatever its header claims. DTDs are refused."""
    info = members.get(path)
    if info is None:
        return None
    with archive.open(info) as handle:
        payload = handle.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(payload) > MAX_ZIP_MEMBER_BYTES:
        raise AttachmentRejected(TOO_MUCH_TEXT)
    # No entity expansion and no external fetches: a workbook part never needs a DTD.
    if not _is_utf8_xml(payload) or _DTD.search(payload):
        raise AttachmentRejected(UNREADABLE)
    return payload


def _is_utf8_xml(payload: bytes) -> bool:
    """Whether expat will read these bytes as UTF-8, so the byte-level DTD search is complete."""
    if b"\x00" in payload or payload.startswith(_WIDE_BOMS):
        return False
    declared = _XML_ENCODING.match(payload.removeprefix(b"\xef\xbb\xbf")[:512])
    return declared is None or declared.group(1).lower() in _UTF8_NAMES


def _parse_xml(payload):
    # _read_member refused any DTD, so no entity can be declared, expanded or fetched.
    return ElementTree.fromstring(payload)  # noqa: S314 - DTDs are refused before parsing


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _read_workbook(archive, members):
    workbook = _parse_xml(_read_member(archive, members, "xl/workbook.xml"))
    date1904 = False
    sheets = []
    for element in workbook.iter():
        tag = _local(element.tag)
        if tag == "workbookPr":
            date1904 = str(element.get("date1904", "")).lower() in {"1", "true"}
        elif tag == "sheet":
            sheets.append((element.get("name") or "Sheet", element.get(f"{{{_REL_NS}}}id") or ""))
    targets = _worksheet_targets(archive, members)
    sheets = [(name, targets[rel]) for name, rel in sheets if rel in targets]
    if not sheets:
        raise AttachmentRejected(EMPTY)
    if len(sheets) > MAX_SHEETS:
        raise AttachmentRejected(TOO_MANY_SHEETS)
    strings = _shared_strings(archive, members)
    date_styles = _date_styles(archive, members)
    epoch = _EXCEL_1904_EPOCH if date1904 else _EXCEL_EPOCH
    parsed = []
    total = 0
    for name, path in sheets:
        rows = _read_sheet(_read_member(archive, members, path), name, strings, date_styles, epoch)
        total += rows.chars
        if total > MAX_TEXT_CHARS:
            raise AttachmentRejected(TOO_MUCH_TEXT)
        parsed.append(rows)
    return _sheet_result(parsed)


def _worksheet_targets(archive, members):
    """Relationship id -> worksheet part. Chart sheets, macros and external links are never opened."""
    payload = _read_member(archive, members, "xl/_rels/workbook.xml.rels")
    if payload is None:
        return {}
    targets = {}
    for element in _parse_xml(payload).iter():
        if _local(element.tag) != "Relationship":
            continue
        if element.get("TargetMode") == "External" or not str(element.get("Type", "")).endswith(_WORKSHEET_REL):
            continue
        target = str(element.get("Target") or "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
        path = posixpath.normpath(path)
        if path.startswith("xl/worksheets/") and path in members:
            targets[element.get("Id") or ""] = path
    return targets


def _shared_strings(archive, members):
    payload = _read_member(archive, members, "xl/sharedStrings.xml")
    if payload is None:
        return []
    strings = []
    for item in _parse_xml(payload):
        if _local(item.tag) != "si":
            continue
        parts = []
        for child in item:
            tag = _local(child.tag)
            if tag == "t":
                parts.append(child.text or "")
            elif tag == "r":
                # A rich-text run; phonetic guides (rPh) are skipped.
                parts.extend(run.text or "" for run in child if _local(run.tag) == "t")
        strings.append(_cell_text("".join(parts)))
    return strings


def _date_styles(archive, members):
    """Indexes of cell styles whose number format shows a date or time."""
    payload = _read_member(archive, members, "xl/styles.xml")
    if payload is None:
        return frozenset()
    root = _parse_xml(payload)
    custom = {}
    styles = []
    for element in root:
        tag = _local(element.tag)
        if tag == "numFmts":
            for item in element:
                if _local(item.tag) == "numFmt":
                    custom[_int(item.get("numFmtId"))] = str(item.get("formatCode") or "")
        elif tag == "cellXfs":
            styles = [_int(item.get("numFmtId")) for item in element if _local(item.tag) == "xf"]
    return frozenset(
        index for index, format_id in enumerate(styles)
        if format_id in _DATE_FORMAT_IDS or (format_id in custom and _is_date_format(custom[format_id]))
    )


def _is_date_format(code: str) -> bool:
    if code.lower() == "general":
        return False
    stripped = _FORMAT_LITERALS.sub("", code.split(";", 1)[0]).lower()
    return any(letter in stripped for letter in "dmyhs")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _column_index(letters: str) -> int:
    index = 0
    for letter in letters.upper():
        index = index * 26 + (ord(letter) - 64)
    return index


def _read_sheet(payload, name, strings, date_styles, epoch):
    rows = _Rows(name)
    if payload is None:
        return rows
    previous_row = 0
    # The payload came through _read_member, which refused any DTD.
    for _event, element in ElementTree.iterparse(io.BytesIO(payload), events=("end",)):  # noqa: S314 - DTDs are refused
        if _local(element.tag) != "row":
            continue
        number = _int(element.get("r"), previous_row + 1)
        previous_row = number
        cells = {}
        column = 0
        for cell in element:
            if _local(cell.tag) != "c":
                continue
            match = _CELL_REF.match(str(cell.get("r") or ""))
            column = _column_index(match.group(1)) if match else column + 1
            if column > MAX_COLUMNS:
                if _cell_value(cell, strings, date_styles, epoch):
                    raise AttachmentRejected(TOO_MANY_COLUMNS)
                continue
            cells[column] = _cell_value(cell, strings, date_styles, epoch)
        element.clear()
        if cells:
            rows.add(number, [cells.get(index, "") for index in range(1, max(cells) + 1)])
    return rows


def _cell_value(cell, strings, date_styles, epoch) -> str:
    kind = cell.get("t") or "n"
    value = None
    inline = []
    for child in cell:
        tag = _local(child.tag)
        if tag == "v":
            value = child.text or ""
        elif tag == "is":
            inline.extend(item.text or "" for item in child.iter() if _local(item.tag) == "t")
    # Formulas (<f>) are never read: a cell shows only the value Excel cached for it.
    if kind == "inlineStr":
        return _cell_text("".join(inline))
    if value is None:
        return ""
    if kind == "s":
        index = _int(value, -1)
        return strings[index] if 0 <= index < len(strings) else ""
    if kind == "b":
        return "TRUE" if value.strip() == "1" else "FALSE"
    if kind in {"str", "e"}:
        return _cell_text(value)
    return _number_text(value, _int(cell.get("s"), -1) in date_styles, epoch)


def _number_text(value: str, is_date: bool, epoch) -> str:
    try:
        number = float(value)
    except ValueError:
        return _cell_text(value)
    if not math.isfinite(number):
        return _cell_text(value)
    if is_date and 0 <= number <= _MAX_SERIAL:
        moment = epoch + timedelta(days=number)
        if moment.hour or moment.minute or moment.second:
            return moment.strftime("%Y-%m-%d %H:%M")
        return moment.date().isoformat()
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.15g}"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


_PDF_WORKER = Path(__file__).with_name("chat_attachment_pdf.py")


def _parse_pdf(data: bytes):
    timeout = max(1, int(settings.CHAT_ATTACHMENT_PDF_TIMEOUT_SECONDS))
    result = _run_pdf_worker(data, timeout)
    error = result.get("error")
    if error == "password":
        raise AttachmentRejected(PASSWORD_PROTECTED)
    if error == "pages":
        raise AttachmentRejected(TOO_MANY_PAGES)
    pages = result.get("pages")
    if error or not isinstance(pages, list) or not all(isinstance(page, str) for page in pages):
        raise AttachmentRejected(UNREADABLE)
    if not any(page.strip() for page in pages):
        raise AttachmentRejected(NO_PDF_TEXT)
    page_count = _int(result.get("page_count"), len(pages))
    summary = {"pages": page_count, "truncated": bool(result.get("truncated"))}
    return {"pages": pages}, summary


def _run_pdf_worker(data: bytes, timeout: int) -> dict:
    command = [sys.executable, "-I", str(_PDF_WORKER), str(MAX_PAGES), str(MAX_TEXT_CHARS), str(timeout + 1)]
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed script under our own interpreter; no shell, no file names.
            command, input=data, capture_output=True, timeout=timeout, check=False, env={"LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        raise AttachmentRejected(PDF_TIMEOUT) from None
    except OSError:
        raise AttachmentRejected(UNREADABLE) from None
    if completed.returncode != 0:
        # Killed at its memory or CPU limit, or crashed: either way the file is not readable here.
        raise AttachmentRejected(UNREADABLE)
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AttachmentRejected(UNREADABLE) from None
    return result if isinstance(result, dict) else {"error": "unreadable"}
