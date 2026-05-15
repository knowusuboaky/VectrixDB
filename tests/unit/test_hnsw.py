"""
Tests for VectrixDB HNSW index functionality.
"""

import pytest
import numpy as np

from vectrixdb import NativeHNSWIndex, DistanceFunctions, IndexType


class TestDistanceFunctions:
    """Test distance function implementations."""

    def test_distance_functions_exists(self):
        """Test DistanceFunctions class exists."""
        assert DistanceFunctions is not None

    def test_cosine_method_exists(self):
        """Test cosine method exists."""
        assert hasattr(DistanceFunctions, 'cosine')

    def test_euclidean_method_exists(self):
        """Test euclidean method exists."""
        assert hasattr(DistanceFunctions, 'euclidean')


class TestNativeHNSWIndex:
    """Test NativeHNSWIndex implementation."""

    def test_hnsw_class_exists(self):
        """Test NativeHNSWIndex class exists."""
        assert NativeHNSWIndex is not None

    def test_hnsw_creation(self):
        """Test creating HNSW index."""
        index = NativeHNSWIndex(dimension=4)
        assert index is not None


class TestIndexType:
    """Test IndexType enum."""

    def test_flat_exists(self):
        """Test FLAT index type exists."""
        assert hasattr(IndexType, 'FLAT')

    def test_hnsw_exists(self):
        """Test HNSW index type exists."""
        assert hasattr(IndexType, 'HNSW')
