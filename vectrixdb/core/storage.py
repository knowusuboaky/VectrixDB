"""
VectrixDB Storage Backends - Pluggable persistence layer.

Supports multiple storage backends:
- InMemory: Fastest, no persistence (for testing/caching)
- SQLite: Local disk persistence (default)
- Azure Cosmos DB: Cloud-scale persistence
- PostgreSQL: Enterprise SQL backend

Author: Daddy Nyame Owusu - Boakye
"""

import json
import os
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
import sqlite3


class StorageBackend(str, Enum):
    """Available storage backends."""
    MEMORY = "memory"
    SQLITE = "sqlite"
    COSMOSDB = "cosmosdb"
    POSTGRESQL = "postgresql"


@dataclass
class StorageConfig:
    """Configuration for storage backends."""
    backend: StorageBackend = StorageBackend.SQLITE

    # SQLite config
    sqlite_path: Optional[str] = None
    sqlite_wal_mode: bool = True  # Write-Ahead Logging for safe restarts

    # Cosmos DB config
    cosmos_endpoint: Optional[str] = None
    cosmos_key: Optional[str] = None
    cosmos_database: str = "vectrixdb"
    cosmos_container: str = "vectors"
    cosmos_throughput: int = 400  # RU/s

    # PostgreSQL config
    postgres_host: Optional[str] = None
    postgres_port: int = 5432
    postgres_database: str = "vectrixdb"
    postgres_user: Optional[str] = None
    postgres_password: Optional[str] = None

    # Performance
    batch_size: int = 1000
    connection_pool_size: int = 10

    @classmethod
    def from_env(cls) -> "StorageConfig":
        """Create config from environment variables."""
        backend = os.getenv("VECTRIX_STORAGE_BACKEND", "sqlite")
        return cls(
            backend=StorageBackend(backend),
            sqlite_path=os.getenv("VECTRIX_SQLITE_PATH"),
            cosmos_endpoint=os.getenv("VECTRIX_COSMOS_ENDPOINT"),
            cosmos_key=os.getenv("VECTRIX_COSMOS_KEY"),
            cosmos_database=os.getenv("VECTRIX_COSMOS_DATABASE", "vectrixdb"),
            postgres_host=os.getenv("VECTRIX_POSTGRES_HOST"),
            postgres_user=os.getenv("VECTRIX_POSTGRES_USER"),
            postgres_password=os.getenv("VECTRIX_POSTGRES_PASSWORD"),
        )


class BaseStorage(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to storage."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close connection."""
        pass

    @abstractmethod
    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        """Create a new collection."""
        pass

    @abstractmethod
    def delete_collection(self, name: str) -> None:
        """Delete a collection."""
        pass

    @abstractmethod
    def list_collections(self) -> List[str]:
        """List all collections."""
        pass

    @abstractmethod
    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        """Get collection configuration."""
        pass

    @abstractmethod
    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        """Insert a single document."""
        pass

    @abstractmethod
    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        """Insert multiple documents. Returns count inserted."""
        pass

    @abstractmethod
    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        """Get a document by ID."""
        pass

    @abstractmethod
    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        """Get multiple documents by ID."""
        pass

    @abstractmethod
    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        """Update a document. Returns True if updated."""
        pass

    @abstractmethod
    def delete(self, collection: str, id: str) -> bool:
        """Delete a document. Returns True if deleted."""
        pass

    @abstractmethod
    def delete_batch(self, collection: str, ids: List[str]) -> int:
        """Delete multiple documents. Returns count deleted."""
        pass

    @abstractmethod
    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[callable] = None
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Scan documents with pagination."""
        pass

    @abstractmethod
    def count(self, collection: str) -> int:
        """Count documents in collection."""
        pass

    @abstractmethod
    def flush(self) -> None:
        """Flush pending writes to storage."""
        pass


class InMemoryStorage(BaseStorage):
    """
    In-memory storage backend.

    Fastest option, no persistence. Use for:
    - Testing
    - Temporary collections
    - As a cache layer
    """

    def __init__(self, config: StorageConfig):
        self.config = config
        self._collections: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._collection_configs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def connect(self) -> None:
        pass  # No connection needed

    def close(self) -> None:
        pass

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        with self._lock:
            if name not in self._collections:
                self._collections[name] = {}
                self._collection_configs[name] = config

    def delete_collection(self, name: str) -> None:
        with self._lock:
            self._collections.pop(name, None)
            self._collection_configs.pop(name, None)

    def list_collections(self) -> List[str]:
        return list(self._collections.keys())

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        return self._collection_configs.get(name)

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        with self._lock:
            if collection in self._collections:
                self._collections[collection][id] = data

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        with self._lock:
            if collection not in self._collections:
                return 0
            count = 0
            for id, data in documents:
                self._collections[collection][id] = data
                count += 1
            return count

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if collection in self._collections:
                return self._collections[collection].get(id)
            return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        return [self.get(collection, id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        with self._lock:
            if collection in self._collections and id in self._collections[collection]:
                self._collections[collection][id].update(data)
                return True
            return False

    def delete(self, collection: str, id: str) -> bool:
        with self._lock:
            if collection in self._collections:
                return self._collections[collection].pop(id, None) is not None
            return False

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        count = 0
        for id in ids:
            if self.delete(collection, id):
                count += 1
        return count

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[callable] = None
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            if collection not in self._collections:
                return

            items = list(self._collections[collection].items())

            if filter_func:
                items = [(k, v) for k, v in items if filter_func(v)]

            for item in items[offset:offset + limit]:
                yield item

    def count(self, collection: str) -> int:
        with self._lock:
            return len(self._collections.get(collection, {}))

    def flush(self) -> None:
        pass  # No-op for in-memory


class SQLiteStorage(BaseStorage):
    """
    SQLite storage backend with WAL mode for safe restarts.

    Features:
    - Write-Ahead Logging (WAL) for crash recovery
    - Automatic checkpointing
    - Connection pooling
    """

    def __init__(self, config: StorageConfig):
        self.config = config
        self.path = Path(config.sqlite_path) if config.sqlite_path else Path("./vectrixdb_data")
        self._connections: Dict[str, sqlite3.Connection] = {}
        self._lock = threading.RLock()

    def connect(self) -> None:
        os.makedirs(self.path, exist_ok=True)

        # Create main metadata database
        main_db = self._get_connection("_meta")
        main_db.executescript("""
            CREATE TABLE IF NOT EXISTS collections (
                name TEXT PRIMARY KEY,
                config TEXT,
                created_at TEXT,
                updated_at TEXT
            );
        """)
        main_db.commit()

    def _get_connection(self, collection: str) -> sqlite3.Connection:
        with self._lock:
            if collection not in self._connections:
                db_path = self.path / f"{collection}.db"
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row

                # Enable WAL mode for safe restarts
                if self.config.sqlite_wal_mode:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA wal_autocheckpoint=1000")

                # Performance optimizations
                conn.execute("PRAGMA cache_size=10000")
                conn.execute("PRAGMA temp_store=MEMORY")

                # Create documents table if not exists
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS documents (
                        id TEXT PRIMARY KEY,
                        data TEXT NOT NULL,
                        created_at TEXT,
                        updated_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_created ON documents(created_at);
                """)
                conn.commit()

                self._connections[collection] = conn

            return self._connections[collection]

    def close(self) -> None:
        with self._lock:
            for conn in self._connections.values():
                # Checkpoint WAL before closing
                if self.config.sqlite_wal_mode:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except:
                        pass
                conn.close()
            self._connections.clear()

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        main_db = self._get_connection("_meta")
        now = datetime.utcnow().isoformat()
        main_db.execute(
            "INSERT OR REPLACE INTO collections (name, config, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(config), now, now)
        )
        main_db.commit()

        # Create collection database
        self._get_connection(name)

    def delete_collection(self, name: str) -> None:
        main_db = self._get_connection("_meta")
        main_db.execute("DELETE FROM collections WHERE name = ?", (name,))
        main_db.commit()

        # Close and delete collection database
        with self._lock:
            if name in self._connections:
                self._connections[name].close()
                del self._connections[name]

        db_path = self.path / f"{name}.db"
        if db_path.exists():
            db_path.unlink()
        # Also delete WAL files
        for suffix in ["-wal", "-shm"]:
            wal_path = self.path / f"{name}.db{suffix}"
            if wal_path.exists():
                wal_path.unlink()

    def list_collections(self) -> List[str]:
        main_db = self._get_connection("_meta")
        cursor = main_db.execute("SELECT name FROM collections")
        return [row["name"] for row in cursor]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        main_db = self._get_connection("_meta")
        row = main_db.execute("SELECT config FROM collections WHERE name = ?", (name,)).fetchone()
        if row:
            return json.loads(row["config"])
        return None

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        conn = self._get_connection(collection)
        now = datetime.utcnow().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO documents (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (id, json.dumps(data), now, now)
        )
        conn.commit()

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        conn = self._get_connection(collection)
        now = datetime.utcnow().isoformat()

        conn.executemany(
            "INSERT OR REPLACE INTO documents (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            [(id, json.dumps(data), now, now) for id, data in documents]
        )
        conn.commit()
        return len(documents)

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection(collection)
        row = conn.execute("SELECT data FROM documents WHERE id = ?", (id,)).fetchone()
        if row:
            return json.loads(row["data"])
        return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        conn = self._get_connection(collection)
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(f"SELECT id, data FROM documents WHERE id IN ({placeholders})", ids)

        results = {row["id"]: json.loads(row["data"]) for row in cursor}
        return [results.get(id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        conn = self._get_connection(collection)
        now = datetime.utcnow().isoformat()

        # Get existing data and merge
        existing = self.get(collection, id)
        if existing:
            existing.update(data)
            conn.execute(
                "UPDATE documents SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(existing), now, id)
            )
            conn.commit()
            return True
        return False

    def delete(self, collection: str, id: str) -> bool:
        conn = self._get_connection(collection)
        cursor = conn.execute("DELETE FROM documents WHERE id = ?", (id,))
        conn.commit()
        return cursor.rowcount > 0

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        conn = self._get_connection(collection)
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", ids)
        conn.commit()
        return cursor.rowcount

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[callable] = None
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        conn = self._get_connection(collection)

        if filter_func:
            # Need to scan all and filter in Python
            cursor = conn.execute("SELECT id, data FROM documents ORDER BY rowid")
            count = 0
            skipped = 0
            for row in cursor:
                data = json.loads(row["data"])
                if filter_func(data):
                    if skipped < offset:
                        skipped += 1
                        continue
                    yield (row["id"], data)
                    count += 1
                    if count >= limit:
                        break
        else:
            cursor = conn.execute(
                "SELECT id, data FROM documents ORDER BY rowid LIMIT ? OFFSET ?",
                (limit, offset)
            )
            for row in cursor:
                yield (row["id"], json.loads(row["data"]))

    def count(self, collection: str) -> int:
        conn = self._get_connection(collection)
        row = conn.execute("SELECT COUNT(*) as cnt FROM documents").fetchone()
        return row["cnt"]

    def flush(self) -> None:
        with self._lock:
            for conn in self._connections.values():
                conn.commit()
                if self.config.sqlite_wal_mode:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except:
                        pass


class CosmosDBStorage(BaseStorage):
    """
    Azure Cosmos DB storage backend.

    Features:
    - Global distribution
    - Automatic scaling
    - 99.999% availability SLA
    - Multi-region writes

    Requires: pip install azure-cosmos
    """

    def __init__(self, config: StorageConfig):
        self.config = config
        self._client = None
        self._database = None
        self._containers: Dict[str, Any] = {}

    def connect(self) -> None:
        try:
            from azure.cosmos import CosmosClient, PartitionKey
            from azure.cosmos.exceptions import CosmosResourceExistsError
        except ImportError:
            raise ImportError("azure-cosmos is required. Install with: pip install azure-cosmos")

        if not self.config.cosmos_endpoint or not self.config.cosmos_key:
            raise ValueError("Cosmos DB endpoint and key are required")

        self._client = CosmosClient(
            self.config.cosmos_endpoint,
            self.config.cosmos_key
        )

        # Create database if not exists
        try:
            self._database = self._client.create_database(self.config.cosmos_database)
        except CosmosResourceExistsError:
            self._database = self._client.get_database_client(self.config.cosmos_database)

        # Create metadata container
        try:
            self._containers["_meta"] = self._database.create_container(
                id="_meta",
                partition_key=PartitionKey(path="/type"),
                offer_throughput=self.config.cosmos_throughput
            )
        except CosmosResourceExistsError:
            self._containers["_meta"] = self._database.get_container_client("_meta")

    def close(self) -> None:
        self._containers.clear()
        self._database = None
        self._client = None

    def _get_container(self, collection: str):
        if collection not in self._containers:
            self._containers[collection] = self._database.get_container_client(collection)
        return self._containers[collection]

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        from azure.cosmos import PartitionKey
        from azure.cosmos.exceptions import CosmosResourceExistsError

        # Create container
        try:
            container = self._database.create_container(
                id=name,
                partition_key=PartitionKey(path="/partition_key"),
                offer_throughput=self.config.cosmos_throughput
            )
            self._containers[name] = container
        except CosmosResourceExistsError:
            self._containers[name] = self._database.get_container_client(name)

        # Store metadata
        meta_container = self._get_container("_meta")
        meta_container.upsert_item({
            "id": name,
            "type": "collection",
            "config": config,
            "created_at": datetime.utcnow().isoformat()
        })

    def delete_collection(self, name: str) -> None:
        try:
            self._database.delete_container(name)
            self._containers.pop(name, None)
        except:
            pass

        # Remove metadata
        try:
            meta_container = self._get_container("_meta")
            meta_container.delete_item(item=name, partition_key="collection")
        except:
            pass

    def list_collections(self) -> List[str]:
        meta_container = self._get_container("_meta")
        query = "SELECT c.id FROM c WHERE c.type = 'collection'"
        items = meta_container.query_items(query, enable_cross_partition_query=True)
        return [item["id"] for item in items]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            meta_container = self._get_container("_meta")
            item = meta_container.read_item(item=name, partition_key="collection")
            return item.get("config")
        except:
            return None

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        container = self._get_container(collection)
        item = {
            "id": id,
            "partition_key": id[:2] if len(id) >= 2 else id,  # Simple partition strategy
            **data,
            "created_at": datetime.utcnow().isoformat()
        }
        container.upsert_item(item)

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        container = self._get_container(collection)
        count = 0
        now = datetime.utcnow().isoformat()

        # Cosmos DB doesn't have native batch insert, but we can use parallel operations
        for id, data in documents:
            item = {
                "id": id,
                "partition_key": id[:2] if len(id) >= 2 else id,
                **data,
                "created_at": now
            }
            container.upsert_item(item)
            count += 1

        return count

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        try:
            container = self._get_container(collection)
            partition_key = id[:2] if len(id) >= 2 else id
            item = container.read_item(item=id, partition_key=partition_key)
            # Remove Cosmos DB metadata
            return {k: v for k, v in item.items() if not k.startswith("_")}
        except:
            return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        return [self.get(collection, id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        existing = self.get(collection, id)
        if existing:
            existing.update(data)
            existing["updated_at"] = datetime.utcnow().isoformat()
            self.insert(collection, id, existing)
            return True
        return False

    def delete(self, collection: str, id: str) -> bool:
        try:
            container = self._get_container(collection)
            partition_key = id[:2] if len(id) >= 2 else id
            container.delete_item(item=id, partition_key=partition_key)
            return True
        except:
            return False

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        count = 0
        for id in ids:
            if self.delete(collection, id):
                count += 1
        return count

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[callable] = None
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        container = self._get_container(collection)

        query = "SELECT * FROM c ORDER BY c._ts OFFSET @offset LIMIT @limit"
        params = [
            {"name": "@offset", "value": offset},
            {"name": "@limit", "value": limit}
        ]

        items = container.query_items(
            query=query,
            parameters=params,
            enable_cross_partition_query=True
        )

        for item in items:
            data = {k: v for k, v in item.items() if not k.startswith("_")}
            if filter_func is None or filter_func(data):
                yield (item["id"], data)

    def count(self, collection: str) -> int:
        container = self._get_container(collection)
        query = "SELECT VALUE COUNT(1) FROM c"
        items = list(container.query_items(query, enable_cross_partition_query=True))
        return items[0] if items else 0

    def flush(self) -> None:
        pass  # Cosmos DB auto-flushes


def create_storage(config: StorageConfig) -> BaseStorage:
    """Factory function to create storage backend."""
    if config.backend == StorageBackend.MEMORY:
        return InMemoryStorage(config)
    elif config.backend == StorageBackend.SQLITE:
        storage = SQLiteStorage(config)
        storage.connect()
        return storage
    elif config.backend == StorageBackend.COSMOSDB:
        storage = CosmosDBStorage(config)
        storage.connect()
        return storage
    else:
        raise ValueError(f"Unknown storage backend: {config.backend}")
