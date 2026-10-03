"""The readers after the audit: nothing lost, nothing leaked, nothing guessed from a name.

The audit fed every reader the files real documents turn into and found
these: a macro-enabled Word file indexed as 35,000 characters of zip bytes, a
Windows-1252 note read as "R�sum�", a semicolon CSV that split 1200,50 into
two numbers, a workbook that said it was one cell and was read as one cell,
every spreadsheet refused by the quality gate, hidden drafting notes indexed,
a masked transcript whose segments still held the phone numbers, a video with
no sound refused, a busy Speech service reported as broken, and a file served
as application/octet-stream read as a web page. Each test here is one of them.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from vectrixdb.exceptions import DependencyError, ExtractionError
from vectrixdb.ingest import _load_xls, _markdown_without_comments, load, load_bytes, media_kind_of
from vectrixdb.quality import extraction_quality


# ============================================================ what a file is ===


def docx_bytes(paragraphs=("Hello from Word.",), body_xml=None):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for words in paragraphs:
        document.add_paragraph(words)
    buffer = io.BytesIO()
    document.save(buffer)
    data = buffer.getvalue()
    if body_xml is None:
        return data
    # Swap the body for hand-written XML, for what python-docx cannot say.
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "word/document.xml":
                text = content.decode("utf-8")
                start, end = text.index("<w:body>") + len("<w:body>"), text.index("<w:sectPr")
                content = (text[:start] + body_xml + text[end:]).encode("utf-8")
            target.writestr(item, content)
    return out.getvalue()


class TestAFileIsReadAsWhatItIs:
    def test_a_macro_enabled_word_file_is_a_word_file(self, tmp_path):
        (tmp_path / "policy.docm").write_bytes(
            docx_bytes(["Remote work is allowed two days a week."])
        )
        doc = load(tmp_path / "policy.docm")
        assert (
            doc.metadata["kind"] == "docx"
            and "two days a week" in doc.text
            and "PK" not in doc.text
        )

    def test_a_docx_renamed_doc_is_a_docx(self, tmp_path):
        (tmp_path / "renamed.doc").write_bytes(docx_bytes(["Read as what it is."]))
        assert load(tmp_path / "renamed.doc").text.strip() == "Read as what it is."

    def test_rtf_named_doc_is_rtf(self, tmp_path):
        (tmp_path / "minutes.doc").write_bytes(
            b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}\\f0 Board minutes\\par The board approved the plan.\\par}"
        )
        doc = load(tmp_path / "minutes.doc")
        assert (
            doc.metadata["kind"] == "rtf"
            and "Board minutes\nThe board approved the plan." in doc.text
            and "Arial" not in doc.text
        )

    def test_a_zip_archive_is_not_a_document(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes/a.txt", "inside")
        (tmp_path / "bundle.zip").write_bytes(buffer.getvalue())
        with pytest.raises(ExtractionError, match="zip archive"):
            load(tmp_path / "bundle.zip")

    def test_binary_bytes_are_refused_and_not_indexed(self, tmp_path):
        (tmp_path / "data.bin").write_bytes(bytes(range(256)) * 4)
        with pytest.raises(ExtractionError, match="not a format this reads"):
            load(tmp_path / "data.bin")

    def test_a_name_that_promises_a_document_the_bytes_do_not_hold(self, tmp_path):
        (tmp_path / "empty.docx").write_bytes(b"")
        (tmp_path / "text.docx").write_bytes(b"just some words")
        with pytest.raises(ExtractionError, match="empty"):
            load(tmp_path / "empty.docx")
        with pytest.raises(ExtractionError, match="not the .docx file its name says"):
            load(tmp_path / "text.docx")

    def test_a_damaged_word_file_is_refused_in_words(self, tmp_path):
        data = docx_bytes()
        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(broken, "w") as target:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == "word/document.xml":
                    content = content[: len(content) // 2]
                target.writestr(item, content)
        (tmp_path / "broken.docx").write_bytes(broken.getvalue())
        with pytest.raises(ExtractionError, match="could not be read as a Word document"):
            load(tmp_path / "broken.docx")

    def test_the_first_bytes_alone_name_the_kind(self):
        assert media_kind_of(b"%PDF-1.7") == "pdf"
        assert (
            media_kind_of(b"PK\x03\x04")
            == media_kind_of(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            == "office"
        )
        assert media_kind_of(b"{\\rtf1\\ansi") == "rtf"
        assert media_kind_of(b"\x89PNG\r\n") == "image"
        assert media_kind_of(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio"
        assert media_kind_of(b"\x00\x00\x00\x18ftypmp42") == "video"
        assert media_kind_of(b"\x00\x00\x00\x18ftypM4A ") == "audio"
        assert media_kind_of(b"  <!DOCTYPE html><html>") == "html"
        assert media_kind_of(b"plain words") is None


# ============================================================ text of any encoding ===


class TestTextInTheEncodingItIsIn:
    @pytest.mark.parametrize(
        "raw",
        [
            "Résumé: café in Montréal, 5€ “quoted”.".encode("cp1252"),
            "Résumé: café in Montréal, 5€ “quoted”.".encode("utf-8"),
            b"\xef\xbb\xbf" + "Résumé: café in Montréal, 5€ “quoted”.".encode("utf-8"),
            "Résumé: café in Montréal, 5€ “quoted”.".encode("utf-16"),
        ],
        ids=["windows-1252", "utf-8", "utf-8 with a mark", "utf-16"],
    )
    def test_accents_quotes_and_the_euro_survive(self, tmp_path, raw):
        (tmp_path / "note.txt").write_bytes(raw)
        assert load(tmp_path / "note.txt").text == "Résumé: café in Montréal, 5€ “quoted”."

    def test_a_markdown_title_keeps_its_accent(self, tmp_path):
        (tmp_path / "policy.md").write_bytes("# Café policy\n\nPrix: 5€.\n".encode("cp1252"))
        assert load(tmp_path / "policy.md").metadata["title"] == "Café policy"

    def test_a_form_feed_is_where_a_page_ends(self, tmp_path):
        (tmp_path / "report.txt").write_text(
            "Page one about liquidity.\fPage two about capital.\fPage three about funding.",
            encoding="utf-8",
        )
        doc = load(tmp_path / "report.txt")
        assert doc.metadata["pages"] == 3 and [n for _o, n in doc.pages] == [1, 2, 3]

    def test_control_characters_nobody_typed_are_gone(self, tmp_path):
        (tmp_path / "t.txt").write_bytes(b"one\x01two\x07three")
        assert load(tmp_path / "t.txt").text == "onetwothree"


# ============================================================ delimited files ===


class TestADelimitedFileInWhateverItUses:
    def test_a_semicolon_file_keeps_its_decimal_commas(self, tmp_path):
        (tmp_path / "eu.csv").write_text(
            "Region;Year;Revenue\nEMEA;2024;1200,50\nAPAC;2024;950,25\n", encoding="utf-8"
        )
        assert "Region: EMEA; Year: 2024; Revenue: 1200,50" in load(tmp_path / "eu.csv").text

    def test_excels_sep_line_says_the_delimiter_and_is_not_a_header(self, tmp_path):
        (tmp_path / "excel.csv").write_text(
            "sep=;\nRegion;Revenue\nEMEA;1200\nAPAC;950\n", encoding="utf-8"
        )
        text = load(tmp_path / "excel.csv").text
        assert "Region: EMEA; Revenue: 1200" in text and "sep" not in text

    @pytest.mark.parametrize(
        "delimiter,name", [("\t", "data.tsv"), ("|", "data.csv"), ("\t", "data.csv")]
    )
    def test_tabs_and_pipes(self, tmp_path, delimiter, name):
        rows = ["Region", "Revenue"], ["EMEA", "1200"], ["APAC", "950"]
        (tmp_path / name).write_text(
            "\n".join(delimiter.join(r) for r in rows) + "\n", encoding="utf-8"
        )
        assert "Region: EMEA; Revenue: 1200" in load(tmp_path / name).text

    def test_a_windows_1252_csv(self, tmp_path):
        (tmp_path / "w.csv").write_bytes(
            "Branch,City\nCafé René,Montréal\nLes Halles,Québec\n".encode("cp1252")
        )
        assert "Branch: Café René; City: Montréal" in load(tmp_path / "w.csv").text


# ============================================================ rtf and opendocument ===


class TestRtf:
    RTF = (
        b"{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}{\\colortbl;\\red0\\green0\\blue0;}"
        b"{\\info{\\title Liquidity Policy 2025}{\\author Someone}}"
        b"\\f0 Caf\\'e9 policy\\par "
        b"Unicode \\u8364?5 and {\\v a hidden drafting note} visible again.\\par "
        b"\\trowd\\cellx2000\\cellx4000 Region\\cell Revenue\\cell\\row "
        b"\\trowd\\cellx2000\\cellx4000 EMEA\\cell 1200\\cell\\row "
        b"After the table.\\par}"
    )

    def test_words_escapes_and_unicode(self, tmp_path):
        (tmp_path / "p.rtf").write_bytes(self.RTF)
        doc = load(tmp_path / "p.rtf")
        assert "Café policy" in doc.text and "Unicode €5" in doc.text

    def test_hidden_text_and_tables(self, tmp_path):
        (tmp_path / "p.rtf").write_bytes(self.RTF)
        doc = load(tmp_path / "p.rtf")
        assert "drafting note" not in doc.text and "visible again" in doc.text
        assert "Region: EMEA; Revenue: 1200" in doc.text and "After the table." in doc.text
        assert "Arial" not in doc.text and "Someone" not in doc.text

    def test_its_title_is_its_info_title_and_nothing_after(self, tmp_path):
        (tmp_path / "p.rtf").write_bytes(self.RTF)
        assert load(tmp_path / "p.rtf").metadata["title"] == "Liquidity Policy 2025"


def odt(path, body, title=None):
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        f"<office:body><office:text>{body}</office:text></office:body></office:document-content>"
    )
    meta = (
        '<?xml version="1.0"?><office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        f'xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta><dc:title>{title or ""}</dc:title></office:meta></office:document-meta>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", content)
        archive.writestr("meta.xml", meta)


class TestOpenDocument:
    def test_headings_paragraphs_lists_and_tables(self, tmp_path):
        odt(
            tmp_path / "policy.odt",
            '<text:h text:outline-level="1">Remote work</text:h><text:p>Staff may work from home.</text:p>'
            "<text:list><text:list-item><text:p>Two days a week</text:p></text:list-item>"
            "<text:list-item><text:p>With a manager's approval</text:p></text:list-item></text:list>"
            "<table:table><table:table-row><table:table-cell><text:p>Region</text:p></table:table-cell>"
            "<table:table-cell><text:p>Revenue</text:p></table:table-cell></table:table-row>"
            "<table:table-row><table:table-cell><text:p>EMEA</text:p></table:table-cell>"
            "<table:table-cell><text:p>1200</text:p></table:table-cell></table:table-row></table:table>",
            title="Remote Work Policy",
        )
        doc = load(tmp_path / "policy.odt")
        assert doc.text.startswith("# Remote work\n\nStaff may work from home.")
        assert (
            "- Two days a week\n\n- With a manager's approval" in doc.text
            or "- Two days a week" in doc.text
        )
        assert "Region: EMEA; Revenue: 1200" in doc.text
        assert doc.metadata["title"] == "Remote Work Policy" and [h[1] for h in doc.headings] == [
            "Remote work"
        ]


# ============================================================ excel ===


def workbook_bytes(build):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    build(book)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class TestEveryRowOfAWorkbook:
    def test_a_sheet_that_says_it_is_one_cell_is_read_whole(self, tmp_path):
        def build(book):
            sheet = book.active
            for row in (["Region", "Revenue"], ["EMEA", 1200], ["APAC", 950]):
                sheet.append(row)

        data = workbook_bytes(build)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    content = content.replace(b'<dimension ref="A1:B3"/>', b'<dimension ref="A1"/>')
                target.writestr(item, content)
        (tmp_path / "exported.xlsx").write_bytes(out.getvalue())
        text = load(tmp_path / "exported.xlsx").text
        assert "Region: EMEA; Revenue: 1200" in text and "Region: APAC; Revenue: 950" in text

    def test_a_hidden_sheet_is_named_and_not_read(self, tmp_path):
        def build(book):
            book.active.title = "Summary"
            book.active.append(["Region", "Revenue"])
            book.active.append(["EMEA", 1200])
            scratch = book.create_sheet("Scratch")
            scratch.append(["Do not use", 99999])
            scratch.sheet_state = "hidden"

        (tmp_path / "book.xlsx").write_bytes(workbook_bytes(build))
        doc = load(tmp_path / "book.xlsx")
        assert "99999" not in doc.text and doc.metadata["sheets_hidden"] == ["Scratch"]

    def test_a_damaged_workbook_is_refused_in_words(self, tmp_path):
        data = workbook_bytes(lambda book: book.active.append(["a", 1]))
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
            for item in source.infolist():
                if item.filename != "xl/workbook.xml":
                    target.writestr(item, source.read(item.filename))
            target.writestr("xl/workbook.xml", b"<not a workbook")
        (tmp_path / "broken.xlsx").write_bytes(out.getvalue())
        with pytest.raises(ExtractionError):
            load(tmp_path / "broken.xlsx")

    def test_a_legacy_workbook_without_its_reader_says_what_to_install(self, tmp_path, monkeypatch):
        import builtins

        real = builtins.__import__
        monkeypatch.setattr(
            builtins,
            "__import__",
            lambda name, *a, **k: (
                (_ for _ in ()).throw(ImportError(name)) if name == "xlrd" else real(name, *a, **k)
            ),
        )
        with pytest.raises(DependencyError, match="xlrd"):
            _load_xls(tmp_path / "old.xls")


# ============================================================ hidden content ===


class TestWhatThePageDoesNotShowIsNotRead:
    def test_hidden_word_text_is_not_read(self, tmp_path):
        body = (
            '<w:p><w:r><w:t xml:space="preserve">The policy applies to all staff. </w:t></w:r>'
            "<w:r><w:rPr><w:vanish/></w:rPr><w:t>Drafting note: remove before publishing.</w:t></w:r></w:p>"
            '<w:p><w:r><w:rPr><w:vanish w:val="false"/></w:rPr><w:t>Shown, the switch is off.</w:t></w:r></w:p>'
        )
        (tmp_path / "h.docx").write_bytes(docx_bytes(body_xml=body))
        text = load(tmp_path / "h.docx").text
        assert (
            "Drafting note" not in text
            and "applies to all staff" in text
            and "Shown, the switch is off." in text
        )

    def test_a_content_control_showing_its_placeholder_is_not_read(self, tmp_path):
        body = (
            "<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent><w:p><w:r><w:t>Click or tap here to enter text.</w:t></w:r></w:p></w:sdtContent></w:sdt>"
            "<w:sdt><w:sdtPr/><w:sdtContent><w:p><w:r><w:t>Filled in by the author.</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        )
        (tmp_path / "c.docx").write_bytes(docx_bytes(body_xml=body))
        text = load(tmp_path / "c.docx").text
        assert "Click or tap" not in text and "Filled in by the author." in text

    def test_a_markdown_comment_is_not_read_but_code_is_code(self):
        text = "# Notes\n\n<!-- internal: the margin is 42% -->\n\nVisible.\n\n```html\n<!-- a comment in code stays -->\n```\n"
        cleaned = _markdown_without_comments(text)
        assert (
            "margin is 42%" not in cleaned
            and "Visible." in cleaned
            and "a comment in code stays" in cleaned
        )


# ============================================================ the quality gate ===


class TestTheGateJudgesProseNotTables:
    def test_a_clean_table_is_usable(self):
        rows = "\n".join(f"Region: R{i}; Year: 2024; Revenue: {1000 + i}" for i in range(40))
        assert extraction_quality(rows).usable

    def test_a_pdf_table_row_shape_is_usable(self):
        rows = "\n".join(
            f"Line item {i}; 2025 Q3: {i * 7:,}; 2024 Q3: ({i * 3:,})" for i in range(30)
        )
        assert extraction_quality(rows).usable

    def test_prose_with_a_semicolon_and_a_colon_is_still_prose(self):
        from vectrixdb.quality import _is_row

        assert not _is_row(
            "Rates rose sharply through the spring; however: the bank held its forecast for the year unchanged."
        )

    def test_the_words_a_chart_prints_are_not_prose(self):
        """A chart's axis values and labels on one line score as a list, not as garbled sentences: as prose, this page fails."""
        ticks = (
            "0 $8,000 6,000 7,000 4,000 5,000 3,000 1,000 2,000 2024 2025 NET INCOME (millions) 0 2,000 4,000 6,000 8,000 10,000 "
            "12,000 14,000 16,000 18,000 20,000 $22,000 150 200 300 250 350 400 450 $500 100 50 0 2024 2025"
        )
        page = "The bank grew its deposits in every region this year.\n\n[Figure: p1-chart1.png]\n"
        assert extraction_quality(page + "Words in the picture: " + ticks).usable
        assert not extraction_quality(page + "Printed: " + ticks).usable

    def test_garbled_prose_beside_a_table_still_fails(self):
        from vectrixdb.quality import degrade

        prose = "The committee reviewed the quarterly liquidity position and concluded that the bank remains well capitalised under every stress scenario considered this year."
        text = (
            "Region: EMEA; Revenue: 1200\n" + degrade(prose, 0.6, 3) + "\n" + degrade(prose, 0.6, 4)
        )
        assert not extraction_quality(text).usable


# ============================================================ media edges ===


class TestSpeechWhenItIsBusy:
    def engine(self, answers, waits):
        from vectrixdb.extract.engines import AzureSpeech

        def transport(method, url, headers, body, timeout):
            return answers.pop(0)

        ok = (
            200,
            {},
            json.dumps(
                {
                    "phrases": [
                        {
                            "offsetMilliseconds": 0,
                            "durationMilliseconds": 1000,
                            "text": "Hello.",
                            "locale": "fr-CA",
                        }
                    ]
                }
            ).encode(),
        )
        return AzureSpeech(
            "https://s.cognitiveservices.azure.com", "k", transport=transport, sleep=waits.append
        ), ok

    def test_a_busy_answer_is_asked_again_after_the_wait_it_names(self):
        waits = []
        engine, ok = self.engine([], waits)
        engine._transport = lambda *a: answers.pop(0)
        answers = [(429, {"Retry-After": "3"}, b"quota"), (503, {}, b"busy"), ok]
        doc = engine(b"sound", "a.wav")
        assert doc.text.strip().endswith("Hello.") and waits == [3.0, 4.0]
        assert doc.metadata["language"] == "fr-CA", "the language it heard"

    def test_still_busy_after_the_retries_is_a_busy_service(self):
        waits = []
        engine, _ok = self.engine([], waits)
        engine._transport = lambda *a: (429, {}, b"quota")
        with pytest.raises(ExtractionError) as said:
            engine(b"sound", "a.wav")
        assert said.value.status == 429 and len(waits) == engine.retries

    def test_several_languages_are_several_locales(self):
        from vectrixdb.extract.engines import AzureSpeech
        from vectrixdb.extract.youtube import _listening_in

        shared = AzureSpeech("https://s.cognitiveservices.azure.com", "k")
        made = _listening_in(shared, "en-US, fr-CA")
        assert made.locales == ["en-US", "fr-CA"] and shared.locales == ["en-US"]


class TestAVideoWithNoSound:
    def test_it_has_no_speech_and_is_not_broken(self, monkeypatch):
        pytest.importorskip("imageio_ffmpeg")
        import subprocess

        from vectrixdb.extract.engines import Video

        class Done:
            returncode = 1
            stderr = b"Input #0, mov,mp4: Stream #0:0: Video: h264, 1280x720\nOutput file #0 does not contain any stream"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
        heard = []
        doc = Video(audio=lambda data, name: heard.append(name))(
            b"\x00\x00\x00\x18ftypmp42", "slides.mp4"
        )
        assert (
            doc.text == ""
            and doc.metadata["speech"] == "none"
            and doc.metadata["sound_track"] is False
            and heard == []
        )


# ============================================================ the extraction app ===


fastapi = pytest.importorskip("fastapi")


def one_line_pdf(text: str) -> bytes:
    """A one-page PDF with one line of Helvetica on it, written by hand so no drawing library is needed."""
    content = f"BT /F1 11 Tf 72 692 Td ({text}) Tj ET".encode("latin-1")
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )


@pytest.fixture
def app(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from vectrixdb.api.extraction import ExtractionService, MemoryJobs, create_extraction_app
    from vectrixdb.extract.engines import AzureSpeech
    from vectrixdb.ingest import LoadedDocument

    monkeypatch.setenv("VECTRIXDB_API_KEY", "k")
    for name in (
        "VECTRIXDB_SIGNIN",
        "VECTRIXDB_ALLOW_OPEN",
        "VECTRIXDB_EXTRACT_PREFIX",
        "VECTRIXDB_EXTRACT_GATEWAY_PATHS",
    ):
        monkeypatch.delenv(name, raising=False)

    def speech_network(method, url, headers, body, timeout):
        phrases = [
            {
                "offsetMilliseconds": 0,
                "durationMilliseconds": 3000,
                "text": "Call me at 416-555-0198 or sarah.mitchell@example.com.",
            }
        ]
        return 200, {}, json.dumps({"phrases": phrases}).encode()

    pdf_bytes = one_line_pdf("Served as octet-stream, read as a PDF.")
    pages = {
        "https://files.example.test/download?id=7": (
            200,
            {"Content-Type": "application/octet-stream"},
            pdf_bytes,
        )
    }

    class Inline:
        def submit(self, fn, *args):
            fn(*args)

    service = ExtractionService(
        image=lambda data, name: LoadedDocument(text="picture"),
        audio=AzureSpeech("https://s.cognitiveservices.azure.com", "k", transport=speech_network),
        url_hosts=("files.example.test",),
        jobs=MemoryJobs(tmp_path / "out", executor=Inline()),
        transport=lambda method, url, headers, body, timeout: pages.get(url, (404, {}, b"no")),
    )
    return TestClient(create_extraction_app(service))


class TestTheAppAfterTheAudit:
    def test_a_masked_json_reply_masks_every_segment_too(self, app):
        reply = app.post(
            "/transcribe/audio?mask=1",
            content=b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(32),
            headers={"api-key": "k", "X-Filename": "call.wav", "Accept": "application/json"},
        )
        assert reply.status_code == 200, reply.text
        body = json.dumps(reply.json())
        assert "416-555" not in body and "sarah.mitchell" not in body

    def test_a_file_served_as_octet_stream_is_read_by_its_bytes(self, app):
        pytest.importorskip("pypdfium2")
        reply = app.post(
            "/transcribe/auto",
            json={"url": "https://files.example.test/download?id=7"},
            headers={"api-key": "k"},
        )
        assert reply.status_code == 200, reply.text
        assert "read as a PDF" in reply.text and "%PDF" not in reply.text
