"""Hostile-file parsing for chat attachments (#644). Every fixture is synthetic and built in memory."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from finance import chat_attachment_parsing as parsing
from finance import chat_attachment_pdf
from finance.chat_attachment_parsing import AttachmentRejected, parse_upload
from tests.chat_attachment_fixtures import (
    BUDGET_CSV,
    MAIN_NS,
    budget_xlsx,
    encrypted_office_file,
    legacy_xls_file,
    make_pdf,
    make_xlsx,
    rows_xml,
    upload,
)


def _rows(parsed, sheet=0):
    return parsed.content["sheets"][sheet]["rows"]


def _rejected(name, data, message):
    with pytest.raises(AttachmentRejected) as caught:
        parse_upload(upload(name, data))
    assert str(caught.value) == message


# --- CSV -------------------------------------------------------------------


def test_csv_rows_keep_source_row_numbers_and_skip_blank_rows():
    parsed = parse_upload(upload("budget.csv", BUDGET_CSV.encode()))
    assert parsed.kind == "csv"
    assert _rows(parsed) == [
        [1, ["Category", "Monthly budget"]],
        [2, ["Groceries", "400"]],
        [3, ["Dining", "150"]],
        [5, ["Rent", "1200"]],
    ]
    assert parsed.summary == {"sheets": [{"name": "CSV", "rows": 4, "columns": 2}]}
    assert parsed.size_bytes == len(BUDGET_CSV.encode())


def test_csv_sniffs_semicolons_and_decodes_windows_text():
    data = "Kategorie;Betrag\nCaf\xe9;12,50\n".encode("cp1252")
    parsed = parse_upload(upload("export.CSV", data))
    assert _rows(parsed) == [[1, ["Kategorie", "Betrag"]], [2, ["Café", "12,50"]]]


def test_csv_cells_are_truncated_and_trailing_blanks_dropped():
    data = ("A," + "x" * 5000 + ",,\n").encode()
    row = _rows(parse_upload(upload("long.csv", data)))[0][1]
    assert len(row) == 2
    assert len(row[1]) == parsing.MAX_CELL_CHARS and row[1].endswith("…")


def test_csv_name_with_binary_bytes_is_unsupported():
    _rejected("budget.csv", b"Category\x00,Amount", parsing.UNSUPPORTED)


def test_csv_with_an_unterminated_huge_field_is_unreadable():
    _rejected("broken.csv", b'"' + b"x" * 200_000, parsing.UNREADABLE)


@pytest.mark.parametrize("name", ["notes.txt", "budget.html", "budget"])
def test_text_under_another_name_is_unsupported(name):
    _rejected(name, BUDGET_CSV.encode(), parsing.UNSUPPORTED)


def test_csv_limits(monkeypatch):
    monkeypatch.setattr(parsing, "MAX_ROWS", 3)
    _rejected("rows.csv", b"a\nb\nc\nd\n", parsing.TOO_MANY_ROWS)
    _rejected("cols.csv", (",".join(["v"] * 101) + "\n").encode(), parsing.TOO_MANY_COLUMNS)
    monkeypatch.setattr(parsing, "MAX_TEXT_CHARS", 10)
    _rejected("text.csv", b"abcdef\nghijkl\n", parsing.TOO_MUCH_TEXT)


def test_empty_and_blank_files_are_refused():
    _rejected("empty.csv", b"", parsing.EMPTY)
    _rejected("blank.csv", b"\n,,\n\n", parsing.EMPTY)


# --- size ------------------------------------------------------------------


def test_over_the_size_cap_is_refused_by_declared_and_by_read_size(settings):
    settings.CHAT_ATTACHMENT_MAX_BYTES = 10
    _rejected("big.csv", b"a,b\n" * 10, parsing.TOO_LARGE)

    class LyingUpload(io.BytesIO):
        name = "big.csv"
        size = 1

    with pytest.raises(AttachmentRejected, match="larger than 10 MB"):
        parse_upload(LyingUpload(b"a,b\n" * 10))


# --- XLSX ------------------------------------------------------------------


def test_xlsx_reads_cached_values_only():
    shared = '<si><t>Category</t></si><si><r><t>Gro</t></r><r><t>ceries</t></r><rPh><t>ignored</t></rPh></si>'
    sheet = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Amount</t></is></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><f>SECRET_FORMULA()</f><v>400</v></c>'
        '<c r="C2" t="b"><v>1</v></c><c r="D2" t="e"><v>#DIV/0!</v></c><c r="E2" t="str"><f>A2</f><v>Groceries</v></c></row>'
        '<row r="4"><c t="inlineStr"><is><t>no ref</t></is></c><c><v>0.1</v></c><c t="b"><v>0</v></c></row>'
        '<row r="5"><c r="A5" t="s"><v>99</v></c><c r="B5"/></row>'
    )
    parsed = parse_upload(upload("budget.xlsx", make_xlsx([("Budget", sheet)], shared_strings=shared)))
    assert parsed.kind == "xlsx"
    assert _rows(parsed) == [
        [1, ["Category", "Amount"]],
        [2, ["Groceries", "400", "TRUE", "#DIV/0!", "Groceries"]],
        [4, ["no ref", "0.1", "FALSE"]],
    ]
    assert "SECRET_FORMULA" not in json.dumps(parsed.content)


def test_xlsx_formats_dates_by_style_including_1904_workbooks():
    styles = (
        '<numFmts><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/><numFmt numFmtId="165" formatCode="&quot;$&quot;#,##0"/>'
        '<numFmt numFmtId="166" formatCode="General"/></numFmts>'
        '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="165"/><xf numFmtId="22"/>'
        '<xf numFmtId="166"/></cellXfs>'
    )
    sheet = (
        '<row r="1"><c r="A1" s="1"><v>46023</v></c><c r="B1" s="2"><v>46024</v></c><c r="C1" s="3"><v>46023</v></c>'
        '<c r="D1" s="4"><v>46023.5</v></c><c r="E1" s="5"><v>7</v></c><c r="F1" s="1"><v>-5</v></c>'
        '<c r="G1"><v>1234.5600000000001</v></c><c r="H1"><v>abc</v></c><c r="I1"><v>NaN</v></c></row>'
    )
    parsed = parse_upload(upload("dates.xlsx", make_xlsx([("Dates", sheet)], styles=styles)))
    assert _rows(parsed)[0][1] == [
        "2026-01-01", "2026-01-02", "46023", "2026-01-01 12:00", "7", "-5", "1234.56", "abc", "NaN",
    ]
    parsed = parse_upload(upload("dates.xlsx", make_xlsx([("Dates", '<row r="1"><c r="A1" s="1"><v>0</v></c></row>')],
                                                      styles=styles, date1904=True)))
    assert _rows(parsed)[0][1] == ["1904-01-01"]


def test_xlsx_never_opens_macros_external_links_or_chart_sheets():
    marker = "SYNTHETIC-MARKER-NEVER-READ"
    extra_rels = (
        '<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" '
        'Target="externalLinks/externalLink1.xml"/>'
        '<Relationship Id="rIdE" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="https://example.invalid/sheet.xml" TargetMode="External"/>'
        '<Relationship Id="rIdOut" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="../../outside.xml"/>'
    )
    extra = {
        "xl/vbaProject.bin": marker.encode(),
        "xl/externalLinks/externalLink1.xml": f'<externalLink xmlns="{MAIN_NS}"><v>{marker}</v></externalLink>',
        "outside.xml": f'<worksheet xmlns="{MAIN_NS}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{marker}</t></is></c></row></sheetData></worksheet>',
    }
    data = make_xlsx([("Budget", rows_xml([["Rent", 1200]]))], extra_rels=extra_rels, extra_members=extra)
    parsed = parse_upload(upload("macro.xlsx", data))
    assert marker not in json.dumps(parsed.content)
    assert [sheet["name"] for sheet in parsed.summary["sheets"]] == ["Budget"]


def test_xlsx_with_absolute_targets_and_several_sheets():
    data = make_xlsx([("One", rows_xml([["a"]])), ("Two\nlines", rows_xml([["b"], ["c"]]))])
    parsed = parse_upload(upload("two.xlsx", data))
    assert parsed.summary["sheets"] == [
        {"name": "One", "rows": 1, "columns": 1},
        {"name": "Two lines", "rows": 2, "columns": 1},
    ]


def test_xlsx_with_a_dtd_is_refused():
    data = make_xlsx([("Budget", rows_xml([["x"]]))], sheet_xml_prefix='<!DOCTYPE worksheet [<!ENTITY a "aaaa">]>')
    _rejected("dtd.xlsx", data, parsing.UNREADABLE)


def test_xlsx_zip_bomb_is_refused_from_its_directory(monkeypatch):
    monkeypatch.setattr(parsing, "MAX_ZIP_MEMBER_BYTES", 1000)
    padded = make_xlsx([("Budget", rows_xml([["x"]]))], extra_members={"xl/media/pad.bin": b"\x00" * 5000})
    _rejected("bomb.xlsx", padded, parsing.TOO_MUCH_TEXT)
    monkeypatch.setattr(parsing, "MAX_ZIP_MEMBER_BYTES", 10_000)
    monkeypatch.setattr(parsing, "MAX_ZIP_TOTAL_BYTES", 5100)
    _rejected("bomb.xlsx", padded, parsing.TOO_MUCH_TEXT)
    monkeypatch.setattr(parsing, "MAX_ZIP_MEMBERS", 3)
    _rejected("many.xlsx", padded, parsing.UNREADABLE)


def test_xlsx_member_that_inflates_past_its_cap_is_refused(monkeypatch):
    data = make_xlsx([("Budget", rows_xml([["x" * 50] for _ in range(50)]))])
    monkeypatch.setattr(parsing, "_checked_members", lambda archive: {info.filename: info for info in archive.infolist()})
    monkeypatch.setattr(parsing, "MAX_ZIP_MEMBER_BYTES", 400)
    _rejected("lying.xlsx", data, parsing.TOO_MUCH_TEXT)


def test_xlsx_limits(monkeypatch):
    _rejected("cols.xlsx", make_xlsx([("S", '<row r="1"><c r="DA1" t="inlineStr"><is><t>far</t></is></c></row>')]),
              parsing.TOO_MANY_COLUMNS)
    blank_far = make_xlsx([("S", '<row r="1"><c r="A1"><v>1</v></c><c r="DA1"/></row>')])
    assert _rows(parse_upload(upload("ok.xlsx", blank_far))) == [[1, ["1"]]]
    monkeypatch.setattr(parsing, "MAX_SHEETS", 1)
    _rejected("sheets.xlsx", make_xlsx([("A", rows_xml([["a"]])), ("B", rows_xml([["b"]]))]), parsing.TOO_MANY_SHEETS)
    monkeypatch.setattr(parsing, "MAX_SHEETS", 20)
    monkeypatch.setattr(parsing, "MAX_TEXT_CHARS", 5)
    _rejected("text.xlsx", make_xlsx([("A", rows_xml([["abc"]])), ("B", rows_xml([["def"]]))]), parsing.TOO_MUCH_TEXT)


def test_xlsx_password_and_legacy_workbooks_are_refused_with_their_own_message():
    _rejected("protected.xlsx", encrypted_office_file(), parsing.PASSWORD_PROTECTED)
    _rejected("old.xls", legacy_xls_file(), parsing.LEGACY_XLS)


def test_encrypted_zip_member_is_treated_as_password_protected():
    data = bytearray(budget_xlsx())
    # Set the encryption bit on every local and central directory header.
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (index := data.find(signature, start)) != -1:
            data[index + offset] |= 0x1
            start = index + 4
    _rejected("zip.xlsx", bytes(data), parsing.PASSWORD_PROTECTED)


def test_other_zip_files_and_broken_workbooks():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
    _rejected("letter.docx", buffer.getvalue(), parsing.UNSUPPORTED)
    _rejected("broken.xlsx", b"PK\x03\x04not really a zip", parsing.UNREADABLE)
    no_sheets = make_xlsx([])
    _rejected("none.xlsx", no_sheets, parsing.EMPTY)
    bad_xml = make_xlsx([("S", "<row><c")])
    _rejected("bad.xlsx", bad_xml, parsing.UNREADABLE)


# --- PDF -------------------------------------------------------------------


def test_pdf_text_is_extracted_page_by_page_in_a_child_process():
    data = make_pdf(["Synthetic budget\nRent 1200", "Page two text"])
    parsed = parse_upload(upload("statement.pdf", data))
    assert parsed.kind == "pdf"
    assert parsed.content == {"pages": ["Synthetic budget\nRent 1200", "Page two text"]}
    assert parsed.summary == {"pages": 2, "truncated": False}


def test_pdf_that_needs_a_password_is_refused_but_owner_only_restrictions_are_read():
    _rejected("locked.pdf", make_pdf(["secret"], user_password="synthetic-pw"), parsing.PASSWORD_PROTECTED)
    parsed = parse_upload(upload("restricted.pdf", make_pdf(["opens without a password"], owner_password="owner-pw")))
    assert parsed.content["pages"] == ["opens without a password"]


def test_pdf_page_cap_and_empty_text(monkeypatch):
    monkeypatch.setattr(parsing, "MAX_PAGES", 1)
    _rejected("long.pdf", make_pdf(["one", "two"]), parsing.TOO_MANY_PAGES)
    monkeypatch.setattr(parsing, "MAX_PAGES", 5)
    _rejected("scan.pdf", make_pdf([""]), parsing.NO_PDF_TEXT)
    _rejected("broken.pdf", b"%PDF-1.7\nnot a pdf at all", parsing.UNREADABLE)


def test_pdf_time_cap_kills_a_slow_parse(monkeypatch, tmp_path, settings):
    slow = tmp_path / "slow_worker.py"
    slow.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(parsing, "_PDF_WORKER", slow)
    settings.CHAT_ATTACHMENT_PDF_TIMEOUT_SECONDS = 1
    _rejected("slow.pdf", make_pdf(["x"]), parsing.PDF_TIMEOUT)


@pytest.mark.parametrize("script", [
    "import sys\nsys.exit(3)\n",
    "print('not json')\n",
    "print('[1, 2]')\n",
    "print('{\"pages\": [1]}')\n",
])
def test_pdf_worker_failures_are_unreadable(monkeypatch, tmp_path, script):
    worker = tmp_path / "worker.py"
    worker.write_text(script)
    monkeypatch.setattr(parsing, "_PDF_WORKER", worker)
    _rejected("odd.pdf", make_pdf(["x"]), parsing.UNREADABLE)


def test_pdf_worker_that_cannot_start_is_unreadable(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("synthetic")

    monkeypatch.setattr(parsing.subprocess, "run", refuse)
    _rejected("odd.pdf", make_pdf(["x"]), parsing.UNREADABLE)


def test_pdf_extract_caps_characters_and_reports_errors():
    data = make_pdf(["abcdef", "ghijkl", "mnopqr"])
    assert chat_attachment_pdf.extract(data, 10, 8) == {"pages": ["abcdef", "gh"], "page_count": 3, "truncated": True}
    assert chat_attachment_pdf.extract(data, 10, 12) == {
        "pages": ["abcdef", "ghijkl"], "page_count": 3, "truncated": True,
    }
    assert chat_attachment_pdf.extract(data, 2, 100) == {"error": "pages", "pages": 3}
    assert chat_attachment_pdf.extract(b"%PDF-garbage", 2, 100) == {"error": "unreadable"}
    locked = make_pdf(["x"], user_password="pw")
    assert chat_attachment_pdf.extract(locked, 2, 100) == {"error": "password"}


def test_pdf_truncated_summary_is_kept(monkeypatch):
    monkeypatch.setattr(parsing, "_run_pdf_worker", lambda data, timeout: {"pages": ["abc"], "page_count": 4, "truncated": True})
    parsed = parse_upload(upload("cut.pdf", make_pdf(["x"])))
    assert parsed.summary == {"pages": 4, "truncated": True}


def test_pdf_worker_main_reads_stdin_and_writes_json(monkeypatch):
    limits = []
    monkeypatch.setattr(chat_attachment_pdf, "_limit_resources", limits.append)
    out = io.StringIO()
    status = chat_attachment_pdf.main(["worker", "5", "100", "7"], stdin=io.BytesIO(make_pdf(["hello"])), stdout=out)
    assert status == 0
    assert json.loads(out.getvalue())["pages"] == ["hello"]
    assert limits == [7]


def test_pdf_worker_limits_its_own_resources(monkeypatch):
    calls = []
    fake = SimpleNamespace(RLIMIT_AS="as", RLIMIT_CPU="cpu", setrlimit=lambda kind, value: calls.append((kind, value)))
    monkeypatch.setitem(__import__("sys").modules, "resource", fake)
    chat_attachment_pdf._limit_resources(9)
    assert calls == [("as", (chat_attachment_pdf.MEMORY_BYTES,) * 2), ("cpu", (9, 9))]


def test_names_are_one_plain_line():
    assert parsing.safe_name("C:\\Users\\x\\bud\nget\x07.csv") == "bud get .csv"
    assert parsing.safe_name("../..") == "attachment"
    assert parsing.safe_name("") == "attachment"
    assert len(parsing.safe_name("a" * 400 + ".csv")) == parsing.MAX_NAME_CHARS
