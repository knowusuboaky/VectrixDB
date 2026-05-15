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
