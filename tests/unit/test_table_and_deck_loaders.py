"""Spreadsheets, decks and CSV files load into text a chunker can cut and a
citation can point at.

The PPTX loader reads the Open XML itself, so the test builds a deck as a
zip of the three parts it reads, with the slides listed out of file order
in the relationship list, which is how a reordered deck looks on disk.
"""

from __future__ import annotations

import zipfile
from datetime import datetime

import pytest

from vectrixdb.ingest import chunk, load, rows_to_lines

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _slide(title, paragraphs=(), table=()):
    def sp(text, ph=None):
        ph_xml = f'<p:nvPr><p:ph type="{ph}"/></p:nvPr>' if ph else "<p:nvPr/>"
        return (
            f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="x"/><p:cNvSpPr/>{ph_xml}</p:nvSpPr>'
            f"<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
        )

    body = sp(title, "title") + "".join(sp(t) for t in paragraphs)
    if table:
        rows = "".join(
            "<a:tr>" + "".join(f"<a:tc><a:txBody><a:p><a:r><a:t>{c}</a:t></a:r></a:p></a:txBody></a:tc>" for c in row) + "</a:tr>"
            for row in table
        )
        body += f"<p:graphicFrame><a:graphic><a:graphicData><a:tbl>{rows}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    return f'<?xml version="1.0"?><p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>'


def make_deck(path, slides, order=None):
    """slides: {"slide1.xml": xml, ...}; order: slide file names in presentation order."""
    order = order or sorted(slides)
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(order)))
    presentation = f'<?xml version="1.0"?><p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>'
    rels = f'<?xml version="1.0"?><Relationships xmlns="{REL}">' + "".join(
        f'<Relationship Id="rId{i + 1}" Type="x" Target="slides/{name}"/>' for i, name in enumerate(order)
    ) + "</Relationships>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", rels)
        for name, xml in slides.items():
            z.writestr(f"ppt/slides/{name}", xml)


class TestRows:
    def test_header_and_values_become_one_line_per_row(self):
        lines = rows_to_lines([["Region", "Revenue", "Note"], ["EMEA", 1200.0, None], [None, None, None], ["APAC", 950.5, "prelim"]])
        assert lines == ["Region: EMEA; Revenue: 1200", "Region: APAC; Revenue: 950.5; Note: prelim"]

    def test_without_a_text_header_rows_are_bare(self):
        assert rows_to_lines([[1, 2], [3, 4]]) == ["1; 2", "3; 4"]

    def test_dates_and_booleans_read_naturally(self):
        lines = rows_to_lines([["When", "Paid"], [datetime(2026, 9, 18), True]])
        assert lines == ["When: 2026-09-18; Paid: true"]


class TestXlsx:
    def test_sheets_become_headings_and_rows_become_lines(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        book = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sales"
        ws.append(["Region", "Revenue"])
        ws.append(["EMEA", 1200])
        ws.append(["APAC", 950])
        empty = wb.create_sheet("Empty")
        notes = wb.create_sheet("Notes")
        notes.append(["Covenant tested quarterly."])
        wb.save(str(book))
        doc = load(book)
        assert doc.metadata["kind"] == "xlsx" and doc.metadata["sheets"] == 2
        assert [h[1] for h in doc.headings] == ["Sales", "Notes"]
        assert "Region: EMEA; Revenue: 1200" in doc.text
        assert doc.heading_at(doc.text.index("APAC")) == "Sales"
        assert empty.title not in doc.text
        chunks = chunk(doc, "markdown", size=500)
        assert [c.heading for c in chunks] == ["Sales", "Notes"]


class TestPptx:
    def test_slides_are_pages_in_presentation_order_with_titles_and_tables(self, tmp_path):
        deck = tmp_path / "deck.pptx"
        make_deck(
            deck,
            {
                "slide1.xml": _slide("Second in the deck", ["Moved to the end."]),
                "slide2.xml": _slide("Results", ["Revenue grew in every region."], table=[["Region", "Revenue"], ["EMEA", "1200"]]),
            },
            order=["slide2.xml", "slide1.xml"],
        )
        doc = load(deck)
        assert doc.metadata["kind"] == "pptx" and doc.metadata["pages"] == 2
        assert [h[1] for h in doc.headings] == ["Slide 1: Results", "Slide 2: Second in the deck"]
        assert doc.page_at(doc.text.index("Moved to the end")) == 2
        assert "Region: EMEA; Revenue: 1200" in doc.text and "|" not in doc.text
        chunks = chunk(doc, "markdown", size=500)
        assert [c.page for c in chunks] == [1, 2]

    def test_a_deck_without_a_relationship_list_falls_back_to_file_order(self, tmp_path):
        deck = tmp_path / "bare.pptx"
        with zipfile.ZipFile(deck, "w") as z:
            z.writestr("ppt/slides/slide10.xml", _slide("Ten"))
            z.writestr("ppt/slides/slide2.xml", _slide("Two"))
        doc = load(deck)
        assert [h[1] for h in doc.headings] == ["Slide 1: Two", "Slide 2: Ten"]


class TestCsv:
    def test_csv_rows_read_like_sentences(self, tmp_path):
        path = tmp_path / "sales.csv"
        path.write_text("Region,Revenue\nEMEA,1200\nAPAC,950\n", encoding="utf-8")
        doc = load(path)
        assert doc.metadata["kind"] == "csv" and doc.metadata["rows"] == 2
        assert doc.text.splitlines() == ["Region: EMEA; Revenue: 1200", "Region: APAC; Revenue: 950"]


class TestThroughVectrix:
    def test_a_deck_is_searchable_and_cited_by_slide(self, tmp_path):
        from vectrixdb import Vectrix

        deck = tmp_path / "deck.pptx"
        make_deck(deck, {"slide1.xml": _slide("Bread", ["Sourdough is leavened by wild yeast."]), "slide2.xml": _slide("Rock", ["Basalt forms when lava cools."])})
        db = Vectrix("decks", path=str(tmp_path / "db"), mode="dense")
        try:
            db.add_document(deck, chunk="markdown")
            hit = db.search("sourdough starter", limit=1).top
            assert hit.citation == "deck.pptx#slide=1"
            assert hit.readable_citation == "deck.pptx, slide 1"
            assert hit.metadata["heading"] == "Slide 1: Bread"
        finally:
            db.close()

    def test_a_spreadsheet_row_is_a_keyword_hit(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        from vectrixdb import Vectrix

        book = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sales"
        ws.append(["Region", "Revenue"])
        ws.append(["EMEA", 1200])
        ws.append(["APAC", 950])
        wb.save(str(book))
        db = Vectrix("books", path=str(tmp_path / "db"), mode="hybrid")
        try:
            db.add_document(book, chunk="sentence", chunk_size=40, overlap=0)
            hit = db.search("APAC revenue", limit=1, mode="sparse").top
            assert "APAC" in hit.text and hit.citation == "book.xlsx#Sales"
        finally:
            db.close()
