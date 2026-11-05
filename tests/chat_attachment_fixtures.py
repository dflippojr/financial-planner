"""Synthetic chat attachments (#644), built in memory. No real file is ever committed."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORKSHEET_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
BUDGET_CSV = "Category,Monthly budget\nGroceries,400\nDining,150\n\nRent,1200\n"


def upload(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def cell(ref, value, *, style=None):
    """One cell: str values become inline strings, numbers stay numbers."""
    s_attr = f' s="{style}"' if style is not None else ""
    if isinstance(value, str):
        return f'<c r="{ref}" t="inlineStr"{s_attr}><is><t>{escape(value)}</t></is></c>'
    return f'<c r="{ref}"{s_attr}><v>{value}</v></c>'


def rows_xml(rows):
    """sheetData rows from Python lists, starting at row 1 and column A."""
    out = []
    for number, values in enumerate(rows, start=1):
        cells = "".join(cell(f"{chr(64 + index)}{number}", value) for index, value in enumerate(values, start=1))
        out.append(f'<row r="{number}">{cells}</row>')
    return "".join(out)


def make_xlsx(sheets, *, shared_strings=None, styles=None, date1904=False, extra_rels="", extra_members=None,
              sheet_xml_prefix=""):
    """A minimal workbook. ``sheets`` is [(name, sheetData inner XML)]."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>'
        ))
        sheet_tags = "".join(
            f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{index}" r:id="rId{index}"/>'
            for index, (name, _xml) in enumerate(sheets, start=1)
        )
        workbook_pr = '<workbookPr date1904="1"/>' if date1904 else ""
        archive.writestr("xl/workbook.xml", (
            f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
            f"{workbook_pr}<sheets>{sheet_tags}</sheets></workbook>"
        ))
        rels = "".join(
            f'<Relationship Id="rId{index}" Type="{WORKSHEET_TYPE}" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr("xl/_rels/workbook.xml.rels", (
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_REL_NS}">{rels}{extra_rels}</Relationships>'
        ))
        for index, (_name, data) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", (
                f'{sheet_xml_prefix}<worksheet xmlns="{MAIN_NS}"><sheetData>{data}</sheetData></worksheet>'
            ))
        if shared_strings is not None:
            archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN_NS}">{shared_strings}</sst>')
        if styles is not None:
            archive.writestr("xl/styles.xml", f'<styleSheet xmlns="{MAIN_NS}">{styles}</styleSheet>')
        for path, payload in (extra_members or {}).items():
            archive.writestr(path, payload)
    return buffer.getvalue()


def budget_xlsx():
    return make_xlsx([("Budget", rows_xml([["Category", "Monthly budget"], ["Groceries", 400], ["Dining", 150]]))])


def make_pdf(pages, *, user_password=None, owner_password=None):
    """A PDF whose pages carry the given text in Helvetica; optionally encrypted."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for text in pages:
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        lines = " T* ".join(
            "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj"
            for line in text.split("\n")
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 14 TL 72 720 Td {lines} ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if user_password is not None or owner_password:
        writer.encrypt(user_password or "", owner_password, algorithm="AES-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_office_file():
    """The start of an OLE container holding an EncryptionInfo stream, as Excel writes a protected .xlsx."""
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
    return header + "EncryptionInfo".encode("utf-16-le") + b"\x00" * 64


def legacy_xls_file():
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504 + "Workbook".encode("utf-16-le") + b"\x00" * 64
