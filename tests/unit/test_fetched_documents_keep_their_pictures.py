"""A document fetched from a bucket comes back with the same figures as one read from a file.

It did not. ``load_bytes`` read the bytes through ``load()`` without ever
asking for the pictures, so a PDF arriving from a blob or an S3 object had no
figures **at all**, not merely figures without descriptions. A collection
opened with ``describe_figures`` described nothing, and one opened with
``image_embedder`` embedded nothing, and neither said so, while the same file
read from disk was fine. Every deployment that ingests on an event lost them.

The collection decides now, not the caller: :meth:`Vectrix.wants_images` is
what ``add_document`` and the worker both ask.

One limit is real and is held to below. A PDF carries its pictures inside its
bytes, so fetching it is enough. A Markdown file's pictures are separate files
beside it, and bytes fetched from a bucket have no neighbours, so its figure
lines survive and its pictures do not.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

from vectrixdb.ingest import load, load_bytes

pytest.importorskip("pypdf", reason="the documents extra is not installed")
Image = pytest.importorskip("PIL.Image", reason="pypdf needs Pillow to hand back a page's pictures")
ImageDraw = pytest.importorskip("PIL.ImageDraw")


def a_pdf_with_a_picture_in_it() -> bytes:
    """One page that is a picture, which is what a scanned chart is.

    Bars and an axis, not a block of colour: a picture with nothing in it
    reads as blank and is dropped before anybody is asked to describe it,
    which is correct and would make this test prove the opposite.
    """
    picture = Image.new("RGB", (240, 180), "white")
    pen = ImageDraw.Draw(picture)
    for n, height in enumerate((40, 90, 65, 130, 100)):
        pen.rectangle([20 + n * 44, 170 - height, 56 + n * 44, 170], fill=(40, 80, 160))
    pen.line([10, 170, 235, 170], fill="black", width=2)
    buffer = io.BytesIO()
    picture.save(buffer, "PDF")
    return buffer.getvalue()


REPORT = """# Q3

Revenue grew in every region, as shown in Figure 1.

![Figure 1: Revenue by region](charts/bars.png)
"""


class TestBytesReadTheWayAFileIs:
    def test_a_fetched_pdf_now_has_the_figures_it_always_had_on_disk(self, tmp_path):
        data = a_pdf_with_a_picture_in_it()
        path = tmp_path / "chart.pdf"
        path.write_bytes(data)
        from_bytes, from_file = load_bytes(data, "chart.pdf", images=True), load(path, images=True)
        assert len(from_bytes.figures) == 1, "this was nought before the fix, from a bucket"
        assert [f[1].get("src") for f in from_bytes.figures] == [
            f[1].get("src") for f in from_file.figures
        ]
        assert list(from_bytes.images) == list(from_file.images) and all(from_bytes.images.values())

    def test_left_off_it_costs_nothing_and_keeps_nothing(self, tmp_path):
        plain = load_bytes(a_pdf_with_a_picture_in_it(), "chart.pdf")
        assert not plain.images and not plain.figures, (
            "off by default: a decode of every picture is not free"
        )

    def test_a_markdown_files_pictures_are_its_neighbours_and_bytes_have_none(self, tmp_path):
        """Its figure lines survive; the pictures cannot, and that is not this fix's to give."""
        (tmp_path / "charts").mkdir()
        picture = io.BytesIO()
        Image.new("RGB", (60, 60), (10, 90, 200)).save(picture, "PNG")
        (tmp_path / "charts" / "bars.png").write_bytes(picture.getvalue())
        path = tmp_path / "q3.md"
        path.write_text(REPORT, encoding="utf-8")
        from_bytes = load_bytes(path.read_bytes(), "q3.md", images=True)
        assert [f[1].get("src") for f in from_bytes.figures] == ["charts/bars.png"], (
            "the figure line is still a figure"
        )
        assert not from_bytes.images, "and the picture is a file this fetch never saw"
        assert load(path, images=True).images, "read from its folder, it is there"


def a_deck_with_a_chart_on_every_page(pages: int = 5) -> bytes:
    """Pages of text with the same chart on each, which is what a deck saved as a PDF is."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    chart = pypdf.PdfReader(io.BytesIO(a_pdf_with_a_picture_in_it())).pages[0]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    writer = pypdf.PdfWriter()
    for p in range(1, pages + 1):
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(
            f"BT /F1 12 Tf 72 720 Td (Slide {p} says something of its own about the quarter.) Tj ET".encode(
                "latin-1"
            )
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        page.replace_contents(stream)
        page.merge_page(chart)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestAPictureOnEveryPage:
    def test_is_not_a_running_line(self):
        """Found while fixing running lines: with a picture on most pages, every page's figure line was taken for a footer, and the figures went."""
        doc = load_bytes(a_deck_with_a_chart_on_every_page(), "deck.pdf", images=True)
        assert len(doc.figures) == 5 and doc.text.count("[Figure: ") == 5
        assert "running_lines" not in doc.metadata


def _embed(texts):
    return np.asarray([[1.0, 0.0, 0.0, 0.0] for _ in texts], dtype=np.float32)


class Reading:
    """A fetcher over a mapping, which is every bucket in one line."""

    def __init__(self, files):
        self.files = files

    def fetch(self, uri):
        return self.files[uri]


class TestTheWorkerAsksWhenItsCollectionWants:
    """The whole of the fix: the collection decides, and the worker asks it."""

    URI = "https://acct.blob.core.windows.net/originals/chart.pdf"

    def collection(self, tmp_path, **options):
        from vectrixdb import Vectrix

        return Vectrix(
            "inbox",
            path=str(tmp_path / "db"),
            embed_fn=_embed,
            dimension=4,
            embedding_cache=False,
            **options,
        )

    def ingest(self, db):
        from vectrixdb.worker import IngestEvent, IngestWorker

        worker = IngestWorker(db, Reading({self.URI: a_pdf_with_a_picture_in_it()}))
        return worker.handle(IngestEvent(kind="created", uri=self.URI))

    def test_a_describer_is_called_for_a_document_that_arrived_as_bytes(self, tmp_path):
        seen = []

        def describe(image, context):
            seen.append(len(image))
            return "A bar chart of revenue by region."

        db = self.collection(tmp_path, describe_figures=describe)
        try:
            assert db.wants_images() is True
            outcome = self.ingest(db)
            assert outcome.action == "created" and outcome.chunks
            assert len(seen) == 1 and seen[0] > 0, (
                "the describer was handed the picture, which never used to happen"
            )
            written = [r for _, _, r in db._collection._iter_documents_raw()]
            assert any(
                "A bar chart of revenue by region." in (r.get("text") or "") for r in written
            ) or any(r.get("figure") for r in written), "and what it wrote is in the collection"
        finally:
            db.close()

    def test_an_image_embedder_is_filled_for_one_too(self, tmp_path):
        class TwoSided:
            dimension = 3

            def __init__(self):
                self.images = []

            def embed_images(self, images):
                self.images += list(images)
                return np.asarray([[1.0, 0.0, 0.0] for _ in images], dtype=np.float32)

            def embed_texts(self, texts):
                return np.asarray([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)

        model = TwoSided()
        db = self.collection(tmp_path, image_embedder=model)
        try:
            assert db.wants_images() is True
            self.ingest(db)
            assert len(model.images) == 1, "the picture reached the image index"
            assert [
                h.metadata.get("figure_src") for h in db.search("chart", limit=3, vectors="image")
            ] == ["p1-fig1.jpg"]
        finally:
            db.close()

    def test_a_collection_that_does_nothing_with_pictures_pays_nothing(self, tmp_path):
        db = self.collection(tmp_path)
        try:
            assert db.wants_images() is False
            outcome = self.ingest(db)
            assert outcome.action == "created", "and the document is indexed exactly as before"
        finally:
            db.close()

    def test_one_answer_serves_add_document_and_the_worker(self, tmp_path):
        """Two places deciding this separately is how they came to disagree."""
        import inspect

        from vectrixdb import easy, worker

        assert "self.wants_images()" in inspect.getsource(easy.Vectrix.add_document)
        assert "wants_images" in inspect.getsource(worker.IngestWorker.handle)
