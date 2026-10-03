"""
Tests for VectrixDB storage backends.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from vectrixdb import (
    VectrixDB,
    StorageBackend,
    StorageConfig,
    InMemoryStorage,
    SQLiteStorage,
)


class TestStorageBackendEnum:
    """Test StorageBackend enum."""

    def test_storage_backends_exist(self):
        """Test all storage backends are defined."""
        assert StorageBackend.MEMORY == "memory"
        assert StorageBackend.SQLITE == "sqlite"
        assert StorageBackend.COSMOSDB == "cosmosdb"
        assert StorageBackend.POSTGRESQL == "postgresql"
        assert StorageBackend.LAKEBASE == "lakebase"
        assert StorageBackend.DELTA_LAKE == "delta_lake"


class TestStorageConfig:
    """Test StorageConfig dataclass."""

    def test_default_config(self):
        """Test default storage config."""
        config = StorageConfig()
        assert config.backend == StorageBackend.SQLITE

    def test_memory_config(self):
        """Test memory storage config."""
        config = StorageConfig(backend=StorageBackend.MEMORY)
        assert config.backend == StorageBackend.MEMORY

    def test_delta_lake_config(self):
        """Test Delta Lake storage config."""
        config = StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url="https://adb-123.azuredatabricks.net",
            delta_token="dapi_test_token",
            delta_catalog="main",
            delta_schema="vectrixdb",
        )

        assert config.backend == StorageBackend.DELTA_LAKE
        assert config.delta_workspace_url == "https://adb-123.azuredatabricks.net"
        assert config.delta_token == "dapi_test_token"
        assert config.delta_catalog == "main"
        assert config.delta_schema == "vectrixdb"

    def test_lakebase_config(self):
        """Test Lakebase storage config."""
        config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host="workspace.cloud.databricks.com",
            lakebase_database="vectrixdb",
            lakebase_token="dapi_test",
        )

        assert config.backend == StorageBackend.LAKEBASE
        assert config.lakebase_host == "workspace.cloud.databricks.com"
        assert config.lakebase_database == "vectrixdb"


class TestInMemoryStorage:
    """Test InMemoryStorage backend."""

    def test_create_in_memory_db(self):
        """Test creating database with memory storage."""
        config = StorageConfig(backend=StorageBackend.MEMORY)
        db = VectrixDB(storage_config=config)

        assert db is not None

        # Add data
        coll = db.create_collection("test", dimension=4)
        coll.add(ids=["v1"], vectors=[[0.1, 0.2, 0.3, 0.4]])

        assert coll.count() == 1
        db.close()

    def test_memory_storage_not_persistent(self):
        """Test that memory storage is not persistent."""
        config = StorageConfig(backend=StorageBackend.MEMORY)

        # Create and add data
        db1 = VectrixDB(storage_config=config)
        coll = db1.create_collection("test", dimension=4)
        coll.add(ids=["v1"], vectors=[[0.1, 0.2, 0.3, 0.4]])
        db1.close()

        # Create new instance - should be empty
        db2 = VectrixDB(storage_config=config)
        assert len(db2.list_collections()) == 0
        db2.close()


class TestSQLiteStorage:
    """Test SQLiteStorage backend."""

    def test_create_sqlite_db(self, temp_dir):
        """Test creating database with SQLite storage."""
        db = VectrixDB(path=temp_dir)

        coll = db.create_collection("test", dimension=4)
        coll.add(ids=["v1"], vectors=[[0.1, 0.2, 0.3, 0.4]])

        assert coll.count() == 1
        db.close()

    def test_sqlite_persistence(self, temp_dir):
        """Test SQLite storage persists data."""
        # Create and add data
        db1 = VectrixDB(path=temp_dir)
        coll = db1.create_collection("test", dimension=4)
        coll.add(ids=["v1"], vectors=[[0.1, 0.2, 0.3, 0.4]])
        db1.close()

        # Reopen - data should persist
        db2 = VectrixDB(path=temp_dir)
        coll = db2.get_collection("test")
        assert coll is not None
        assert coll.count() == 1
        db2.close()


class TestDeltaLakeStorage:
    """Test DeltaLakeStorage backend (mocked)."""

    def test_with_delta_lake_factory(self):
        """Test VectrixDB.with_delta_lake factory method exists."""
        # Just verify the method exists and has correct signature
        assert hasattr(VectrixDB, "with_delta_lake")
        assert callable(VectrixDB.with_delta_lake)

    @patch("vectrixdb.core.storage.DeltaLakeStorage")
    def test_delta_lake_config_creation(self, mock_storage):
        """Test Delta Lake config is created correctly."""
        config = StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url="https://adb-123.azuredatabricks.net",
            delta_token="dapi_test",
            delta_catalog="main",
            delta_schema="vectrixdb",
            delta_warehouse_id="abc123",
        )

        assert config.delta_workspace_url == "https://adb-123.azuredatabricks.net"
        assert config.delta_token == "dapi_test"
        assert config.delta_catalog == "main"
        assert config.delta_schema == "vectrixdb"
        assert config.delta_warehouse_id == "abc123"


class TestLakebaseStorage:
    """Test LakebaseStorage backend (mocked)."""

    def test_with_lakebase_factory(self):
        """Test VectrixDB.with_lakebase factory method exists."""
        assert hasattr(VectrixDB, "with_lakebase")
        assert callable(VectrixDB.with_lakebase)

    def test_lakebase_config_with_schema(self):
        """Test Lakebase config with schema parameter."""
        config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host="workspace.cloud.databricks.com",
            lakebase_database="vectrixdb",
            lakebase_schema="custom_schema",
            lakebase_token="dapi_test",
        )

        assert config.lakebase_schema == "custom_schema"


class TestLakebaseStorageMocked:
    """Test LakebaseStorage with mocked psycopg2."""

    @patch("vectrixdb.core.storage.psycopg2", create=True)
    def test_ensure_collection_table_filters_by_schema(self, mock_psycopg2):
        """Test that _ensure_collection_table queries with table_schema filter."""
        from vectrixdb.core.storage import LakebaseStorage, StorageConfig, StorageBackend

        # Setup mock connection and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_psycopg2.connect.return_value = mock_conn

        # Mock fetchall to return empty (no existing table)
        mock_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = {"config": {"dimension": 384, "mode": "ultimate"}}

        config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host="test.databricks.com",
            lakebase_database="testdb",
            lakebase_schema="public",
            lakebase_token="test_token",
        )

        storage = LakebaseStorage(config)
        storage._conn = mock_conn  # Inject mock connection
        storage._ensure_collection_table("test_collection", dimension=384, mode="ultimate")

        # Verify the information_schema query includes table_schema
        calls = mock_cursor.execute.call_args_list
        schema_query_found = False
        for call in calls:
            query = call[0][0] if call[0] else ""
            if "information_schema.columns" in query:
                assert "table_schema" in query, f"Query missing table_schema filter: {query}"
                schema_query_found = True

        assert schema_query_found, "information_schema query not found in execute calls"

    @patch("vectrixdb.core.storage.psycopg2", create=True)
    def test_ensure_collection_table_drops_old_schema(self, mock_psycopg2):
        """Test that table is dropped when missing dense_embedding column."""
        from vectrixdb.core.storage import LakebaseStorage, StorageConfig, StorageBackend

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_psycopg2.connect.return_value = mock_conn

        # Return old schema columns (missing dense_embedding)
        mock_cursor.fetchall.return_value = [
            {"column_name": "id"},
            {"column_name": "embedding"},  # Old column name
            {"column_name": "metadata"},
        ]
        mock_cursor.fetchone.return_value = {"config": {"dimension": 384, "mode": "ultimate"}}

        config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host="test.databricks.com",
            lakebase_database="testdb",
            lakebase_schema="public",
            lakebase_token="test_token",
        )

        storage = LakebaseStorage(config)
        storage._conn = mock_conn
        storage._ensure_collection_table("old_table", dimension=384, mode="ultimate")

        # Verify DROP TABLE was called
        calls = [str(call) for call in mock_cursor.execute.call_args_list]
        drop_found = any("DROP TABLE" in str(call) for call in calls)
        assert drop_found, f"DROP TABLE not called. Calls: {calls}"

        # Verify CREATE TABLE was called with dense_embedding
        create_found = any(
            "CREATE TABLE" in str(call) and "dense_embedding" in str(call) for call in calls
        )
        assert create_found, f"CREATE TABLE with dense_embedding not called. Calls: {calls}"

    @patch("vectrixdb.core.storage.psycopg2", create=True)
    def test_insert_uses_schema_filter(self, mock_psycopg2):
        """Test that insert method queries columns with schema filter."""
        from vectrixdb.core.storage import LakebaseStorage, StorageConfig, StorageBackend

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_psycopg2.connect.return_value = mock_conn

        # Return full schema columns
        mock_cursor.fetchall.return_value = [
            {"column_name": "id"},
            {"column_name": "dense_embedding"},
            {"column_name": "sparse_embedding"},
            {"column_name": "late_interaction_embedding"},
            {"column_name": "metadata"},
            {"column_name": "text_content"},
        ]

        config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host="test.databricks.com",
            lakebase_database="testdb",
            lakebase_schema="myschema",  # Custom schema
            lakebase_token="test_token",
        )

        storage = LakebaseStorage(config)
        storage._conn = mock_conn
        storage.insert("test_coll", "id1", {"_embedding": [0.1, 0.2, 0.3], "text_content": "test"})

        # Verify schema filter in information_schema query
        calls = mock_cursor.execute.call_args_list
        for call in calls:
            query = call[0][0] if call[0] else ""
            if "information_schema.columns" in query:
                assert "table_schema" in query, f"Query missing table_schema: {query}"
                # Check that myschema is passed as parameter
                params = call[0][1] if len(call[0]) > 1 else ()
                assert "myschema" in params, f"Schema param not passed: {params}"


class TestCreateStorage:
    """Test create_storage factory function."""

    def test_create_memory_storage(self):
        """Test creating memory storage."""
        from vectrixdb.core.storage import create_storage

        config = StorageConfig(backend=StorageBackend.MEMORY)
        storage = create_storage(config)

        assert storage is not None

    def test_create_sqlite_storage_via_vectrixdb(self, temp_dir):
        """Test creating SQLite storage via VectrixDB."""
        # Use VectrixDB directly which handles storage creation
        db = VectrixDB(path=temp_dir)
        assert db is not None
        db.close()


class TestCoreAuditRegressions:
    def test_a_worker_thread_reconnects_after_delete_collection(self, tmp_path):
        """delete_collection closed the worker's connection but left it in the
        worker's cache: "Cannot operate on a closed database" ever after."""
        from concurrent.futures import ThreadPoolExecutor

        storage = SQLiteStorage(
            StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path))
        )
        storage.connect()
        storage.create_collection("x", {})
        with ThreadPoolExecutor(1) as worker:
            worker.submit(lambda: storage.insert("x", "a", {"v": 1})).result()
            storage.delete_collection("x")
            storage.create_collection("x", {})
            worker.submit(lambda: storage.insert("x", "b", {"v": 2})).result()
            assert worker.submit(lambda: storage.get("x", "b")).result() is not None
        storage.close()

    @pytest.mark.parametrize("metric", ["euclidean", "dot", "manhattan"])
    def test_sqlite_vector_search_measures_by_the_collections_metric(self, tmp_path, metric):
        """vector_search ranked by cosine whatever the collection was made with. The distance is
        what the local index's would be, euclidean squared, so the scores agree."""
        storage = SQLiteStorage(
            StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path))
        )
        storage.connect()
        storage.create_collection("x", {"dimension": 2, "metric": metric})
        storage.insert("x", "far_same_direction", {"_embedding": [10.0, 0.0]})
        storage.insert("x", "near", {"_embedding": [1.0, 1.0]})
        storage.insert("x", "unit", {"_embedding": [1.0, 0.0]})
        found = storage.vector_search("x", [1.0, 0.0], limit=3)
        ids = [id_ for id_, _, _ in found]
        distances = {id_: d for id_, _, d in found}
        if metric == "euclidean":
            assert ids == ["unit", "near", "far_same_direction"]
            assert distances["near"] == pytest.approx(1.0) and distances[
                "far_same_direction"
            ] == pytest.approx(81.0)
        elif metric == "manhattan":
            assert ids == ["unit", "near", "far_same_direction"]
            assert distances["far_same_direction"] == pytest.approx(9.0)
        else:
            assert ids == ["far_same_direction", "near", "unit"], (
                "by dot product the longest vector wins"
            )
            assert distances["far_same_direction"] == pytest.approx(-9.0)
        assert [id_ for id_, _, _ in storage.vector_search("x", [1.0, 0.0], limit=1)] == ids[:1]
        storage.close()

    def test_sqlite_vector_search_is_cosine_when_the_config_says_so_or_nothing(self, tmp_path):
        storage = SQLiteStorage(
            StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path))
        )
        storage.connect()
        storage.create_collection("x", {"dimension": 2})
        storage.insert("x", "far_same_direction", {"_embedding": [10.0, 0.0]})
        storage.insert("x", "near", {"_embedding": [1.0, 1.0]})
        found = storage.vector_search("x", [1.0, 0.0], limit=2)
        assert [id_ for id_, _, _ in found] == ["far_same_direction", "near"]
        assert found[0][2] == pytest.approx(0.0)
        storage.close()

    def test_a_backend_search_widens_its_window_for_a_selective_filter(self, tmp_path):
        """_search_backend fetched limit * 2 and filtered that, so a filter one point in forty passed
        found nothing while the local path found it. The window now grows until the limit is filled
        or the store is exhausted. The result's metadata is the caller's, the store's own columns
        (text_content, the embeddings) left out and a key of theirs starting with _ kept."""
        import numpy as np

        from vectrixdb import VectrixDB

        db = VectrixDB(
            path=str(tmp_path / "db"),
            storage_config=StorageConfig(
                backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path / "store")
            ),
        )
        for metric in ("cosine", "euclidean"):
            c = db.create_collection("c_" + metric, dimension=2, metric=metric)
            c.add(
                ids=[f"p{i}" for i in range(40)],
                vectors=[[1.0, float(i)] for i in range(40)],
                metadata=[{"k": i, "_mine": "kept"} for i in range(40)],
                texts=[f"text {i}" for i in range(40)],
            )
            query = np.array([1.0, 39.0], dtype=np.float32)
            asked = []
            original = c._storage_backend.vector_search

            def counting(*args, _original=original, _asked=asked, **kwargs):
                _asked.append(kwargs.get("limit"))
                return _original(*args, **kwargs)

            c._storage_backend.vector_search = counting
            try:
                found = c.search(
                    query=query,
                    limit=5,
                    filter={"k": {"$lte": 2}},
                    use_backend=True,
                    use_cache=False,
                )
                assert [r.id for r in found.results] == ["p2", "p1", "p0"]
                assert asked == [10, 40, 160], (
                    "limit * 2, then four times wider until the limit is filled or the store is out"
                )
                assert found.results[0].metadata == {"k": 2, "_mine": "kept"}
                assert found.results[0].text == "text 2"
                local = c.search(
                    query=query,
                    limit=5,
                    filter={"k": {"$lte": 2}},
                    use_backend=False,
                    use_cache=False,
                )
                assert [(r.id, round(r.score, 4)) for r in local.results] == [
                    (r.id, round(r.score, 4)) for r in found.results
                ]
                # Unfiltered, one trip, and the whole limit.
                asked.clear()
                plain = c.search(query=query, limit=5, use_backend=True, use_cache=False)
                assert asked == [5] and len(plain.results) == 5
            finally:
                c._storage_backend.vector_search = original
        db.close()

    def test_postgres_identifiers_are_quoted_and_escaped(self):
        from vectrixdb.core.storage import AuroraPostgreSQLStorage, LakebaseStorage

        aurora = object.__new__(AuroraPostgreSQLStorage)
        aurora.config = StorageConfig()
        assert aurora._table_name('a"b; drop') == '"public"."a""b; drop"'
        lakebase = object.__new__(LakebaseStorage)
        lakebase.config = StorageConfig()
        assert lakebase._table_ref('x"y') == '"public"."x""y"'

    def test_aurora_index_names_are_quoted(self):
        from unittest.mock import MagicMock

        from vectrixdb.core.storage import AuroraPostgreSQLStorage

        aurora = object.__new__(AuroraPostgreSQLStorage)
        aurora.config = StorageConfig()
        aurora._conn = MagicMock()
        aurora._ensure_collection_table('we"ird', mode="hybrid")
        cur = aurora._conn.cursor.return_value.__enter__.return_value
        sent = [call.args[0] for call in cur.execute.call_args_list]
        assert any('"we""ird_hnsw_idx" ON "public"."we""ird"' in sql for sql in sent)
        assert any('"we""ird_sparse_idx"' in sql for sql in sent)
