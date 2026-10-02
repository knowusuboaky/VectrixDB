"""A PDF whose pages have no text layer is read, and keeps its figures.

Registering an extractor for ``.pdf`` reads a scan well and throws every
figure away, because the registry has no way to ask for images and no engine
returns any. ``page_ocr`` is the other way round: the built-in reader keeps
doing figures and the pages with no text are the only ones sent to be read.
Every reader here is a fake; nothing is drawn and nothing is called out to.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from vectrixdb.ingest import load, load_bytes


def png(width: int, height: int, filler: int = 4000) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return head + bytes(filler)


class Image:
    def __init__(self, name, data):
        self.name, self.data = name, data


class Page:
    def __init__(self, text, images=()):
        self._text, self.images = text, list(images)

    def extract_text(self):
        return self._text


@pytest.fixture
def report(tmp_path, monkeypatch):
    """A three page PDF: text, a scan, then text with a figure on it."""
    pypdf = pytest.importorskip("pypdf")
    pages = [
        Page("Revenue grew in every region this year, and costs were flat."),
        Page("   "),  # a scanned insert: nothing in the text layer
        Page("Costs were flat across the period, as the table shows.", [Image("Im1.png", png(640, 480))]),
    ]

    class Reader:
        def __init__(self, path):
            self.pages = pages

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    # Only the pages asked for are ever drawn, so this also records which.
    drawn = []

    def draw(path, wanted):
        drawn.extend(wanted)
        return [(index, png(1000, 1400)) for index in wanted]

    monkeypatch.setattr("vectrixdb.ingest._pdf_page_images", draw)
    path = tmp_path / "q3.pdf"
    path.write_bytes(b"%PDF")
    return path, drawn


class TestOnlyThePagesThatNeedItAreRead:
    def test_a_pdf_that_is_text_throughout_never_calls_the_reader(self, tmp_path, monkeypatch):
        pypdf = pytest.importorskip("pypdf")

        class Reader:
            def __init__(self, path):
                self.pages = [Page("Revenue grew in every region this year."), Page("Costs were flat across it.")]

        monkeypatch.setattr(pypdf, "PdfReader", Reader)
        path = tmp_path / "clean.pdf"
        path.write_bytes(b"%PDF")

        def never(image):
            raise AssertionError("a PDF with a text layer must cost nothing")

        doc = load(path, ocr=never)
        assert "Revenue grew" in doc.text
        assert "ocr" not in doc.metadata, "nothing was read by machine, so nothing says it was"

    def test_only_the_page_with_no_text_is_drawn_and_sent(self, report):
        path, drawn = report
        asked = []

        def read(image):
            asked.append(image)
            return ["Table 4: costs by region", "EMEA 1200"]

        doc = load(path, ocr=read)
        assert drawn == [1], "the two pages that had text were never drawn"
        assert len(asked) == 1
        assert "Table 4: costs by region" in doc.text and "EMEA 1200" in doc.text

    def test_the_pages_it_read_are_named_so_a_chunk_can_say_so(self, report):
        path, _ = report
        doc = load(path, ocr=lambda image: ["read by machine"])
        assert doc.metadata["ocr"] is True
        assert doc.metadata["pages_ocr"] == 1
        assert doc.metadata["ocr_pages"] == [2], "page numbers, one-based, as a citation uses them"

    def test_a_page_with_no_ink_is_not_sent_at_all(self, tmp_path, monkeypatch):
        """A reader shown an empty page invents text for it, so it is not shown one."""
        pypdf = pytest.importorskip("pypdf")

        class Reader:
            def __init__(self, path):
                self.pages = [Page("Revenue grew in every region this year."), Page("")]

        monkeypatch.setattr(pypdf, "PdfReader", Reader)
        monkeypatch.setattr("vectrixdb.ingest._pdf_page_images", lambda path, wanted: [(i, png(1000, 1400)) for i in wanted])
        monkeypatch.setattr("vectrixdb.extract.layout.looks_blank", lambda image, ink=0.002: True)
        path = tmp_path / "b.pdf"
        path.write_bytes(b"%PDF")

        def never(image):
            raise AssertionError("a blank page must not be sent")

        doc = load(path, ocr=never)
        assert doc.metadata["pages_blank"] == 1
        assert "ocr_pages" not in doc.metadata

    def test_a_reader_that_finds_nothing_leaves_the_page_as_it_was(self, report):
        path, _ = report
        doc = load(path, ocr=lambda image: [])
        assert "ocr_pages" not in doc.metadata, "a page nothing was read from was not read"


class TestTheFiguresSurviveIt:
    """The whole point: an extractor registered for .pdf loses these."""

    def test_a_figure_on_a_text_page_is_still_a_figure(self, report):
        path, _ = report
        doc = load(path, images=True, ocr=lambda image: ["read by machine"])
        assert [info["src"] for _offset, info in doc.figures] == ["p3-fig1.png"]
        assert "p3-fig1.png" in doc.images

    def test_reading_a_page_again_does_not_drop_what_was_on_it(self, tmp_path, monkeypatch):
        """A scanned page can carry a picture too, and OCR replaces its text, not its figures."""
        pypdf = pytest.importorskip("pypdf")

        class Reader:
            def __init__(self, path):
                self.pages = [Page("  ", [Image("Im1.png", png(640, 480))])]

        monkeypatch.setattr(pypdf, "PdfReader", Reader)
        monkeypatch.setattr("vectrixdb.ingest._pdf_page_images", lambda path, wanted: [(i, png(1000, 1400)) for i in wanted])
        path = tmp_path / "scan.pdf"
        path.write_bytes(b"%PDF")
        doc = load(path, images=True, ocr=lambda image: ["Quarterly summary", "Revenue 1200"])
        assert "Quarterly summary" in doc.text
        assert [info["src"] for _offset, info in doc.figures] == ["p1-fig1.png"]

    def test_the_figure_line_still_sits_on_its_own_page(self, report):
        path, _ = report
        doc = load(path, images=True, ocr=lambda image: ["read by machine"])
        offset = doc.figures[0][0]
        assert doc.page_at(offset) == 3


class TestEveryWayInReadsItTheSame:
    def test_load_bytes_takes_it_too(self, report):
        """A blob and a file are the same document or the pipeline has two truths."""
        path, _ = report
        doc = load_bytes(path.read_bytes(), "q3.pdf", ocr=lambda image: ["read by machine"])
        assert doc.metadata["ocr_pages"] == [2]

    def test_a_collection_carries_it_and_the_worker_reads_it_off_the_collection(self, tmp_path):
        from vectrixdb import Vectrix

        def read(image):
            return ["read by machine"]

        db = Vectrix("t", path=str(tmp_path / "db"), page_ocr=read)
        try:
            assert db.page_ocr is read
            assert getattr(db, "page_ocr", None) is read, "this is what IngestWorker.handle reads"
        finally:
            db.close()

    def test_something_that_is_not_callable_is_refused_at_the_door(self, tmp_path):
        from vectrixdb import Vectrix

        with pytest.raises(TypeError, match="page_ocr is callable"):
            Vectrix("t", path=str(tmp_path / "db"), page_ocr="rapidocr")

    def test_nothing_changes_for_a_collection_that_sets_none(self, report):
        path, drawn = report
        doc = load(path)
        assert drawn == [], "no reader means no page is drawn, which is what it cost before"
        assert "ocr" not in doc.metadata
