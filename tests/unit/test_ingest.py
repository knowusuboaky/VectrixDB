"""Ingestion: chunkers, loaders, near-duplicates, parent-child retrieval,
the embedding cache and the model-versioning check.

Chunkers are tested as properties: every chunk is a substring at its
offsets, chunks cover the text in order, nothing exceeds the size, and the
sentence strategy never cuts a sentence. Loaders are tested on files written
by the test. The Vectrix-level features are tested end to end with the
bundled model, kept small.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vectrixdb import Vectrix
from vectrixdb.ingest import (
    Chunk,
    EmbeddingCache,
    LoadedDocument,
    NearDuplicateIndex,
    ParentStore,
    chunk,
    load,
    split_sentences,
)

PROSE = (
    "Basalt forms when lava cools quickly at the surface. It is dark and fine-grained. "
    "Granite cools slowly underground, so its crystals are large! Both are igneous rocks.\n\n"
    "Sourdough starter is flour and water kept alive by wild yeast. Bakers feed it daily. "
    "The bread it leavens has a sour taste? Yes, from the lactic acid.\n\n"
    "The Nobel Prize was awarded for research on radium. Marie Curie won twice. "
    "Her notebooks are still radioactive today."
)

MARKDOWN = """# Geology

Basalt forms when lava cools quickly at the surface. It is dark and fine-grained.

## Granite

Granite cools slowly underground, so its crystals are large. Both are igneous rocks.

# Baking

Sourdough starter is flour and water kept alive by wild yeast. Bakers feed it daily.
"""


# =============================================================================
# Chunkers
# =============================================================================


def _check_invariants(text: str, chunks: list, size: int) -> None:
    last_start = -1
    for c in chunks:
        assert isinstance(c, Chunk)
        assert text[c.start : c.end] == c.text, "offsets must point at the chunk text"
        assert c.text.strip() == c.text, "chunks are stripped"
        assert len(c.text) <= size, f"chunk of {len(c.text)} exceeds size {size}"
        assert c.start > last_start, "chunks come in document order"
        last_start = c.start
    assert [c.index for c in chunks] == list(range(len(chunks)))


class TestStrategies:
    @pytest.mark.parametrize("strategy", ["recursive", "sentence", "markdown"])
    def test_invariants_on_prose(self, strategy):
        text = MARKDOWN if strategy == "markdown" else PROSE
        chunks = chunk(text, strategy, size=120, overlap=30)
        assert chunks
        _check_invariants(text, chunks, 120)

    def test_every_word_lands_in_some_chunk(self):
        chunks = chunk(PROSE, "sentence", size=150, overlap=40)
        covered = " ".join(c.text for c in chunks)
        for word in PROSE.split():
            assert word in covered

    def test_sentence_strategy_never_cuts_a_sentence(self):
        for c in chunk(PROSE, "sentence", size=150, overlap=0):
            assert c.text[-1] in ".!?", c.text

    def test_sentence_overlap_repeats_the_last_sentence(self):
        chunks = chunk(PROSE, "sentence", size=150, overlap=60)
        assert len(chunks) >= 2
        first_sentences = [split_sentences(c.text)[0] for c in chunks[1:]]
        assert all(first_sentences), "overlap carries a whole sentence"
        # The first sentence of chunk n+1 appears in chunk n.
        for prev, cur in zip(chunks, chunks[1:]):
            s, e = split_sentences(cur.text)[0]
            assert cur.text[s:e] in prev.text

    def test_markdown_carries_headings_and_drops_heading_lines(self):
        chunks = chunk(MARKDOWN, "markdown", size=400, overlap=0)
        headings = [c.heading for c in chunks]
        assert headings == ["Geology", "Granite", "Baking"]
        assert not any(c.text.startswith("#") for c in chunks)

    def test_semantic_splits_where_topics_change(self):
        # A stand-in embedder: sentences about rocks point one way, bread
        # another, radium a third. The topic boundaries are then obvious.
        def embed(sentences):
            out = []
            for s in sentences:
                low = s.lower()
                if any(w in low for w in ("basalt", "granite", "igneous", "crystals", "dark")):
                    out.append([1.0, 0.0, 0.0])
                elif any(w in low for w in ("sourdough", "yeast", "bread", "bakers", "lactic")):
                    out.append([0.0, 1.0, 0.0])
                else:
                    out.append([0.0, 0.0, 1.0])
            return np.asarray(out, dtype=np.float32)

        # Ten boundaries, two of them topic changes: ask for the sharpest fifth.
        chunks = chunk(PROSE, "semantic", size=2000, embed=embed, threshold=0.2)
        assert len(chunks) == 3
        assert (
            "Basalt" in chunks[0].text
            and "Sourdough" in chunks[1].text
            and "Nobel" in chunks[2].text
        )

    def test_semantic_needs_an_embedder(self):
        with pytest.raises(ValueError, match="embed"):
            chunk(PROSE, "semantic")

    def test_bad_arguments(self):
        with pytest.raises(ValueError):
            chunk(PROSE, "nope")
        with pytest.raises(ValueError):
            chunk(PROSE, size=0)
        with pytest.raises(ValueError):
            chunk(PROSE, size=10, overlap=10)
        assert chunk("   \n ", "recursive") == []

    @settings(max_examples=60, deadline=None)
    # Two counterexamples this property found and the chunker has since been
    # fixed for: repeated digits made chunk_text exceed its own size limit,
    # and made two chunks resolve to the same offset. Hypothesis will not
    # rediscover them reliably, so they are pinned here.
    @example(text="000" + chr(10) + "00000", size=8, strategy="recursive")
    @example(text="0000" + chr(10) + "0000", size=8, strategy="recursive")
    # A third: a Markdown document of headings alone came back with no chunks at all.
    @example(text="# 0", size=8, strategy="markdown")
    @given(
        text=st.text(
            alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=600
        ),
        size=st.integers(min_value=8, max_value=200),
        strategy=st.sampled_from(["recursive", "sentence", "markdown"]),
    )
    def test_invariants_hold_for_any_text(self, text, size, strategy):
        chunks = chunk(text, strategy, size=size, overlap=min(3, size - 1))
        _check_invariants(text, chunks, size)
        if text.strip():
            assert chunks or not any(ch.isalnum() for ch in text)


class TestPagesAndHeadings:
    def test_chunks_know_their_page(self):
        doc = LoadedDocument(
            text=PROSE, pages=[(0, 1), (PROSE.index("Sourdough"), 2), (PROSE.index("The Nobel"), 3)]
        )
        chunks = chunk(doc, "sentence", size=150, overlap=0)
        pages = [c.page for c in chunks]
        assert pages == sorted(pages) and pages[0] == 1 and 2 in pages
        for c in chunks:
            assert c.page == doc.page_at(c.start)
            assert c.metadata()["page"] == c.page
        # A page boundary inside a chunk: the chunk belongs to the page it starts on.
        nobel = next(c for c in chunks if "Nobel" in c.text)
        assert nobel.page in (2, 3)

    def test_page_markers_round_trip_into_the_pdf_tree_builder(self):
        from vectrixdb import build_tree_from_pdf

        doc = LoadedDocument(text="alpha\n\nbeta", pages=[(0, 1), (7, 2)])
        nodes = build_tree_from_pdf(doc.with_page_markers(), doc_id="d")
        assert [n.page_num for n in nodes] == [1, 2]
        assert nodes[1].text == "beta"


# =============================================================================
# Loaders
# =============================================================================


class TestLoaders:
    def test_markdown_and_text(self, tmp_path):
        md = tmp_path / "guide.md"
        md.write_text(MARKDOWN, encoding="utf-8")
        doc = load(md)
        assert doc.metadata["kind"] == "markdown" and doc.metadata["filename"] == "guide.md"
        assert [h[1] for h in doc.headings] == ["Geology", "Granite", "Baking"]
        txt = tmp_path / "notes.txt"
        txt.write_text("plain words", encoding="utf-8")
        assert load(txt).text == "plain words"

    def test_html_keeps_visible_text_and_headings(self, tmp_path):
        page = tmp_path / "page.html"
        page.write_text(
            "<html><head><title>T</title><style>p{}</style></head><body>"
            "<h1>Geology</h1><p>Basalt forms   when lava cools.</p><script>x()</script>"
            "<h2>Granite</h2><div>Granite cools slowly.</div></body></html>",
            encoding="utf-8",
        )
        doc = load(page)
        assert "x()" not in doc.text and "p{}" not in doc.text and "T" != doc.text[:1]
        assert "Basalt forms when lava cools." in doc.text
        assert [h[1] for h in doc.headings] == ["Geology", "Granite"]
        chunks = chunk(doc, "markdown", size=500)
        assert [c.heading for c in chunks] == ["Geology", "Granite"]

    def test_pdf_pages(self, tmp_path):
        fitz = pytest.importorskip("fitz")
        pytest.importorskip("pypdf")
        pdf = tmp_path / "two.pdf"
        with fitz.open() as out:
            for words in ("First page about basalt.", "Second page about sourdough."):
                page = out.new_page()
                page.insert_text((72, 72), words)
            out.save(str(pdf))
        doc = load(pdf)
        assert doc.metadata["pages"] == 2
        assert doc.page_at(doc.text.index("sourdough")) == 2
        assert doc.page_at(0) == 1

    def test_docx_headings(self, tmp_path):
        docx = pytest.importorskip("docx")
        path = tmp_path / "memo.docx"
        d = docx.Document()
        d.add_heading("Geology", level=1)
        d.add_paragraph("Basalt forms when lava cools.")
        d.add_heading("Baking", level=2)
        d.add_paragraph("Sourdough needs a starter.")
        d.save(str(path))
        doc = load(path)
        assert [(h[1], h[2]) for h in doc.headings] == [("Geology", 1), ("Baking", 2)]
        assert doc.heading_at(doc.text.index("Sourdough")) == "Baking"

    def test_missing_parser_is_a_dependency_error(self, tmp_path, monkeypatch):
        import builtins

        from vectrixdb.exceptions import DependencyError

        real_import = builtins.__import__

        def no_pdf(name, *args, **kwargs):
            if name in ("pypdf", "fitz"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pdf)
        (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4")
        with pytest.raises(DependencyError, match="pip install"):
            load(tmp_path / "x.pdf")


# =============================================================================
# Near-duplicates
# =============================================================================


class TestNearDuplicateIndex:
    def test_estimates_jaccard(self):
        index = NearDuplicateIndex(permutations=128, bands=16)
        a = "the quick brown fox jumps over the lazy dog by the river bank at noon"
        b = a + " today"
        c = "vector databases index embeddings for similarity search at scale"
        sa, sb, sc = index.signature(a), index.signature(b), index.signature(c)
        assert index.similarity(sa, sb) > 0.7
        assert index.similarity(sa, sc) < 0.2

    def test_query_finds_near_duplicates_and_not_others(self, tmp_path):
        index = NearDuplicateIndex(tmp_path / "mh.json")
        index.add("a", "the quick brown fox jumps over the lazy dog by the river bank at noon")
        assert (
            index.query(
                "the quick brown fox jumps over the lazy dog by the river bank at noon!", 0.8
            )
            is not None
        )
        assert index.query("vector databases index embeddings for similarity search", 0.8) is None
        index.save()
        reopened = NearDuplicateIndex(tmp_path / "mh.json")
        assert len(reopened) == 1
        assert (
            reopened.query(
                "the quick brown fox jumps over the lazy dog by the river bank at noon", 0.9
            )[0]
            == "a"
        )
        reopened.remove("a")
        assert (
            reopened.query(
                "the quick brown fox jumps over the lazy dog by the river bank at noon", 0.5
            )
            is None
        )


# =============================================================================
# Embedding cache and parents, unit level
# =============================================================================


class TestEmbeddingCache:
    def test_hits_after_put(self, tmp_path):
        cache = EmbeddingCache(tmp_path / "c.db")
        assert cache.get_many("m", ["a", "b"]) == [None, None]
        cache.put_many("m", ["a"], np.array([[1.0, 2.0]], dtype=np.float32))
        got = cache.get_many("m", ["a", "b"])
        assert got[1] is None and np.allclose(got[0], [1.0, 2.0])
        assert cache.get_many("other-model", ["a"]) == [None]
        assert cache.hits == 1 and cache.misses == 4
        assert len(cache) == 1
        cache.close()

    def test_its_folder_is_made_when_nothing_else_made_it(self, tmp_path):
        """A collection kept in Azure Search, Cosmos and Blob wrote nothing to /tmp before its cache opened: every file failed."""
        cache = EmbeddingCache(tmp_path / "vectrixdb" / "financial" / "_embed_cache.db")
        cache.put_many("m", ["a"], np.array([[1.0, 2.0]], dtype=np.float32))
        assert len(cache) == 1 and (tmp_path / "vectrixdb" / "financial" / "_embed_cache.db").exists()
        cache.close()


class TestParentStore:
    def test_disk_and_memory(self, tmp_path):
        for store in (ParentStore(tmp_path / "p.db"), ParentStore()):
            store.put("d:parent:0", "section text", {"heading": "H"})
            assert store.get("d:parent:0") == ("section text", {"heading": "H"})
            assert store.get("missing") is None
            assert store.delete_document("d") == 1
            assert store.get("d:parent:0") is None
            store.close()


# =============================================================================
# End to end through Vectrix
# =============================================================================


class TestAddDocument:
    def test_chunks_carry_document_metadata(self, tmp_path):
        db = Vectrix("ingest", path=str(tmp_path))
        n = db.add_document(MARKDOWN, chunk="markdown", chunk_size=400, metadata={"source": "test"})
        assert n == 3 and db.count() == 3
        hit = db.search("igneous rocks with large crystals", limit=1).top
        assert hit.metadata["heading"] == "Granite"
        assert hit.metadata["source"] == "test"
        assert hit.metadata["_vx_doc"] and hit.metadata["_vx_chunk"] == 1
        db.close()

    def test_a_file_path_is_loaded(self, tmp_path):
        md = tmp_path / "guide.md"
        md.write_text(MARKDOWN, encoding="utf-8")
        db = Vectrix("files", path=str(tmp_path / "db"))
        assert db.add_document(md, chunk="markdown", chunk_size=400) == 3
        assert db.search("sourdough", limit=1).top.metadata["filename"] == "guide.md"
        db.close()

    def test_parent_child_returns_the_section(self, tmp_path):
        db = Vectrix("pc", path=str(tmp_path))
        db.add_document(
            MARKDOWN, chunk="sentence", chunk_size=90, overlap=0, parent_size=400, doc_id="g"
        )
        assert db.count() > 3, "small chunks"
        plain = db.search("crystals are large", limit=3)
        assert all(r.metadata.get("_vx_parent") for r in plain)
        parents = db.search("crystals are large", limit=3, parents=True)
        top = parents.top
        assert top.id.startswith("g:parent:")
        assert "Granite cools slowly" in top.text and "Both are igneous rocks" in top.text
        assert top.metadata["_vx_child"] in {r.id for r in plain}
        assert len({r.id for r in parents}) == len(parents), "one result per section"
        # Survives a reopen: parents live beside the collection.
        db.close()
        again = Vectrix("pc", path=str(tmp_path))
        assert again.search("crystals are large", limit=1, parents=True).top.id.startswith(
            "g:parent:"
        )
        assert again.delete_document("g") == len(plain) or again.count() == 0
        again.close()

    def test_delete_document_removes_chunks_and_parents(self, tmp_path):
        db = Vectrix("del", path=str(tmp_path))
        db.add_document(PROSE, chunk="sentence", chunk_size=120, parent_size=300, doc_id="p")
        db.add("unrelated text that stays")
        removed = db.delete_document("p")
        assert removed >= 3 and db.count() == 1
        assert db._parents.get("p:parent:0") is None
        db.close()


class TestDedupe:
    def test_near_duplicates_are_skipped_and_reported(self, tmp_path):
        db = Vectrix("dd", path=str(tmp_path))
        base = "The quick brown fox jumps over the lazy dog by the river bank at noon every day."
        db.add([base, "Completely different words about vector databases and their indexes."])
        db.add(
            [base + " Really.", "Another new sentence about gardening tomatoes in June."],
            dedupe=0.6,
        )
        report = db.last_add_report
        assert len(report.added) == 1 and len(report.skipped) == 1
        skipped_id, duplicate_of, score = report.skipped[0]
        assert duplicate_of == db._generate_id(base) and score >= 0.6
        assert db.count() == 3
        # The index persists: a new handle still knows the texts.
        db.close()
        again = Vectrix("dd", path=str(tmp_path))
        again.add([base], dedupe=0.9)
        assert again.last_add_report.skipped and again.count() == 3
        again.close()

    def test_dedupe_within_one_call(self, tmp_path):
        db = Vectrix("dd2", path=str(tmp_path))
        text = "Sourdough starter is flour and water kept alive by wild yeast and bacteria."
        db.add(
            [text, text + " Feed it.", "Basalt forms when lava cools quickly at the surface."],
            dedupe=0.6,
        )
        assert db.count() == 2 and len(db.last_add_report.skipped) == 1
        db.close()


class TestEmbeddingCacheInVectrix:
    def test_second_add_of_the_same_text_does_not_embed(self, tmp_path, monkeypatch):
        db = Vectrix("cache", path=str(tmp_path))
        db.add(["alpha beta gamma", "delta epsilon"])
        assert db._embed_cache is not None and len(db._embed_cache) == 2
        calls = []
        original = db._embed_batched

        def counting(texts, progress):
            calls.append(list(texts))
            return original(texts, progress)

        monkeypatch.setattr(db, "_embed_batched", counting)
        db.add(["alpha beta gamma", "zeta"], ids=["x", "y"])
        assert calls == [["zeta"]], "only the unseen text reached the model"
        assert db.count() == 4
        db.close()

    def test_no_cache_when_off_or_readonly(self, tmp_path):
        db = Vectrix("off", path=str(tmp_path), embedding_cache=False)
        assert db._embed_cache is None
        db.close()


class TestModelVersioning:
    def test_model_is_recorded_and_mismatch_warns(self, tmp_path):
        db = Vectrix("mv", path=str(tmp_path))
        db.add(["one document"])
        recorded = db.embedding_model
        assert recorded == db.model_name
        db.close()

        # Pretend the collection was built with something else.
        db = Vectrix("mv", path=str(tmp_path))
        db._collection.set_meta("embedding_model", "someone/other-model")
        db.close()

        from vectrixdb import ModelMismatchWarning

        with pytest.warns(ModelMismatchWarning, match="reembed"):
            db = Vectrix("mv", path=str(tmp_path))
        assert db.reembed() == 1
        assert db.embedding_model == db.model_name
        db.close()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Vectrix("mv", path=str(tmp_path)).close()

    def test_reembed_keeps_search_working(self, tmp_path):
        db = Vectrix("re", path=str(tmp_path))
        db.add(
            ["Basalt forms when lava cools quickly.", "Sourdough is leavened by wild yeast."],
            metadata=[{"k": 1}, {"k": 2}],
        )
        before = db.search("bread yeast", limit=1).top
        assert db.reembed() == 2
        after = db.search("bread yeast", limit=1).top
        assert after.id == before.id and after.metadata["k"] == 2
        assert db.count() == 2
        db.close()
