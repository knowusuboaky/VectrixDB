"""
Tests for VectrixDB type definitions.
"""

import pytest

from vectrixdb import (
    DistanceMetric,
    SearchResult,
    SearchResults,
    Point,
    CollectionInfo,
    DatabaseInfo,
    BatchResult,
    SparseVector,
)


class TestDistanceMetric:
    """Test DistanceMetric enum."""

    def test_cosine_exists(self):
        """Test COSINE metric exists."""
        assert hasattr(DistanceMetric, 'COSINE')

    def test_euclidean_exists(self):
        """Test EUCLIDEAN metric exists."""
        assert hasattr(DistanceMetric, 'EUCLIDEAN')

    def test_dot_metric_exists(self):
        """Test DOT metric exists."""
        assert hasattr(DistanceMetric, 'DOT')


class TestPoint:
    """Test Point dataclass."""

    def test_point_class_exists(self):
        """Test Point class exists."""
        assert Point is not None


class TestSearchResult:
    """Test SearchResult dataclass."""

    def test_search_result_class_exists(self):
        """Test SearchResult class exists."""
        assert SearchResult is not None


class TestSearchResults:
    """Test SearchResults container."""

    def test_search_results_class_exists(self):
        """Test SearchResults class exists."""
        assert SearchResults is not None


class TestCollectionInfo:
    """Test CollectionInfo dataclass."""

    def test_collection_info_class_exists(self):
        """Test CollectionInfo class exists."""
        assert CollectionInfo is not None


class TestDatabaseInfo:
    """Test DatabaseInfo dataclass."""

    def test_database_info_class_exists(self):
        """Test DatabaseInfo class exists."""
        assert DatabaseInfo is not None


class TestSparseVector:
    """Test SparseVector dataclass."""

    def test_sparse_vector_class_exists(self):
        """Test SparseVector class exists."""
        assert SparseVector is not None


class TestBatchResult:
    """Test BatchResult dataclass."""

    def test_batch_result_class_exists(self):
        """Test BatchResult class exists."""
        assert BatchResult is not None
