"""Citations a person can follow: the printed page, the range, the slide, the second.

A PDF's link keeps the page's place in the file, ``report.pdf#page=41``,
which opens the right page in any viewer. Beside it a person is shown what
the page says, ``report.pdf, pp. 39-40``, from the PDF's own page-label
table: in the TD annual report every printed number is two behind the
page's place, because the covers are C1 and C2. A recording is cited by
the second and a deck by the slide. And a file read through a service is
kept under its own address, not the name the service was sent.
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest

from vectrixdb.citations import citation_for, readable_citation_for, readable_citation_of
from vectrixdb.ingest import LoadedDocument, load, load_bytes, pdf_page_labels, prepare_document

LABELS = {1: "C1", 2: "C2", 3: "1", 4: "2", 5: "3"}
BLOB = "https://acct.blob.core.windows.net/ingestion/raw/financial/td/report.pdf"


def labelled_pdf(pages: int = 5) -> bytes:
    """Blank pages printed C1, C2, then 1, 2, 3, the way the TD report numbers its own."""
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    writer.set_page_label(0, 1, prefix="C", style="/D", start=1)
    writer.set_page_label(2, pages - 1, style="/D", start=1)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def plain_pdf(pages: int = 3) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


#: A page each, each short enough to be a chunk of 40 and two too long to be one.
PAGES = [
    "Annual report for the year 2025.",
    "Inside the front cover of it.",
    "Revenue grew in every region.",
    "Costs were flat for the period.",
    "The covenant is tested quarterly.",
]


def paged(texts=PAGES, labels=None, name="report.pdf") -> LoadedDocument:
    """A document of these pages, as a PDF reader gives one."""
    offsets, offset = [], 0
    for n, text in enumerate(texts, start=1):
        offsets.append((offset, n))
        offset += len(text) + 2
    return LoadedDocument(
        text="\n\n".join(texts),
        pages=offsets,
        metadata={"filename": name, "source": name},
        page_labels=dict(labels or {}),
    )


def reply(data: bytes, name: str, source=None):
    """What the extraction app answers for a PDF: text a page at a time, and its own name for the file."""
    import pypdf

    count = len(pypdf.PdfReader(io.BytesIO(data)).pages)
    texts = (
        PAGES[:count] if count <= len(PAGES) else [f"Page {n} text." for n in range(1, count + 1)]
    )
    doc = paged(texts, name=name)
    return {
        "text": doc.text,
        "pages": [list(p) for p in doc.pages],
        "headings": [],
        "metadata": {"source": name, "kind": "pdf"},
    }


def cut(doc, size=40):
    return prepare_document(doc, "td/report.pdf", chunk="recursive", chunk_size=size, overlap=0)


# ================================================================ the table ===


class TestThePrintedNumbersComeFromThePdf:
    def test_a_pdf_that_numbers_its_own_pages(self):
        assert pdf_page_labels(labelled_pdf()) == LABELS

    def test_a_pdf_numbered_one_two_three_has_none(self):
        assert pdf_page_labels(plain_pdf()) == {}

    def test_what_is_not_a_pdf_has_none(self):
        assert pdf_page_labels(b"not a pdf at all") == {}

    def test_the_reader_keeps_them(self, tmp_path):
        path = tmp_path / "report.pdf"
        path.write_bytes(labelled_pdf())
        assert load(path).page_labels == LABELS


class TestTheFrontMatter:
    def test_they_are_written_and_read_back(self):
        doc = paged(labels=LABELS)
        kept = doc.to_markdown()
        assert 'page_labels: {"1": "C1", "2": "C2", "3": "1", "4": "2", "5": "3"}' in kept
        assert LoadedDocument.from_markdown(kept).page_labels == LABELS

    def test_a_file_without_them_is_written_as_before(self):
        assert "page_labels" not in paged().to_markdown()

    def test_a_value_that_is_not_a_table_is_left_out(self):
        kept = (
            paged(labels=LABELS)
            .to_markdown()
            .replace(
                'page_labels: {"1": "C1", "2": "C2", "3": "1", "4": "2", "5": "3"}',
                "page_labels: [1, 2]",
            )
        )
        assert LoadedDocument.from_markdown(kept).page_labels == {}

    def test_describing_a_figure_keeps_them(self):
        from vectrixdb.ingest import describe_figures

        doc = paged(["Revenue by region.\n\n[Figure: p1-fig1.png]"], labels={1: "39"})
        doc.figures = [
            (
                doc.text.index("[Figure"),
                {"caption": "p1-fig1.png", "src": "p1-fig1.png", "described": False},
            )
        ]
        doc.images = {"p1-fig1.png": b"\x89PNG" + bytes(range(256)) * 40}
        described = describe_figures(
            doc,
            lambda data, context: {"caption": "Revenue by region", "description": "A bar chart."},
            skip_decorative=False,
        )
        assert "[Figure: Revenue by region]" in described.text and described.page_labels == {
            1: "39"
        }


# ================================================================ the chunks ===


class TestEveryChunkSaysWhatItsPageSays:
    def test_a_chunk_carries_the_printed_number_and_keeps_the_place_as_its_link(self):
        prepared = cut(paged(labels=LABELS))
        on_page_3 = next(m for m in prepared.metadata if m["page"] == 3)
        assert on_page_3["page_label"] == "1"
        assert on_page_3["_vx_citation"] == "report.pdf#page=3", (
            "the link opens the right page in any viewer"
        )
        assert on_page_3["_vx_readable_citation"] == "report.pdf, p. 1"

    def test_a_chunk_that_runs_onto_the_next_page_names_both(self):
        prepared = cut(paged(labels=LABELS), size=1000)
        whole = prepared.metadata[0]
        assert (whole["page"], whole["page_end"], whole["page_label"], whole["page_label_end"]) == (
            1,
            5,
            "C1",
            "3",
        )
        assert whole["_vx_readable_citation"] == "report.pdf, pp. C1-3"
        assert whole["_vx_citation"] == "report.pdf#page=1"

    def test_without_a_table_a_person_is_shown_the_place(self):
        prepared = cut(paged(), size=1000)
        assert "page_label" not in prepared.metadata[0]
        assert prepared.metadata[0]["_vx_readable_citation"] == "report.pdf, pp. 1-5"
        assert cut(paged()).metadata[2]["_vx_readable_citation"] == "report.pdf, p. 3"

    def test_printed_numbers_at_both_ends_or_at_neither(self):
        assert (
            readable_citation_for(
                "r.pdf", "x", page=3, page_end=4, page_label="1", page_label_end=None
            )
            == "r.pdf, pp. 3-4"
        )

    def test_the_chunk_file_carries_them(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix(
            "docs", path=str(tmp_path / "db"), embed_fn=embed, dimension=8, keep_chunks=True
        )
        try:
            db.add_document(
                paged(labels=LABELS),
                doc_id="td/report.pdf",
                chunk="recursive",
                chunk_size=40,
                overlap=0,
            )
            lines = db.kept_chunks.get("td/report.pdf")
            assert [c["metadata"]["page_label"] for c in lines] == ["C1", "C2", "1", "2", "3"]
            assert lines[2]["metadata"]["_vx_readable_citation"] == "report.pdf, p. 1"
        finally:
            db.close()


# ================================================================ recordings and slides ===


def embed(texts):
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, hash(word) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


class TestARecordingIsCitedByTheSecond:
    def call(self):
        from vectrixdb.extract.engines import segments_to_document

        doc = segments_to_document(
            [
                (0.0, 4.0, "Welcome to the call."),
                (61.2, 65.0, "The covenant is tested quarterly."),
                (665.4, 670.2, "Revenue is up."),
            ]
        )
        doc.metadata["filename"] = "call.wav"
        return doc

    def test_each_chunk_says_when_it_was_said(self):
        prepared = prepare_document(
            self.call(), "call.wav", chunk="recursive", chunk_size=34, overlap=0
        )
        revenue = next(m for t, m in zip(prepared.texts, prepared.metadata) if "Revenue" in t)
        assert (revenue["start_seconds"], revenue["end_seconds"]) == (665.4, 670.2)
        assert revenue["_vx_citation"] == "call.wav#t=665", (
            "how a media player is told where to start"
        )
        assert revenue["_vx_readable_citation"] == "call.wav, 11:05"
        first = prepared.metadata[0]
        assert (
            first["_vx_citation"] == "call.wav#t=0"
            and first["_vx_readable_citation"] == "call.wav, 0:00"
        )

    def test_the_second_wins_over_the_minute(self):
        """A recording's pages are its minutes, and page 12 of a WAV file is not somewhere a player can go."""
        assert citation_for("call.wav", "x", page=12, seconds=665.4) == "call.wav#t=665"

    def test_a_long_one_shows_hours(self):
        assert readable_citation_for("hearing.mp4", "x", seconds=3725) == "hearing.mp4, 1:02:05"

    def test_the_kept_transcript_cuts_the_same(self):
        doc = self.call()
        again = LoadedDocument.from_markdown(doc.to_markdown())
        one = prepare_document(doc, "call.wav", chunk="recursive", chunk_size=34, overlap=0)
        two = prepare_document(again, "call.wav", chunk="recursive", chunk_size=34, overlap=0)
        assert [m["_vx_citation"] for m in one.metadata] == [
            m["_vx_citation"] for m in two.metadata
        ]


class TestADeckIsCitedBySlide:
    def test_the_link_and_the_readable_form(self):
        assert citation_for("deck.pptx", "x", page=3) == "deck.pptx#slide=3"
        assert readable_citation_for("deck.pptx", "x", page=3) == "deck.pptx, slide 3"
        assert (
            readable_citation_for("deck.pptx", "x", page=3, page_end=4) == "deck.pptx, slides 3-4"
        )

    def test_a_pdf_is_still_cited_by_page(self):
        assert citation_for("report.pdf", "x", page=3) == "report.pdf#page=3"


class TestTheReadableForm:
    def test_a_heading_where_there_are_no_pages(self):
        assert (
            readable_citation_for("guide.md", "x", heading="Offline [beta]")
            == "guide.md, Offline beta"
        )

    def test_the_bare_name_where_there_is_nothing_else(self):
        assert readable_citation_for("notes.txt", "x") == "notes.txt"

    def test_a_figure_names_its_caption(self):
        assert (
            readable_citation_for(
                "r.pdf", "x", page=41, page_label="39", figure="Figure 3: Revenue (FY2025)"
            )
            == "r.pdf, p. 39 (Figure 3: Revenue FY2025)"
        )

    def test_what_was_stamped_wins_and_a_result_reads_it(self):
        from vectrixdb.easy import Result

        stamped = {
            "_vx_readable_citation": "report.pdf, p. 39",
            "page": 41,
            "filename": "report.pdf",
        }
        assert readable_citation_of(stamped, "x") == "report.pdf, p. 39"
        assert (
            Result(
                id="a",
                text="t",
                score=1.0,
                metadata={"page": 41, "page_label": "39", "filename": "report.pdf"},
            ).readable_citation
            == "report.pdf, p. 39"
        )


# ================================================================ the main app's path ===


class TestThroughTheExtractionApp:
    def test_the_reply_carries_them_and_is_read_back(self):
        from vectrixdb.api.extraction import _as_json
        from vectrixdb.extract import coerce

        sent = _as_json(paged(labels=LABELS))
        assert sent["page_labels"] == {"1": "C1", "2": "C2", "3": "1", "4": "2", "5": "3"}
        assert coerce(json.loads(json.dumps(sent)), "report.pdf").page_labels == LABELS
        assert "page_labels" not in _as_json(paged()), "a reply for anything else is as it was"

    def test_they_are_read_from_the_original_when_the_reply_has_none(self):
        doc = load_bytes(labelled_pdf(), "report.pdf", extractors={".pdf": reply}, source=BLOB)
        assert doc.page_labels == LABELS

    def test_the_file_keeps_its_own_address_whatever_the_service_called_it(self):
        def elsewhere(data, name):
            answer = reply(data, name)
            answer["metadata"].update(source="report.pages-1.pdf", filename="report.pages-1.pdf")
            return answer

        doc = load_bytes(labelled_pdf(), "report.pdf", extractors={".pdf": elsewhere}, source=BLOB)
        assert (doc.metadata["source"], doc.metadata["filename"]) == (BLOB, "report.pdf")
        assert doc.metadata["kind"] == "pdf"

    def test_a_long_pdf_read_in_pieces_is_the_whole_file(self):
        from vectrixdb.extract import batched

        reader = batched(reply, pages=2, at_once=1)
        doc = load_bytes(labelled_pdf(5), "report.pdf", extractors={".pdf": reader}, source=BLOB)
        assert doc.metadata["pieces"] == 3
        assert (doc.metadata["source"], doc.metadata["filename"]) == (BLOB, "report.pdf")
        assert doc.page_labels == LABELS, (
            "read from the original: the pieces have no table of their own"
        )
        joined = reader(labelled_pdf(5), "report.pdf", source=BLOB)
        assert joined.metadata["source"] == BLOB, (
            "the join names the whole file, not its first piece"
        )

    def test_the_kept_markdown_and_the_chunks_carry_them(self, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.documents import DocumentStore, LocalFiles
        from vectrixdb.worker import IngestEvent, IngestWorker, LocalFetcher

        original = tmp_path / "raw" / "report.pdf"
        original.parent.mkdir()
        original.write_bytes(labelled_pdf())
        db = Vectrix(
            "financial",
            path=str(tmp_path / "db"),
            embed_fn=embed,
            dimension=8,
            extractors={".pdf": reply},
            keep_source=DocumentStore(LocalFiles(tmp_path / "markdown"), keep_deleted=False),
            keep_chunks=tmp_path / "chunks",
            markdown_first=True,
        )
        worker = IngestWorker(
            db,
            LocalFetcher(),
            doc_id_of=lambda uri: "td/report.pdf",
            chunk="recursive",
            chunk_size=40,
            overlap=0,
        )
        try:
            assert worker.handle(IngestEvent("created", original.as_uri())).action == "created"
            kept = (tmp_path / "markdown" / "td" / "report.pdf.md").read_text(encoding="utf-8")
            assert 'page_labels: {"1": "C1", "2": "C2", "3": "1", "4": "2", "5": "3"}' in kept
            assert db.documents.entry("td/report.pdf")["source"] == original.as_uri()
            lines = db.kept_chunks.get("td/report.pdf")
            assert [c["metadata"]["_vx_readable_citation"] for c in lines][2:] == [
                "report.pdf, p. 1",
                "report.pdf, p. 2",
                "report.pdf, p. 3",
            ]
            db.rechunk("td/report.pdf", chunk_size=1000)
            assert (
                db.kept_chunks.get("td/report.pdf")[0]["metadata"]["_vx_readable_citation"]
                == "report.pdf, pp. C1-3"
            )
        finally:
            db.close()
