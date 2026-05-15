"""
Integration tests for VectrixDB.
"""

import pytest
import numpy as np
from pathlib import Path

from vectrixdb import VectrixDB, DistanceMetric


class TestFullWorkflow:
    """Test complete VectrixDB workflows."""

    def test_create_add_search_delete(self, temp_dir, sample_vectors):
        """Test full CRUD workflow."""
        db = VectrixDB(path=temp_dir)

        # Create
        coll = db.create_collection("test", dimension=4)
        assert coll is not None

        # Add
        ids = ["v1", "v2", "v3", "v4"]
        coll.add(ids=ids, vectors=sample_vectors)
        assert coll.count() == 4

        # Search
        results = coll.search(query=sample_vectors[0], limit=2)
        assert len(results.results) == 2

        # Delete
        coll.delete(ids=["v1"])
        assert coll.count() == 3

        db.close()

    def test_multiple_collections(self, temp_dir, sample_vectors):
        """Test working with multiple collections."""
        db = VectrixDB(path=temp_dir)

        # Create multiple collections
        coll1 = db.create_collection("coll1", dimension=4)
        coll2 = db.create_collection("coll2", dimension=4)
        coll3 = db.create_collection("coll3", dimension=4)

        # Add to each
        coll1.add(ids=["v1"], vectors=[sample_vectors[0]])
        coll2.add(ids=["v2"], vectors=[sample_vectors[1]])
        coll3.add(ids=["v3"], vectors=[sample_vectors[2]])

        # Verify
        assert coll1.count() == 1
        assert coll2.count() == 1
        assert coll3.count() == 1
        assert len(db.list_collections()) == 3

        db.close()

    def test_persistence(self, temp_dir, sample_vectors):
        """Test data persistence across sessions."""
        # First session
        db1 = VectrixDB(path=temp_dir)
        coll = db1.create_collection("persistent", dimension=4)
        coll.add(ids=["v1", "v2"], vectors=sample_vectors[:2])
        db1.close()

        # Second session
        db2 = VectrixDB(path=temp_dir)
        coll = db2.get_collection("persistent")
        assert coll is not None
        assert coll.count() == 2
        db2.close()


class TestWithConfigs:
    """Test VectrixDB with various configurations."""

    def test_with_memory_storage(self):
        """Test with memory storage."""
        from vectrixdb import StorageConfig, StorageBackend

        db = VectrixDB(
            storage_config=StorageConfig(backend=StorageBackend.MEMORY)
        )

        coll = db.create_collection("test", dimension=4)
        coll.add(ids=["v1"], vectors=[[0.1, 0.2, 0.3, 0.4]])

        assert coll.count() == 1
        db.close()

    def test_with_cache(self):
        """Test with cache enabled."""
        from vectrixdb import CacheConfig, CacheBackend

        db = VectrixDB(
            cache_config=CacheConfig(backend=CacheBackend.MEMORY)
        )

        coll = db.create_collection("test", dimension=4)
        coll.add(ids=["v1"], vectors=[[0.1, 0.2, 0.3, 0.4]])

        assert coll.count() == 1
        db.close()


class TestLargeScale:
    """Test with larger datasets."""

    def test_100_vectors(self, temp_dir):
        """Test with 100 vectors."""
        db = VectrixDB(path=temp_dir)
        coll = db.create_collection("large", dimension=64)

        # Generate vectors
        vectors = np.random.randn(100, 64).astype(np.float32).tolist()
        ids = [f"v{i}" for i in range(100)]

        coll.add(ids=ids, vectors=vectors)

        assert coll.count() == 100

        # Search
        results = coll.search(query=vectors[0], limit=10)
        assert len(results.results) == 10

        db.close()


class TestMetadataWorkflows:
    """Test metadata-related workflows."""

    def test_add_with_metadata(self, temp_dir):
        """Test adding vectors with metadata."""
        db = VectrixDB(path=temp_dir)
        coll = db.create_collection("meta_test", dimension=4)

        vectors = [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
        metadata = [
            {"category": "A", "score": 10},
            {"category": "B", "score": 20},
        ]

        coll.add(ids=["v1", "v2"], vectors=vectors, metadata=metadata)

        # Verify metadata stored
        point = coll.get("v1")
        assert point.metadata.get("category") == "A"

        db.close()


class TestDistanceMetrics:
    """Test different distance metrics."""

    def test_cosine_metric(self, temp_dir):
        """Test cosine distance metric."""
        db = VectrixDB(path=temp_dir)
        coll = db.create_collection(
            "cosine_test",
            dimension=4,
            metric=DistanceMetric.COSINE
        )

        coll.add(ids=["v1"], vectors=[[1.0, 0.0, 0.0, 0.0]])

        assert coll.count() == 1
        db.close()

    def test_euclidean_metric(self, temp_dir):
        """Test Euclidean distance metric."""
        db = VectrixDB(path=temp_dir)
        coll = db.create_collection(
            "euclidean_test",
            dimension=4,
            metric=DistanceMetric.EUCLIDEAN
        )

        coll.add(ids=["v1"], vectors=[[0.0, 0.0, 0.0, 0.0]])

        assert coll.count() == 1
        db.close()
