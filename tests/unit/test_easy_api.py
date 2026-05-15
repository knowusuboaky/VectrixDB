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
except:
    MODELS_AVAILABLE = False

requires_models = pytest.mark.skipif(
    not MODELS_AVAILABLE,
    reason="Requires embedding models to be installed"
)


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
        assert hasattr(results, 'top')
        assert hasattr(results, 'texts')
        assert hasattr(results, 'scores')
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
