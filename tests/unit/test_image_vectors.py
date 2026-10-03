"""A figure found by what it looks like: its picture embedded into a vector index of its own.

The model is a stand-in with two sides that share a space, as CLIP's do: a
picture of a bar chart and the words "bar chart" land on the same axis. No
model is loaded and nothing is fetched.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np
import pytest

from vectrixdb.exceptions import ConfigurationError, ExtractionError

REPORT = """# Q3 report

## Regional performance

Revenue grew in every region, as shown in Figure 1.

![Figure 1: Revenue by region](charts/bars.png)

## Organisation

The reporting lines are in Figure 2.

![Figure 2: Reporting lines](charts/tree.png)

## Notes

![Figure 3: A scan nobody kept](charts/missing.png)

Nothing else changed in the period.
"""


def png(width, height, marker):
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    head = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return head + marker * 1500


BARS, TREE = png(640, 480, b"bars"), png(640, 481, b"tree")
LOOKS = {"bar": 0, "bars": 0, "chart": 0, "columns": 0, "tree": 1, "boxes": 1, "hierarchy": 1}


class TwoSided:
    """Pictures and words in one three-axis space: bar charts, trees, anything else."""

    dimension = 3

    def __init__(self):
        self.images, self.texts = [], []

    def embed_images(self, images):
        self.images += list(images)
        return np.asarray(
            [
                [1, 0, 0] if b"bars" in data else [0, 1, 0] if b"tree" in data else [0, 0, 1]
                for data in images
            ],
            dtype=np.float32,
        )

    def embed_texts(self, texts):
        self.texts += list(texts)
        out = np.zeros((len(texts), 3), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in text.lower().split():
                if word in LOOKS:
                    out[i, LOOKS[word]] += 1.0
            if not out[i].any():
                out[i, 2] = 1.0
        return out


def words(texts):
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, zlib.crc32(word.encode()) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


@pytest.fixture
def inbox(tmp_path):
    folder = tmp_path / "inbox"
    (folder / "charts").mkdir(parents=True)
    (folder / "charts" / "bars.png").write_bytes(BARS)
    (folder / "charts" / "tree.png").write_bytes(TREE)
    (folder / "q3.md").write_text(REPORT, encoding="utf-8")
    return folder


def open_db(tmp_path, model, **options):
    from vectrixdb import Vectrix

    return Vectrix(
        "reports",
        path=str(tmp_path / "db"),
        embed_fn=words,
        dimension=8,
        embedding_cache=False,
        image_embedder=model,
        **options,
    )


class TestAFigureIsFoundByItsLooks:
    def test_the_picture_is_what_is_embedded_and_the_question_goes_through_the_text_side(
        self, tmp_path, inbox
    ):
        model = TwoSided()
        db = open_db(tmp_path, model)
        try:
            db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            assert sorted(model.images) == sorted([BARS, TREE]), (
                "each picture once, and the one nobody kept not at all"
            )
            assert db.vector_names() == ["own", "image"]
            found = db.search("a chart with columns", limit=2, vectors="image")
            assert [h.metadata["figure_src"] for h in found] == [
                "charts/bars.png",
                "charts/tree.png",
            ]
            assert "a chart with columns" in model.texts
            assert [
                h.metadata["figure_src"]
                for h in db.search("hierarchy of boxes", limit=1, vectors="image")
            ] == ["charts/tree.png"]
        finally:
            db.close()

    def test_what_comes_back_is_the_figure_chunk_itself(self, tmp_path, inbox):
        db = open_db(tmp_path, TwoSided(), keep_source=tmp_path / "kept")
        try:
            db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            (hit,) = db.search("bar chart", limit=1, vectors="image")
            assert hit.metadata["figure_id"] == "q3.md#fig1" and hit.text.startswith(
                "[Figure: Figure 1: Revenue by region"
            )
            assert db.get([hit.id])[0].text == hit.text, "the same id the collection holds"
            assert db.figure_bytes(hit) == BARS
        finally:
            db.close()

    def test_a_search_that_does_not_ask_for_pictures_is_the_search_it_always_was(
        self, tmp_path, inbox
    ):
        model = TwoSided()
        db = open_db(tmp_path, model)
        plain = open_db(tmp_path / "plain", None)
        try:
            for handle in (db, plain):
                handle.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            asked = len(model.texts)
            ours, theirs = (
                db.search("reporting lines", limit=5),
                plain.search("reporting lines", limit=5),
            )
            assert [h.id for h in ours] == [h.id for h in theirs] and [h.score for h in ours] == [
                h.score for h in theirs
            ]
            assert ours.items[0].relevance is not None, (
                "the verdict is kept, which a fused list does not have"
            )
            assert len(model.texts) == asked, "and the picture model was not asked"
        finally:
            db.close()
            plain.close()

    def test_all_fuses_the_words_and_the_pictures(self, tmp_path, inbox):
        db = open_db(tmp_path, TwoSided())
        try:
            db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            found = db.search("hierarchy of boxes", limit=3, vectors="all", explain=True)
            tree = next(h for h in found if h.metadata.get("figure_src") == "charts/tree.png")
            assert tree.explain["vector_ranks"]["image"] == 1, (
                "no word of the question is in its caption; its picture found it"
            )
        finally:
            db.close()


class TestTheImageIndexFollowsTheCollection:
    def test_a_document_replaced_or_deleted_takes_its_pictures_with_it(self, tmp_path, inbox):
        db = open_db(tmp_path, TwoSided())
        try:
            db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            (inbox / "q3.md").write_text(
                REPORT.replace("![Figure 2: Reporting lines](charts/tree.png)", ""),
                encoding="utf-8",
            )
            db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            assert [
                h.metadata["figure_src"] for h in db.search("tree", limit=5, vectors="image")
            ] == ["charts/bars.png"]
            db.delete_document("q3.md")
            assert db.search("bar chart", limit=5, vectors="image").items == []
        finally:
            db.close()

    def test_cutting_the_documents_again_keeps_the_pictures(self, tmp_path, inbox):
        """rechunk() reads the kept Markdown and the kept images, so nothing is lost and no original is needed."""
        model = TwoSided()
        db = open_db(tmp_path, model, keep_source=tmp_path / "kept")
        try:
            db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
            for picture in (inbox / "charts").iterdir():
                picture.unlink()
            db.rechunk(chunk="recursive", chunk_size=300, overlap=50)
            found = db.search("bar chart", limit=5, vectors="image")
            assert [h.metadata["figure_src"] for h in found] == [
                "charts/bars.png",
                "charts/tree.png",
            ]
            assert all(
                db.get([h.id])[0].metadata.get("figure_src") == h.metadata["figure_src"]
                for h in found
            )
        finally:
            db.close()

    def test_plain_text_writes_never_reach_it(self, tmp_path):
        model = TwoSided()
        db = open_db(tmp_path, model)
        try:
            db.add(["a bar chart described in words"], ids=["t1"])
            assert (
                model.images == [] and db.search("bar chart", limit=5, vectors="image").items == []
            )
        finally:
            db.close()

    def test_it_is_reopened_with_the_model_it_was_built_with(self, tmp_path, inbox):
        db = open_db(tmp_path, TwoSided())
        db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
        db.close()
        from vectrixdb import Vectrix

        with pytest.raises(ConfigurationError, match="named dense vectors"):
            Vectrix("reports", path=str(tmp_path / "db"), embed_fn=words, dimension=8)
        again = open_db(tmp_path, TwoSided())
        try:
            assert [
                h.metadata["figure_src"]
                for h in again.search("bar chart", limit=1, vectors="image")
            ] == ["charts/bars.png"]
        finally:
            again.close()


class TestWhatIsRefused:
    def test_a_model_without_both_sides_or_a_dimension(self, tmp_path):
        class NoTextSide:
            dimension = 3

            def embed_images(self, images):
                return []

        class NoDimension(TwoSided):
            dimension = None

        with pytest.raises(TypeError, match="no embed_texts"):
            open_db(tmp_path, NoTextSide())
        with pytest.raises(TypeError, match="has a dimension"):
            open_db(tmp_path, NoDimension())

    def test_the_wrong_number_or_width_of_vectors_says_so(self, tmp_path, inbox):
        class Short(TwoSided):
            def embed_images(self, images):
                return np.zeros((len(images), 2), dtype=np.float32)

        db = open_db(tmp_path, Short())
        try:
            with pytest.raises(ConfigurationError, match=r"returned \(2, 2\) for 2 images"):
                db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
        finally:
            db.close()

    def test_a_model_that_fails_is_an_extraction_error_naming_the_document(self, tmp_path, inbox):
        class Broken(TwoSided):
            def embed_images(self, images):
                raise RuntimeError("out of memory")

        db = open_db(tmp_path, Broken())
        try:
            with pytest.raises(
                ExtractionError, match="embedding the figures of q3.md failed: out of memory"
            ):
                db.add_document(inbox / "q3.md", doc_id="q3.md", chunk="markdown")
        finally:
            db.close()

    def test_image_cannot_name_a_dense_model_too(self, tmp_path):
        with pytest.raises(ConfigurationError, match="no dense model can be called that"):
            open_db(tmp_path, TwoSided(), dense_model=[None, ("image", words, 8)])

    def test_not_under_an_entitlement_policy(self, tmp_path):
        from vectrixdb.policy import Overlap, Policy

        with pytest.raises(ConfigurationError, match="entitlement policy"):
            open_db(
                tmp_path, TwoSided(), policy=Policy([Overlap("client_id", "clients", scope=True)])
            )
