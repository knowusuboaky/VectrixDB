"""
Tests for VectrixDB Easy API (Vectrix class).

Note: Many tests are skipped because they require embedding models.
Run with: pytest -m "not requires_models" to skip model tests.
"""

import pytest

from vectrixdb import Vectrix, V, Result, Results

# Check if models are available
try:
    from vectrixdb import is_models_installed

    MODELS_AVAILABLE = is_models_installed()
except (ImportError, AttributeError):
    MODELS_AVAILABLE = False

requires_models = pytest.mark.skipif(
    not MODELS_AVAILABLE, reason="Requires embedding models to be installed"
)


@pytest.fixture(autouse=True)
def _isolated_working_directory(tmp_path, monkeypatch):
    """Every test in this file builds ``Vectrix(name)`` with no path.

    That writes to ./vectrixdb_data in the working directory, so the
    collections outlived the run and the next one opened them again: the
    first assertion of test_count is that a fresh collection is empty, and it
    found four documents from last time. These were gated behind
    ``requires_models``, which reported False on a correct install, so nobody
    saw it until that check was fixed.
    """
    monkeypatch.chdir(tmp_path)


class TestVectrixInit:
    """Test Vectrix initialization."""

    def test_create_vectrix(self):
        """Test creating a Vectrix instance."""
        db = Vectrix("test_collection")
        assert db is not None
        db.close()

    def test_vectrix_alias(self):
        """Test V is an alias for Vectrix."""
        assert V is Vectrix


class TestVectrixAdd:
    """Test Vectrix add operations."""

    @requires_models
    def test_add_texts(self, sample_texts):
        """Test adding texts."""
        db = Vectrix("test_add")
        db.add(sample_texts)

        assert db.count() == len(sample_texts)
        db.close()

    @requires_models
    def test_add_texts_with_ids(self, sample_texts):
        """Test adding texts with custom IDs."""
        db = Vectrix("test_add_ids")
        ids = ["t1", "t2", "t3", "t4"]
        db.add(sample_texts, ids=ids)

        assert db.count() == len(sample_texts)
        db.close()

    @requires_models
    def test_add_texts_with_metadata(self, sample_texts, sample_metadata):
        """Test adding texts with metadata."""
        db = Vectrix("test_add_meta")
        db.add(sample_texts, metadata=sample_metadata)

        assert db.count() == len(sample_texts)
        db.close()

    @requires_models
    def test_add_chaining(self, sample_texts):
        """Test method chaining with add."""
        db = Vectrix("test_chain").add(sample_texts[:2]).add(sample_texts[2:])

        assert db.count() == len(sample_texts)
        db.close()


class TestVectrixSearch:
    """Test Vectrix search operations."""

    @requires_models
    def test_basic_search(self, sample_texts):
        """Test basic text search."""
        db = Vectrix("test_search")
        db.add(sample_texts)

        results = db.search("programming language")

        assert results is not None
        assert isinstance(results, Results)
        assert len(results) > 0
        db.close()

    @requires_models
    def test_search_limit(self, sample_texts):
        """Test search with limit."""
        db = Vectrix("test_search_limit")
        db.add(sample_texts)

        results = db.search("technology", limit=2)

        assert len(results) <= 2
        db.close()

    @requires_models
    def test_search_returns_results(self, sample_texts):
        """Test search returns Results object."""
        db = Vectrix("test_results")
        db.add(sample_texts)

        results = db.search("machine learning")

        assert isinstance(results, Results)
        assert hasattr(results, "top")
        assert hasattr(results, "texts")
        assert hasattr(results, "scores")
        db.close()

    @requires_models
    def test_result_properties(self, sample_texts):
        """Test Result object properties."""
        db = Vectrix("test_result_props")
        db.add(sample_texts)

        results = db.search("programming")

        if len(results) > 0:
            top_result = results.top
            assert isinstance(top_result, Result)
            assert top_result.text is not None
            assert top_result.score is not None
        db.close()


class TestVectrixUtilities:
    """Test Vectrix utility methods."""

    @requires_models
    def test_count(self, sample_texts):
        """Test count method."""
        db = Vectrix("test_count")
        assert db.count() == 0

        db.add(sample_texts)
        assert db.count() == len(sample_texts)
        db.close()

    @requires_models
    def test_clear(self, sample_texts):
        """Test clear method."""
        db = Vectrix("test_clear")
        db.add(sample_texts)
        assert db.count() > 0

        db.clear()
        assert db.count() == 0
        db.close()

    @requires_models
    def test_close(self, sample_texts):
        """Test close method."""
        db = Vectrix("test_close")
        db.add(sample_texts)
        db.close()
        # Should not raise error


class TestConvenienceFunctions:
    """Test module-level convenience functions."""

    def test_create_function(self):
        """Test create() function."""
        from vectrixdb import create

        db = create("test_create")
        assert db is not None
        db.close()

    @requires_models
    def test_open_function(self, sample_texts):
        """Test open() function."""
        from vectrixdb import open as vectrix_open

        # Create first
        db1 = Vectrix("test_open")
        db1.add(sample_texts)
        db1.close()

        # Open existing
        db2 = vectrix_open("test_open")
        assert db2 is not None
        db2.close()

    @requires_models
    def test_quick_search_keeps_nothing(self, tmp_path):
        """Documented as keeping nothing: it wrote ./vectrixdb_data, and
        concurrent calls cleared each other's collection."""
        import threading

        from vectrixdb.easy import quick_search

        found = []

        def run(i):
            found.append(quick_search([f"note {i} about cats", "Rust is fast"], "cats").top.text)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(found) == [f"note {i} about cats" for i in range(3)]
        assert list(tmp_path.iterdir()) == []


class TestRecordedModelNames:
    """A collection an older server wrote records the embedder's key where
    the library records the model's name. Both mean the same vectors."""

    def test_a_key_and_its_name_are_the_same_model(self, tmp_path):
        import warnings

        from vectrixdb.exceptions import ModelMismatchWarning

        with Vectrix("k", path=str(tmp_path)) as db:
            db._collection.set_meta("embedding_model", "bge_small_en")
        with warnings.catch_warnings():
            warnings.simplefilter("error", ModelMismatchWarning)
            Vectrix("k", path=str(tmp_path)).close()

    def test_another_model_still_warns(self, tmp_path):
        from vectrixdb.exceptions import ModelMismatchWarning

        with Vectrix("k", path=str(tmp_path)) as db:
            db._collection.set_meta("embedding_model", "dense_en")
        with pytest.warns(ModelMismatchWarning):
            Vectrix("k", path=str(tmp_path)).close()


class TestKeywordHalfSkipsHighlights:
    """The library's hybrid and ultimate searches fetch ten times the limit
    from the keyword index, and every one of those came back with highlights:
    each candidate's whole text tokenized again, then thrown away, because
    nothing in the library reads them. On SciFact that was nine tenths of a
    hybrid query's time. The REST keyword route still asks for them."""

    @pytest.fixture
    def counted(self, monkeypatch):
        from vectrixdb.core.collection import TextIndex

        calls = []
        real = TextIndex.get_highlights

        def counting(self, doc_id, query, max_length=150):
            calls.append(doc_id)
            return real(self, doc_id, query, max_length)

        monkeypatch.setattr(TextIndex, "get_highlights", counting)
        return calls

    @requires_models
    @pytest.mark.parametrize("mode", ["hybrid", "sparse"])
    def test_no_highlights_are_made_for_a_library_search(self, counted, mode, tmp_path):
        texts = [f"invoice {i} for the harbour office, paid in {2000 + i}" for i in range(40)]
        with Vectrix("h", path=str(tmp_path), mode="hybrid") as db:
            db.add(texts)
            results = db.search("harbour invoice", mode=mode, limit=5, rerank=False)
        assert len(results) == 5
        assert counted == []

    def test_the_collection_still_makes_them_when_asked(self, counted, tmp_path):
        import numpy as np

        from vectrixdb.core.database import VectrixDB

        db = VectrixDB(str(tmp_path / "raw"))
        col = db.create_collection("c", dimension=4, enable_text_index=True)
        col.add(
            ids=["a", "b"],
            vectors=np.eye(4, dtype=np.float32)[:2],
            texts=["the harbour office pays invoices", "nothing about it"],
        )
        found = col.keyword_search("harbour", limit=2)
        assert counted == ["a"]
        assert found.results[0].highlights == ["the harbour office pays invoices"]
        db.close()
