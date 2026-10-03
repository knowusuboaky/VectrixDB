"""
Tests for VectrixDB core database functionality.
"""

import pytest
from pathlib import Path

from vectrixdb import VectrixDB, Collection, DistanceMetric


class TestVectrixDBInit:
    """Test VectrixDB initialization."""

    def test_create_in_memory(self):
        """Test creating in-memory database."""
        db = VectrixDB()
        assert db is not None
        assert len(db) == 0
        db.close()

    def test_create_with_path(self, temp_dir):
        """Test creating database with path."""
        db_path = Path(temp_dir) / "test_db"
        db = VectrixDB(str(db_path))
        assert db is not None
        db.close()

    def test_database_info(self):
        """Test getting database info."""
        db = VectrixDB()
        info = db.info()
        assert info.collections_count == 0
        assert info.total_vectors == 0
        db.close()


class TestCollections:
    """Test collection management."""

    def test_create_collection(self):
        """Test creating a collection."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)
        assert coll is not None
        assert coll.name == "test"
        assert coll.dimension == 4
        db.close()

    def test_create_collection_with_metric(self):
        """Test creating collection with different metrics."""
        db = VectrixDB()

        coll_cosine = db.create_collection("cosine", dimension=4, metric=DistanceMetric.COSINE)
        assert coll_cosine.metric == DistanceMetric.COSINE

        coll_euclidean = db.create_collection(
            "euclidean", dimension=4, metric=DistanceMetric.EUCLIDEAN
        )
        assert coll_euclidean.metric == DistanceMetric.EUCLIDEAN

        db.close()

    def test_list_collections(self):
        """Test listing collections."""
        db = VectrixDB()
        db.create_collection("coll1", dimension=4)
        db.create_collection("coll2", dimension=8)

        collections = db.list_collections()
        names = [c.name for c in collections]

        assert "coll1" in names
        assert "coll2" in names
        assert len(collections) == 2
        db.close()

    def test_get_collection(self):
        """Test getting a collection by name."""
        db = VectrixDB()
        db.create_collection("test", dimension=4)

        coll = db.get_collection("test")
        assert coll is not None
        assert coll.name == "test"

        # Non-existent collection raises KeyError
        with pytest.raises(KeyError):
            db.get_collection("nonexistent")
        db.close()

    def test_delete_collection(self):
        """Test deleting a collection."""
        db = VectrixDB()
        db.create_collection("to_delete", dimension=4)
        assert db.get_collection("to_delete") is not None

        db.delete_collection("to_delete")

        # After delete, should raise KeyError
        with pytest.raises(KeyError):
            db.get_collection("to_delete")
        db.close()


class TestVectorOperations:
    """Test vector CRUD operations."""

    def test_add_vectors(self, sample_vectors):
        """Test adding vectors to collection."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        ids = ["v1", "v2", "v3", "v4"]
        coll.add(ids=ids, vectors=sample_vectors)

        assert coll.count() == 4
        db.close()

    def test_add_vectors_with_metadata(self, sample_vectors, sample_metadata):
        """Test adding vectors with metadata."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        ids = ["v1", "v2", "v3", "v4"]
        coll.add(ids=ids, vectors=sample_vectors, metadata=sample_metadata)

        # Get and verify metadata
        point = coll.get("v1")
        assert point is not None
        assert point.metadata.get("category") == "programming"
        db.close()

    def test_get_vector(self, sample_vectors):
        """Test getting a vector by ID."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        coll.add(ids=["v1"], vectors=[sample_vectors[0]])

        point = coll.get("v1")
        assert point is not None
        assert point.id == "v1"
        assert len(point.vector) == 4
        db.close()

    def test_delete_vector(self, sample_vectors):
        """Test deleting a vector."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        coll.add(ids=["v1", "v2"], vectors=sample_vectors[:2])
        assert coll.count() == 2

        coll.delete(ids=["v1"])
        assert coll.count() == 1
        assert coll.get("v1") is None
        assert coll.get("v2") is not None
        db.close()


class TestSearch:
    """Test vector search operations."""

    def test_basic_search(self, sample_vectors):
        """Test basic vector search."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        ids = ["v1", "v2", "v3", "v4"]
        coll.add(ids=ids, vectors=sample_vectors)

        results = coll.search(query=sample_vectors[0], limit=2)

        assert results is not None
        assert len(results.results) <= 2
        assert results.results[0].id == "v1"  # Should be most similar to itself
        db.close()

    def test_search_with_filter(self, sample_vectors, sample_metadata):
        """Test search with metadata filter."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        ids = ["v1", "v2", "v3", "v4"]
        coll.add(ids=ids, vectors=sample_vectors, metadata=sample_metadata)

        # Search with filter
        results = coll.search(query=sample_vectors[0], limit=10, filter={"category": "programming"})

        assert results is not None
        assert len(results.results) >= 1
        db.close()

    def test_search_limit(self, sample_vectors):
        """Test search respects limit."""
        db = VectrixDB()
        coll = db.create_collection("test", dimension=4)

        ids = ["v1", "v2", "v3", "v4"]
        coll.add(ids=ids, vectors=sample_vectors)

        results = coll.search(query=sample_vectors[0], limit=2)
        assert len(results.results) == 2

        results = coll.search(query=sample_vectors[0], limit=1)
        assert len(results.results) == 1
        db.close()


class TestOneBackendTwoProcesses:
    """A server beside an ingest worker reads what the worker made, not only what it made itself.

    One in-memory store stands in for the backend both reach. It is handed
    over as a Cosmos DB one, because what makes a backend shared is that it
    is not SQLite or memory on this machine; nothing here opens Cosmos.
    """

    @pytest.fixture
    def shared(self, monkeypatch):
        from vectrixdb.core.storage import InMemoryStorage, StorageBackend, StorageConfig

        store = InMemoryStorage(StorageConfig(backend=StorageBackend.MEMORY))
        monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: store)
        return StorageConfig(backend=StorageBackend.COSMOSDB), store

    def test_the_server_lists_and_opens_what_the_worker_made(self, shared, tmp_path):
        config, _ = shared
        worker = VectrixDB(path=tmp_path / "worker", storage_config=config)
        worker.create_collection(
            "handbook", dimension=4, tags=["Hybrid"], description="the staff handbook"
        )
        server = VectrixDB(path=tmp_path / "server", storage_config=config, follow_shared=True)
        assert [info.name for info in server.list_collections()] == ["handbook"]
        opened = server.get_collection("handbook")
        assert (opened.dimension, opened.info().has_text_index, opened.info().description) == (
            4,
            True,
            "the staff handbook",
        )

    def test_one_made_after_the_server_started_is_found_by_name(self, shared, tmp_path):
        config, _ = shared
        server = VectrixDB(path=tmp_path / "server", storage_config=config, follow_shared=True)
        VectrixDB(path=tmp_path / "worker", storage_config=config).create_collection(
            "late", dimension=4
        )
        assert server.has_collection("late") and server.get_collection("late").dimension == 4
        assert "late" in [info.name for info in server.list_collections()]

    def test_without_follow_shared_nothing_changes(self, shared, tmp_path):
        """Vectrix creates its collection when the lookup misses, with its own options, so the default must still miss."""
        config, _ = shared
        VectrixDB(path=tmp_path / "worker", storage_config=config).create_collection(
            "handbook", dimension=4
        )
        handle = VectrixDB(path=tmp_path / "handle", storage_config=config)
        assert handle.list_collections() == [] and not handle.has_collection("handbook")
        with pytest.raises(KeyError):
            handle.get_collection("handbook")
        assert (
            handle.open_shared() == ["handbook"]
            and handle.get_collection("handbook").dimension == 4
        )

    def test_a_backend_on_this_machine_is_left_alone(self, tmp_path):
        db = VectrixDB(path=tmp_path / "local", follow_shared=True)
        assert db.open_shared() == [] and db.list_collections() == []

    def test_a_backend_that_cannot_be_read_leaves_the_server_up(
        self, shared, tmp_path, monkeypatch
    ):
        config, store = shared

        def down():
            raise ConnectionError("the service did not answer")

        monkeypatch.setattr(store, "list_collections", down)
        server = VectrixDB(path=tmp_path / "server", storage_config=config, follow_shared=True)
        assert server.list_collections() == []

    def test_a_record_that_will_not_open_is_named_and_the_others_open(self, shared, tmp_path):
        from vectrixdb.exceptions import StorageOperationError

        config, store = shared
        store.create_collection("broken", {"metric": "cosine"})
        store.create_collection("fine", {"dimension": 4, "metric": "cosine"})
        server = VectrixDB(path=tmp_path / "server", storage_config=config, follow_shared=True)
        assert [info.name for info in server.list_collections()] == ["fine"]
        assert "names no dimension" in server.failed_collections["broken"]
        with pytest.raises(StorageOperationError):
            server.get_collection("broken")

    def test_the_api_server_follows_what_it_shares(self):
        import vectrixdb.api.server as server

        source = Path(server.__file__).read_text(encoding="utf-8")
        assert "follow_shared=True" in source.split("_db = VectrixDB(", 1)[1][:600]


class TestRegressionsFromTheCoreAudit:
    """Search cache, sparse persistence, writes, rebuilds and names."""

    def _collection(self, tmp_path, **kwargs):
        db = VectrixDB(str(tmp_path / "db"))
        return db, db.create_collection("t", dimension=4, **kwargs)

    def test_the_search_cache_keys_on_threshold_and_hands_out_copies(self, tmp_path):
        from vectrixdb.core.cache import CacheBackend, CacheConfig

        db = VectrixDB(str(tmp_path / "db"), cache_config=CacheConfig(backend=CacheBackend.MEMORY))
        c = db.create_collection("t", dimension=4)
        c.add(ids=["a", "b", "c"], vectors=[[1, 0, 0, 0], [0.7, 0.7, 0, 0], [0, 0, 1, 0]])
        assert len(c.search([1, 0, 0, 0], limit=3, score_threshold=0.99).results) == 1
        assert len(c.search([1, 0, 0, 0], limit=3).results) == 3
        c.search([0, 0, 1, 0], limit=3).results.clear()
        assert len(c.search([0, 0, 1, 0], limit=3).results) == 3
        db.close()

    def test_sparse_vectors_survive_a_reopen_and_go_on_delete(self, tmp_path):
        db, c = self._collection(tmp_path)
        c.add(
            ids=["a", "b"],
            vectors=[[1, 0, 0, 0], [0, 1, 0, 0]],
            sparse_vectors=[{1: 1.0, 5: 0.5}, {1: 0.5}],
        )
        db.close()
        db = VectrixDB(str(tmp_path / "db"))
        c = db.get_collection("t")
        assert [r.id for r in c.sparse_search({1: 1.0}).results] == ["a", "b"]
        c.delete(["a"])
        assert [r.id for r in c.sparse_search({1: 1.0}).results] == ["b"]
        db.close()
        db = VectrixDB(str(tmp_path / "db"))
        assert [r.id for r in db.get_collection("t").sparse_search({1: 1.0}).results] == ["b"]
        db.close()

    def test_a_wrong_dimension_writes_nothing_and_an_empty_add_is_zero(self, tmp_path):
        from vectrixdb.core.types import Point

        db, c = self._collection(tmp_path)
        assert c.add(ids=[], vectors=[]) == 0
        result = c.add_batch([Point(id="a", vector=[1.0, 2.0, 3.0])])
        assert result.error_count == 1
        assert c.count() == 0 and c.get("a") is None
        db.close()

    def test_a_write_during_rebuild_index_is_kept(self, tmp_path):
        db, c = self._collection(tmp_path)
        c.add(ids=["a", "b"], vectors=[[1, 0, 0, 0], [0, 1, 0, 0]])
        real = c._index

        class WriteMidRebuild:
            fired = False

            def __getattr__(self, name):
                return getattr(real, name)

            def contains(self, key):
                if not WriteMidRebuild.fired:
                    WriteMidRebuild.fired = True
                    c.add(ids=["late"], vectors=[[0, 0, 1, 0]])
                return real.contains(key)

        c._index = WriteMidRebuild()
        assert c.rebuild_index() == 3
        hits = c.search([0, 0, 1, 0], limit=1, use_cache=False).results
        assert [r.id for r in hits] == ["late"]
        db.close()

    def test_a_delete_during_rebuild_index_is_kept_too(self, tmp_path):
        """A key the build had already copied into the fresh index and a delete then removed from the
        collection came back at the swap, and the deleted point was found again. Deletes made during
        the build are recorded like the writes and applied at the swap."""
        import threading

        db, c = self._collection(tmp_path)
        c.add(ids=["a", "b", "gone"], vectors=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
        real = c._index

        class DeleteMidRebuild:
            fired = False

            def __getattr__(self, name):
                return getattr(real, name)

            def contains(self, key):
                # The last key is asked about after the others were read, so the
                # vector of "gone" is already on its way into the fresh index.
                if not DeleteMidRebuild.fired and key == max(c._idx_to_id):
                    DeleteMidRebuild.fired = True
                    worker = threading.Thread(target=c.delete, args=(["gone"],))
                    worker.start()
                    worker.join()
                return real.contains(key)

        c._index = DeleteMidRebuild()
        assert c.rebuild_index() == 2
        assert c.count() == 2 and not c._index.contains(3) or "gone" not in c._id_to_idx
        hits = c.search([0, 0, 1, 0], limit=3, use_cache=False).results
        assert "gone" not in [r.id for r in hits]
        db.close()
        db = VectrixDB(str(tmp_path / "db"))
        reopened = db.get_collection("t")
        assert "gone" not in [
            r.id for r in reopened.search([0, 0, 1, 0], limit=3, use_cache=False).results
        ]
        db.close()

    def test_ef_search_and_the_text_index_setting_are_kept(self, tmp_path):
        from vectrixdb.core.types import IndexConfig, IndexType

        config = IndexConfig(index_type=IndexType.HNSW, hnsw_ef_search=300, hnsw_m=24)
        db, c = self._collection(tmp_path, index_config=config)
        assert c._index.expansion_search == 300 and c._text_index is None
        db.close()
        db = VectrixDB(str(tmp_path / "db"))
        c = db.get_collection("t")
        assert c.index_config.hnsw_ef_search == 300 and c._index.expansion_search == 300
        assert c._text_index is None
        db.close()

    def test_a_readonly_open_leaves_demo_collections_alone(self, tmp_path):
        import os

        db = VectrixDB(str(tmp_path / "db"))
        db.create_collection("demo1", dimension=4, tags=["demo"]).add(
            ids=["a"], vectors=[[1, 0, 0, 0]]
        )
        db.close()
        VectrixDB(str(tmp_path / "db"), readonly=True).close()
        assert os.path.isdir(tmp_path / "db" / "demo1")

    def test_a_collection_that_failed_to_load_can_be_deleted_and_made_again(self, tmp_path):
        import warnings

        db, c = self._collection(tmp_path)
        c.add(ids=["a"], vectors=[[1, 0, 0, 0]])
        db.close()
        (tmp_path / "db" / "t" / "t.usearch").write_bytes(b"garbage" * 10)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            db = VectrixDB(str(tmp_path / "db"))
        assert "t" in db.failed_collections
        with pytest.raises(ValueError, match="failed to load"):
            db.create_collection("t", dimension=4)
        assert db.delete_collection("t") is True
        assert "t" not in db.failed_collections
        db.create_collection("t", dimension=4)
        db.close()

    @pytest.mark.parametrize(
        "name",
        [
            "_meta",
            "_documents",
            "_nodes",
            "_vectrixdb",
            "_META",
            "docs.db",
            "x.db-wal",
            "x.documents",
        ],
    )
    def test_names_that_collide_with_internal_files_are_refused(self, tmp_path, name):
        from vectrixdb.exceptions import InvalidCollectionName

        db = VectrixDB(str(tmp_path / "db"))
        with pytest.raises(InvalidCollectionName):
            db.create_collection(name, dimension=4)
        db.close()
