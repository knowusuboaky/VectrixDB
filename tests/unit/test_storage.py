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
            delta_schema="vectrixdb"
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
            lakebase_token="dapi_test"
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
        assert hasattr(VectrixDB, 'with_delta_lake')
        assert callable(VectrixDB.with_delta_lake)

    @patch('vectrixdb.core.storage.DeltaLakeStorage')
    def test_delta_lake_config_creation(self, mock_storage):
        """Test Delta Lake config is created correctly."""
        config = StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url="https://adb-123.azuredatabricks.net",
            delta_token="dapi_test",
            delta_catalog="main",
            delta_schema="vectrixdb",
            delta_warehouse_id="abc123"
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
        assert hasattr(VectrixDB, 'with_lakebase')
        assert callable(VectrixDB.with_lakebase)

    def test_lakebase_config_with_schema(self):
        """Test Lakebase config with schema parameter."""
        config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host="workspace.cloud.databricks.com",
            lakebase_database="vectrixdb",
            lakebase_schema="custom_schema",
            lakebase_token="dapi_test"
        )

        assert config.lakebase_schema == "custom_schema"


class TestLakebaseStorageMocked:
    """Test LakebaseStorage with mocked psycopg2."""

    @patch('vectrixdb.core.storage.psycopg2', create=True)
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
            lakebase_token="test_token"
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

    @patch('vectrixdb.core.storage.psycopg2', create=True)
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
            lakebase_token="test_token"
        )

        storage = LakebaseStorage(config)
        storage._conn = mock_conn
        storage._ensure_collection_table("old_table", dimension=384, mode="ultimate")

        # Verify DROP TABLE was called
        calls = [str(call) for call in mock_cursor.execute.call_args_list]
        drop_found = any("DROP TABLE" in str(call) for call in calls)
        assert drop_found, f"DROP TABLE not called. Calls: {calls}"

        # Verify CREATE TABLE was called with dense_embedding
        create_found = any("CREATE TABLE" in str(call) and "dense_embedding" in str(call) for call in calls)
        assert create_found, f"CREATE TABLE with dense_embedding not called. Calls: {calls}"

    @patch('vectrixdb.core.storage.psycopg2', create=True)
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
            lakebase_token="test_token"
        )

        storage = LakebaseStorage(config)
        storage._conn = mock_conn
        storage.insert("test_coll", "id1", {
            "_embedding": [0.1, 0.2, 0.3],
            "text_content": "test"
        })

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
