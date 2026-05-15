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
    LAKEBASE = "lakebase"  # Databricks Lakebase (PostgreSQL + pgvector)
    DELTA_LAKE = "delta_lake"  # Databricks Delta Lake + Unity Catalog


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

    # Lakebase config (Databricks managed PostgreSQL with pgvector)
    lakebase_host: Optional[str] = None
    lakebase_port: int = 5432
    lakebase_database: str = "vectrixdb"
    lakebase_user: Optional[str] = None
    lakebase_password: Optional[str] = None
    lakebase_token: Optional[str] = None  # Databricks PAT for auth
    lakebase_ssl: bool = True  # SSL required for Databricks
    lakebase_schema: str = "public"  # PostgreSQL schema

    # Delta Lake config (Databricks Unity Catalog)
    delta_workspace_url: Optional[str] = None  # e.g., https://adb-123.azuredatabricks.net
    delta_token: Optional[str] = None  # Databricks PAT
    delta_catalog: str = "main"  # Unity Catalog name
    delta_schema: str = "vectrixdb"  # Schema name (created if not exists)
    delta_warehouse_id: Optional[str] = None  # SQL Warehouse ID (optional)
    delta_http_path: Optional[str] = None  # HTTP path for SQL Warehouse

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
            lakebase_host=os.getenv("VECTRIX_LAKEBASE_HOST"),
            lakebase_database=os.getenv("VECTRIX_LAKEBASE_DATABASE", "vectrixdb"),
            lakebase_user=os.getenv("VECTRIX_LAKEBASE_USER"),
            lakebase_password=os.getenv("VECTRIX_LAKEBASE_PASSWORD"),
            lakebase_token=os.getenv("VECTRIX_LAKEBASE_TOKEN"),
            lakebase_schema=os.getenv("VECTRIX_LAKEBASE_SCHEMA", "public"),
            delta_workspace_url=os.getenv("DATABRICKS_HOST") or os.getenv("VECTRIX_DELTA_WORKSPACE_URL"),
            delta_token=os.getenv("DATABRICKS_TOKEN") or os.getenv("VECTRIX_DELTA_TOKEN"),
            delta_catalog=os.getenv("VECTRIX_DELTA_CATALOG", "main"),
            delta_schema=os.getenv("VECTRIX_DELTA_SCHEMA", "vectrixdb"),
            delta_warehouse_id=os.getenv("VECTRIX_DELTA_WAREHOUSE_ID"),
            delta_http_path=os.getenv("VECTRIX_DELTA_HTTP_PATH"),
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

    # =========================================================================
    # Document Index Methods (for hierarchical document storage)
    # =========================================================================

    def ensure_document_tables(self) -> None:
        """Ensure document and node tables exist. Override in subclasses."""
        pass

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        """Save document metadata. Override in subclasses."""
        pass

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID. Override in subclasses."""
        return None

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents. Override in subclasses."""
        return []

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document. Override in subclasses."""
        return False

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node. Override in subclasses."""
        pass

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID. Override in subclasses."""
        return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document. Override in subclasses."""
        return []

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent. Override in subclasses."""
        return []

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document. Returns count deleted. Override in subclasses."""
        return 0


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

    # =========================================================================
    # Document Index Methods
    # =========================================================================

    def ensure_document_tables(self) -> None:
        """Create document and node tables if they don't exist."""
        # Documents table
        doc_conn = self._get_connection("_documents")
        doc_conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                title TEXT,
                doc_type TEXT,
                source_path TEXT,
                etag TEXT,
                content_hash TEXT,
                page_count INTEGER DEFAULT 0,
                section_count INTEGER DEFAULT 0,
                node_count INTEGER DEFAULT 0,
                indexed_at TEXT,
                last_synced TEXT,
                metadata TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_doc_type ON documents(doc_type);
            CREATE INDEX IF NOT EXISTS idx_indexed_at ON documents(indexed_at);
        """)
        doc_conn.commit()

        # Nodes table
        node_conn = self._get_connection("_nodes")
        node_conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                parent_id TEXT,
                level INTEGER DEFAULT 1,
                title TEXT,
                text TEXT,
                summary TEXT,
                page_num INTEGER,
                position INTEGER DEFAULT 0,
                metadata TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_node_doc ON nodes(doc_id);
            CREATE INDEX IF NOT EXISTS idx_node_parent ON nodes(parent_id);
            CREATE INDEX IF NOT EXISTS idx_node_page ON nodes(page_num);
        """)
        node_conn.commit()

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        """Save document metadata."""
        conn = self._get_connection("_documents")
        now = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT OR REPLACE INTO documents
            (doc_id, title, doc_type, source_path, etag, content_hash,
             page_count, section_count, node_count, indexed_at, last_synced,
             metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_data["doc_id"],
            doc_data.get("title", ""),
            doc_data.get("doc_type", "text"),
            doc_data.get("source_path"),
            doc_data.get("etag"),
            doc_data.get("content_hash"),
            doc_data.get("page_count", 0),
            doc_data.get("section_count", 0),
            doc_data.get("node_count", 0),
            doc_data.get("indexed_at"),
            doc_data.get("last_synced"),
            json.dumps(doc_data.get("metadata", {})),
            now,
            now,
        ))
        conn.commit()

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        conn = self._get_connection("_documents")
        row = conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()

        if row:
            return {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "doc_type": row["doc_type"],
                "source_path": row["source_path"],
                "etag": row["etag"],
                "content_hash": row["content_hash"],
                "page_count": row["page_count"],
                "section_count": row["section_count"],
                "node_count": row["node_count"],
                "indexed_at": row["indexed_at"],
                "last_synced": row["last_synced"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
        return None

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents."""
        conn = self._get_connection("_documents")
        cursor = conn.execute("SELECT * FROM documents ORDER BY indexed_at DESC")
        return [
            {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "doc_type": row["doc_type"],
                "source_path": row["source_path"],
                "etag": row["etag"],
                "content_hash": row["content_hash"],
                "page_count": row["page_count"],
                "section_count": row["section_count"],
                "node_count": row["node_count"],
                "indexed_at": row["indexed_at"],
                "last_synced": row["last_synced"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
            for row in cursor
        ]

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document."""
        conn = self._get_connection("_documents")
        cursor = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        conn.commit()
        return cursor.rowcount > 0

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""
        conn = self._get_connection("_nodes")
        now = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT OR REPLACE INTO nodes
            (node_id, doc_id, parent_id, level, title, text, summary,
             page_num, position, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node_data["node_id"],
            node_data["doc_id"],
            node_data.get("parent_id"),
            node_data.get("level", 1),
            node_data.get("title", ""),
            node_data.get("text", ""),
            node_data.get("summary", ""),
            node_data.get("page_num"),
            node_data.get("position", 0),
            json.dumps(node_data.get("metadata", {})),
            now,
            now,
        ))
        conn.commit()

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""
        conn = self._get_connection("_nodes")
        row = conn.execute(
            "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()

        if row:
            return {
                "node_id": row["node_id"],
                "doc_id": row["doc_id"],
                "parent_id": row["parent_id"],
                "level": row["level"],
                "title": row["title"],
                "text": row["text"],
                "summary": row["summary"],
                "page_num": row["page_num"],
                "position": row["position"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
        return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document."""
        conn = self._get_connection("_nodes")
        cursor = conn.execute(
            "SELECT * FROM nodes WHERE doc_id = ? ORDER BY position",
            (doc_id,)
        )
        return [
            {
                "node_id": row["node_id"],
                "doc_id": row["doc_id"],
                "parent_id": row["parent_id"],
                "level": row["level"],
                "title": row["title"],
                "text": row["text"],
                "summary": row["summary"],
                "page_num": row["page_num"],
                "position": row["position"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
            for row in cursor
        ]

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent."""
        conn = self._get_connection("_nodes")
        cursor = conn.execute(
            "SELECT * FROM nodes WHERE parent_id = ? ORDER BY position",
            (parent_id,)
        )
        return [
            {
                "node_id": row["node_id"],
                "doc_id": row["doc_id"],
                "parent_id": row["parent_id"],
                "level": row["level"],
                "title": row["title"],
                "text": row["text"],
                "summary": row["summary"],
                "page_num": row["page_num"],
                "position": row["position"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
            for row in cursor
        ]

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document."""
        conn = self._get_connection("_nodes")
        cursor = conn.execute("DELETE FROM nodes WHERE doc_id = ?", (doc_id,))
        conn.commit()
        return cursor.rowcount


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


class LakebaseStorage(BaseStorage):
    """
    Databricks Lakebase storage backend (PostgreSQL + pgvector).

    Features:
    - Managed PostgreSQL in Databricks
    - pgvector extension for vector similarity search
    - SSL/TLS encryption
    - Databricks token authentication

    Requires: pip install psycopg2-binary pgvector
    """

    def __init__(self, config: StorageConfig):
        self.config = config
        self._conn = None
        self._lock = threading.RLock()

    def connect(self) -> None:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError:
            raise ImportError("psycopg2 is required. Install with: pip install psycopg2-binary")

        if not self.config.lakebase_host:
            raise ValueError("Lakebase host is required")

        # Build connection string
        conn_params = {
            "host": self.config.lakebase_host,
            "port": self.config.lakebase_port,
            "database": self.config.lakebase_database,
            "cursor_factory": RealDictCursor,
        }

        # Auth: prefer token, fallback to user/password
        if self.config.lakebase_token:
            conn_params["user"] = "token"
            conn_params["password"] = self.config.lakebase_token
        else:
            conn_params["user"] = self.config.lakebase_user
            conn_params["password"] = self.config.lakebase_password

        # SSL for Databricks
        if self.config.lakebase_ssl:
            conn_params["sslmode"] = "require"

        self._conn = psycopg2.connect(**conn_params)
        self._conn.autocommit = False

        # Enable pgvector extension
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self._conn.commit()

        # Create metadata table
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS _vectrix_collections (
                    name TEXT PRIMARY KEY,
                    config JSONB,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_collection_table(self, name: str) -> None:
        """Create collection table if not exists."""
        with self._lock:
            with self._conn.cursor() as cur:
                # Table for documents with JSONB data and vector support
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{name}" (
                        id TEXT PRIMARY KEY,
                        data JSONB NOT NULL,
                        embedding vector,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                # Index for vector similarity search
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS "{name}_embedding_idx"
                    ON "{name}" USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 100)
                """)
                self._conn.commit()

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO _vectrix_collections (name, config, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (name) DO UPDATE SET config = %s, updated_at = NOW()
                """, (name, json.dumps(config), json.dumps(config)))
                self._conn.commit()
            self._ensure_collection_table(name)

    def delete_collection(self, name: str) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM _vectrix_collections WHERE name = %s", (name,))
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
                self._conn.commit()

    def list_collections(self) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM _vectrix_collections")
            return [row["name"] for row in cur.fetchall()]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT config FROM _vectrix_collections WHERE name = %s", (name,))
            row = cur.fetchone()
            return row["config"] if row else None

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        self._ensure_collection_table(collection)

        # Extract embedding if present
        embedding = data.pop("_embedding", None)

        with self._lock:
            with self._conn.cursor() as cur:
                if embedding:
                    cur.execute(f"""
                        INSERT INTO "{collection}" (id, data, embedding, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET data = %s, embedding = %s, updated_at = NOW()
                    """, (id, json.dumps(data), embedding, json.dumps(data), embedding))
                else:
                    cur.execute(f"""
                        INSERT INTO "{collection}" (id, data, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET data = %s, updated_at = NOW()
                    """, (id, json.dumps(data), json.dumps(data)))
                self._conn.commit()

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        self._ensure_collection_table(collection)

        with self._lock:
            with self._conn.cursor() as cur:
                count = 0
                for id, data in documents:
                    embedding = data.pop("_embedding", None)
                    if embedding:
                        cur.execute(f"""
                            INSERT INTO "{collection}" (id, data, embedding, updated_at)
                            VALUES (%s, %s, %s, NOW())
                            ON CONFLICT (id) DO UPDATE SET data = %s, embedding = %s, updated_at = NOW()
                        """, (id, json.dumps(data), embedding, json.dumps(data), embedding))
                    else:
                        cur.execute(f"""
                            INSERT INTO "{collection}" (id, data, updated_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (id) DO UPDATE SET data = %s, updated_at = NOW()
                        """, (id, json.dumps(data), json.dumps(data)))
                    count += 1
                self._conn.commit()
                return count

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        try:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT data FROM "{collection}" WHERE id = %s', (id,))
                row = cur.fetchone()
                return row["data"] if row else None
        except:
            return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        try:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT id, data FROM "{collection}" WHERE id = ANY(%s)', (ids,))
                results = {row["id"]: row["data"] for row in cur.fetchall()}
                return [results.get(id) for id in ids]
        except:
            return [None] * len(ids)

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        existing = self.get(collection, id)
        if existing:
            existing.update(data)
            embedding = existing.pop("_embedding", None)
            with self._lock:
                with self._conn.cursor() as cur:
                    if embedding:
                        cur.execute(f"""
                            UPDATE "{collection}" SET data = %s, embedding = %s, updated_at = NOW()
                            WHERE id = %s
                        """, (json.dumps(existing), embedding, id))
                    else:
                        cur.execute(f"""
                            UPDATE "{collection}" SET data = %s, updated_at = NOW()
                            WHERE id = %s
                        """, (json.dumps(existing), id))
                    self._conn.commit()
                    return cur.rowcount > 0
        return False

    def delete(self, collection: str, id: str) -> bool:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f'DELETE FROM "{collection}" WHERE id = %s', (id,))
                self._conn.commit()
                return cur.rowcount > 0

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f'DELETE FROM "{collection}" WHERE id = ANY(%s)', (ids,))
                self._conn.commit()
                return cur.rowcount

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[callable] = None
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        try:
            with self._conn.cursor() as cur:
                if filter_func:
                    # Fetch all and filter in Python
                    cur.execute(f'SELECT id, data FROM "{collection}" ORDER BY created_at')
                    count = 0
                    skipped = 0
                    for row in cur:
                        if filter_func(row["data"]):
                            if skipped < offset:
                                skipped += 1
                                continue
                            yield (row["id"], row["data"])
                            count += 1
                            if count >= limit:
                                break
                else:
                    cur.execute(
                        f'SELECT id, data FROM "{collection}" ORDER BY created_at LIMIT %s OFFSET %s',
                        (limit, offset)
                    )
                    for row in cur:
                        yield (row["id"], row["data"])
        except:
            return

    def count(self, collection: str) -> int:
        try:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) as cnt FROM "{collection}"')
                row = cur.fetchone()
                return row["cnt"] if row else 0
        except:
            return 0

    def flush(self) -> None:
        if self._conn:
            self._conn.commit()

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10,
        filter_sql: Optional[str] = None
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """
        Perform vector similarity search using pgvector.

        Args:
            collection: Collection name
            query_vector: Query embedding vector
            limit: Max results to return
            filter_sql: Optional SQL WHERE clause for filtering

        Returns:
            List of (id, data, distance) tuples ordered by similarity
        """
        try:
            with self._conn.cursor() as cur:
                where_clause = f"AND {filter_sql}" if filter_sql else ""
                cur.execute(f"""
                    SELECT id, data, embedding <=> %s::vector AS distance
                    FROM "{collection}"
                    WHERE embedding IS NOT NULL {where_clause}
                    ORDER BY distance
                    LIMIT %s
                """, (query_vector, limit))

                return [(row["id"], row["data"], row["distance"]) for row in cur.fetchall()]
        except Exception as e:
            print(f"Vector search error: {e}")
            return []

    # =========================================================================
    # Document Index Methods
    # =========================================================================

    def ensure_document_tables(self) -> None:
        """Create document and node tables if they don't exist."""
        with self._lock:
            with self._conn.cursor() as cur:
                # Documents table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _vectrix_documents (
                        doc_id TEXT PRIMARY KEY,
                        title TEXT,
                        doc_type TEXT,
                        source_path TEXT,
                        etag TEXT,
                        content_hash TEXT,
                        page_count INTEGER DEFAULT 0,
                        section_count INTEGER DEFAULT 0,
                        node_count INTEGER DEFAULT 0,
                        indexed_at TIMESTAMP,
                        last_synced TIMESTAMP,
                        metadata JSONB,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # Nodes table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _vectrix_nodes (
                        node_id TEXT PRIMARY KEY,
                        doc_id TEXT NOT NULL,
                        parent_id TEXT,
                        level INTEGER DEFAULT 1,
                        title TEXT,
                        text TEXT,
                        summary TEXT,
                        page_num INTEGER,
                        position INTEGER DEFAULT 0,
                        metadata JSONB,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # Indexes
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vectrix_docs_type
                    ON _vectrix_documents(doc_type)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vectrix_nodes_doc
                    ON _vectrix_nodes(doc_id)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vectrix_nodes_parent
                    ON _vectrix_nodes(parent_id)
                """)

                self._conn.commit()

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        """Save document metadata."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO _vectrix_documents
                    (doc_id, title, doc_type, source_path, etag, content_hash,
                     page_count, section_count, node_count, indexed_at, last_synced,
                     metadata, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (doc_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        doc_type = EXCLUDED.doc_type,
                        source_path = EXCLUDED.source_path,
                        etag = EXCLUDED.etag,
                        content_hash = EXCLUDED.content_hash,
                        page_count = EXCLUDED.page_count,
                        section_count = EXCLUDED.section_count,
                        node_count = EXCLUDED.node_count,
                        indexed_at = EXCLUDED.indexed_at,
                        last_synced = EXCLUDED.last_synced,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                """, (
                    doc_data["doc_id"],
                    doc_data.get("title", ""),
                    doc_data.get("doc_type", "text"),
                    doc_data.get("source_path"),
                    doc_data.get("etag"),
                    doc_data.get("content_hash"),
                    doc_data.get("page_count", 0),
                    doc_data.get("section_count", 0),
                    doc_data.get("node_count", 0),
                    doc_data.get("indexed_at"),
                    doc_data.get("last_synced"),
                    json.dumps(doc_data.get("metadata", {})),
                ))
                self._conn.commit()

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM _vectrix_documents WHERE doc_id = %s",
                    (doc_id,)
                )
                row = cur.fetchone()
                if row:
                    return {
                        "doc_id": row["doc_id"],
                        "title": row["title"],
                        "doc_type": row["doc_type"],
                        "source_path": row["source_path"],
                        "etag": row["etag"],
                        "content_hash": row["content_hash"],
                        "page_count": row["page_count"],
                        "section_count": row["section_count"],
                        "node_count": row["node_count"],
                        "indexed_at": row["indexed_at"].isoformat() if row["indexed_at"] else None,
                        "last_synced": row["last_synced"].isoformat() if row["last_synced"] else None,
                        "metadata": row["metadata"] or {},
                    }
        except:
            pass
        return None

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM _vectrix_documents ORDER BY indexed_at DESC"
                )
                return [
                    {
                        "doc_id": row["doc_id"],
                        "title": row["title"],
                        "doc_type": row["doc_type"],
                        "source_path": row["source_path"],
                        "etag": row["etag"],
                        "content_hash": row["content_hash"],
                        "page_count": row["page_count"],
                        "section_count": row["section_count"],
                        "node_count": row["node_count"],
                        "indexed_at": row["indexed_at"].isoformat() if row["indexed_at"] else None,
                        "last_synced": row["last_synced"].isoformat() if row["last_synced"] else None,
                        "metadata": row["metadata"] or {},
                    }
                    for row in cur.fetchall()
                ]
        except:
            return []

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _vectrix_documents WHERE doc_id = %s",
                    (doc_id,)
                )
                self._conn.commit()
                return cur.rowcount > 0

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO _vectrix_nodes
                    (node_id, doc_id, parent_id, level, title, text, summary,
                     page_num, position, metadata, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (node_id) DO UPDATE SET
                        doc_id = EXCLUDED.doc_id,
                        parent_id = EXCLUDED.parent_id,
                        level = EXCLUDED.level,
                        title = EXCLUDED.title,
                        text = EXCLUDED.text,
                        summary = EXCLUDED.summary,
                        page_num = EXCLUDED.page_num,
                        position = EXCLUDED.position,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                """, (
                    node_data["node_id"],
                    node_data["doc_id"],
                    node_data.get("parent_id"),
                    node_data.get("level", 1),
                    node_data.get("title", ""),
                    node_data.get("text", ""),
                    node_data.get("summary", ""),
                    node_data.get("page_num"),
                    node_data.get("position", 0),
                    json.dumps(node_data.get("metadata", {})),
                ))
                self._conn.commit()

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM _vectrix_nodes WHERE node_id = %s",
                    (node_id,)
                )
                row = cur.fetchone()
                if row:
                    return {
                        "node_id": row["node_id"],
                        "doc_id": row["doc_id"],
                        "parent_id": row["parent_id"],
                        "level": row["level"],
                        "title": row["title"],
                        "text": row["text"],
                        "summary": row["summary"],
                        "page_num": row["page_num"],
                        "position": row["position"],
                        "metadata": row["metadata"] or {},
                    }
        except:
            pass
        return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM _vectrix_nodes WHERE doc_id = %s ORDER BY position",
                    (doc_id,)
                )
                return [
                    {
                        "node_id": row["node_id"],
                        "doc_id": row["doc_id"],
                        "parent_id": row["parent_id"],
                        "level": row["level"],
                        "title": row["title"],
                        "text": row["text"],
                        "summary": row["summary"],
                        "page_num": row["page_num"],
                        "position": row["position"],
                        "metadata": row["metadata"] or {},
                    }
                    for row in cur.fetchall()
                ]
        except:
            return []

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM _vectrix_nodes WHERE parent_id = %s ORDER BY position",
                    (parent_id,)
                )
                return [
                    {
                        "node_id": row["node_id"],
                        "doc_id": row["doc_id"],
                        "parent_id": row["parent_id"],
                        "level": row["level"],
                        "title": row["title"],
                        "text": row["text"],
                        "summary": row["summary"],
                        "page_num": row["page_num"],
                        "position": row["position"],
                        "metadata": row["metadata"] or {},
                    }
                    for row in cur.fetchall()
                ]
        except:
            return []

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _vectrix_nodes WHERE doc_id = %s",
                    (doc_id,)
                )
                self._conn.commit()
                return cur.rowcount


class DeltaLakeStorage(BaseStorage):
    """
    Databricks Delta Lake storage backend with Unity Catalog.

    Features:
    - Unity Catalog governance (access control, lineage, audit)
    - Delta Lake ACID transactions
    - Time travel (query historical data)
    - Schema enforcement

    Note: Vector search is SLOW (batch scan). Use Lakebase for real-time search.

    Requires: pip install databricks-sql-connector
    """

    def __init__(self, config: StorageConfig):
        self.config = config
        self._conn = None
        self._cursor = None
        self._lock = threading.RLock()
        self._catalog = config.delta_catalog
        self._schema = config.delta_schema

    def _full_table_name(self, table: str) -> str:
        """Get fully qualified table name: catalog.schema.table"""
        return f"`{self._catalog}`.`{self._schema}`.`{table}`"

    def connect(self) -> None:
        try:
            from databricks import sql as databricks_sql
        except ImportError:
            raise ImportError(
                "databricks-sql-connector is required. Install with: pip install databricks-sql-connector"
            )

        if not self.config.delta_workspace_url:
            raise ValueError("Delta Lake workspace_url is required")
        if not self.config.delta_token:
            raise ValueError("Delta Lake token is required")

        # Parse workspace URL
        server_hostname = self.config.delta_workspace_url.replace("https://", "").replace("http://", "").rstrip("/")

        # Build HTTP path
        http_path = self.config.delta_http_path
        if not http_path and self.config.delta_warehouse_id:
            http_path = f"/sql/1.0/warehouses/{self.config.delta_warehouse_id}"
        if not http_path:
            # Try to use serverless or default warehouse
            http_path = "/sql/1.0/warehouses"

        self._conn = databricks_sql.connect(
            server_hostname=server_hostname,
            http_path=http_path,
            access_token=self.config.delta_token,
        )
        self._cursor = self._conn.cursor()

        # Create schema if not exists
        self._cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{self._catalog}`.`{self._schema}`")

        # Create metadata tables
        self._cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._full_table_name('_vectrix_collections')} (
                name STRING NOT NULL,
                config STRING,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            ) USING DELTA
        """)

        self._cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._full_table_name('_vectrix_documents')} (
                doc_id STRING NOT NULL,
                title STRING,
                doc_type STRING,
                source_path STRING,
                etag STRING,
                content_hash STRING,
                page_count INT,
                section_count INT,
                node_count INT,
                indexed_at TIMESTAMP,
                last_synced TIMESTAMP,
                metadata STRING
            ) USING DELTA
        """)

        self._cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._full_table_name('_vectrix_nodes')} (
                node_id STRING NOT NULL,
                doc_id STRING NOT NULL,
                parent_id STRING,
                level INT,
                title STRING,
                text STRING,
                summary STRING,
                page_num INT,
                position INT,
                metadata STRING
            ) USING DELTA
        """)

    def close(self) -> None:
        if self._cursor:
            self._cursor.close()
            self._cursor = None
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_collection_table(self, name: str) -> None:
        """Create collection table if not exists."""
        with self._lock:
            # Table for documents with vector as array
            self._cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._full_table_name(name)} (
                    id STRING NOT NULL,
                    data STRING,
                    embedding ARRAY<DOUBLE>,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                ) USING DELTA
            """)

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        with self._lock:
            now = datetime.now().isoformat()
            self._cursor.execute(f"""
                MERGE INTO {self._full_table_name('_vectrix_collections')} AS target
                USING (SELECT '{name}' AS name) AS source
                ON target.name = source.name
                WHEN MATCHED THEN UPDATE SET config = '{json.dumps(config)}', updated_at = '{now}'
                WHEN NOT MATCHED THEN INSERT (name, config, created_at, updated_at)
                VALUES ('{name}', '{json.dumps(config)}', '{now}', '{now}')
            """)
            self._ensure_collection_table(name)

    def delete_collection(self, name: str) -> None:
        with self._lock:
            self._cursor.execute(f"DELETE FROM {self._full_table_name('_vectrix_collections')} WHERE name = '{name}'")
            self._cursor.execute(f"DROP TABLE IF EXISTS {self._full_table_name(name)}")

    def list_collections(self) -> List[str]:
        with self._lock:
            self._cursor.execute(f"SELECT name FROM {self._full_table_name('_vectrix_collections')}")
            return [row[0] for row in self._cursor.fetchall()]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._cursor.execute(f"SELECT config FROM {self._full_table_name('_vectrix_collections')} WHERE name = '{name}'")
            row = self._cursor.fetchone()
            return json.loads(row[0]) if row else None

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        self._ensure_collection_table(collection)
        embedding = data.pop("_embedding", None)
        now = datetime.now().isoformat()

        with self._lock:
            embedding_str = f"ARRAY({','.join(map(str, embedding))})" if embedding else "NULL"
            self._cursor.execute(f"""
                MERGE INTO {self._full_table_name(collection)} AS target
                USING (SELECT '{id}' AS id) AS source
                ON target.id = source.id
                WHEN MATCHED THEN UPDATE SET data = '{json.dumps(data)}', embedding = {embedding_str}, updated_at = '{now}'
                WHEN NOT MATCHED THEN INSERT (id, data, embedding, created_at, updated_at)
                VALUES ('{id}', '{json.dumps(data)}', {embedding_str}, '{now}', '{now}')
            """)

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        self._ensure_collection_table(collection)
        count = 0
        for id, data in documents:
            self.insert(collection, id, data)
            count += 1
        return count

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT data, embedding FROM {self._full_table_name(collection)} WHERE id = '{id}'")
                row = self._cursor.fetchone()
                if row:
                    data = json.loads(row[0]) if row[0] else {}
                    if row[1]:
                        data["_embedding"] = list(row[1])
                    return data
            except:
                pass
            return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        return [self.get(collection, id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        embedding = data.pop("_embedding", None)
        now = datetime.now().isoformat()

        with self._lock:
            embedding_str = f"ARRAY({','.join(map(str, embedding))})" if embedding else "embedding"
            self._cursor.execute(f"""
                UPDATE {self._full_table_name(collection)}
                SET data = '{json.dumps(data)}', embedding = {embedding_str}, updated_at = '{now}'
                WHERE id = '{id}'
            """)
            return True

    def delete(self, collection: str, id: str) -> bool:
        with self._lock:
            self._cursor.execute(f"DELETE FROM {self._full_table_name(collection)} WHERE id = '{id}'")
            return True

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        count = 0
        for id in ids:
            if self.delete(collection, id):
                count += 1
        return count

    def count(self, collection: str) -> int:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT COUNT(*) FROM {self._full_table_name(collection)}")
                row = self._cursor.fetchone()
                return row[0] if row else 0
            except:
                return 0

    def iterate(self, collection: str, batch_size: int = 1000) -> Iterator[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            offset = 0
            while True:
                self._cursor.execute(f"""
                    SELECT id, data, embedding FROM {self._full_table_name(collection)}
                    LIMIT {batch_size} OFFSET {offset}
                """)
                rows = self._cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    data = json.loads(row[1]) if row[1] else {}
                    if row[2]:
                        data["_embedding"] = list(row[2])
                    yield row[0], data
                offset += batch_size

    def vector_search(self, collection: str, query_vector: List[float], limit: int = 10) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        Vector search using cosine similarity.
        NOTE: This is SLOW in Delta Lake (full table scan). Use Lakebase for fast search.
        """
        # Delta Lake doesn't have native vector search, so we do client-side
        results = []
        for id, data in self.iterate(collection):
            embedding = data.pop("_embedding", None)
            if embedding:
                # Cosine similarity
                dot = sum(a * b for a, b in zip(query_vector, embedding))
                norm_q = sum(a * a for a in query_vector) ** 0.5
                norm_e = sum(a * a for a in embedding) ** 0.5
                if norm_q > 0 and norm_e > 0:
                    similarity = dot / (norm_q * norm_e)
                    results.append((id, similarity, data))

        # Sort by similarity descending
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    # Document Index methods
    def ensure_document_tables(self) -> None:
        """Tables already created in connect()."""
        pass

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        with self._lock:
            now = datetime.now().isoformat()
            doc_id = doc_data.get("doc_id", "")
            self._cursor.execute(f"""
                MERGE INTO {self._full_table_name('_vectrix_documents')} AS target
                USING (SELECT '{doc_id}' AS doc_id) AS source
                ON target.doc_id = source.doc_id
                WHEN MATCHED THEN UPDATE SET
                    title = '{doc_data.get("title", "")}',
                    doc_type = '{doc_data.get("doc_type", "")}',
                    page_count = {doc_data.get("page_count", 0)},
                    section_count = {doc_data.get("section_count", 0)},
                    node_count = {doc_data.get("node_count", 0)},
                    indexed_at = '{now}',
                    metadata = '{json.dumps(doc_data.get("metadata", {}))}'
                WHEN NOT MATCHED THEN INSERT (doc_id, title, doc_type, page_count, section_count, node_count, indexed_at, metadata)
                VALUES ('{doc_id}', '{doc_data.get("title", "")}', '{doc_data.get("doc_type", "")}',
                        {doc_data.get("page_count", 0)}, {doc_data.get("section_count", 0)},
                        {doc_data.get("node_count", 0)}, '{now}', '{json.dumps(doc_data.get("metadata", {}))}')
            """)

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT * FROM {self._full_table_name('_vectrix_documents')} WHERE doc_id = '{doc_id}'")
                row = self._cursor.fetchone()
                if row:
                    cols = [desc[0] for desc in self._cursor.description]
                    return dict(zip(cols, row))
            except:
                pass
            return None

    def list_documents(self) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT * FROM {self._full_table_name('_vectrix_documents')}")
                cols = [desc[0] for desc in self._cursor.description]
                return [dict(zip(cols, row)) for row in self._cursor.fetchall()]
            except:
                return []

    def delete_document(self, doc_id: str) -> bool:
        with self._lock:
            self._cursor.execute(f"DELETE FROM {self._full_table_name('_vectrix_documents')} WHERE doc_id = '{doc_id}'")
            return True

    def save_node(self, node_data: Dict[str, Any]) -> None:
        with self._lock:
            node_id = node_data.get("node_id", "")
            self._cursor.execute(f"""
                MERGE INTO {self._full_table_name('_vectrix_nodes')} AS target
                USING (SELECT '{node_id}' AS node_id) AS source
                ON target.node_id = source.node_id
                WHEN MATCHED THEN UPDATE SET
                    doc_id = '{node_data.get("doc_id", "")}',
                    parent_id = '{node_data.get("parent_id", "") or ""}',
                    level = {node_data.get("level", 0)},
                    title = '{node_data.get("title", "").replace("'", "''")}',
                    text = '{node_data.get("text", "").replace("'", "''")}',
                    page_num = {node_data.get("page_num") or "NULL"},
                    position = {node_data.get("position") or 0},
                    metadata = '{json.dumps(node_data.get("metadata", {}))}'
                WHEN NOT MATCHED THEN INSERT (node_id, doc_id, parent_id, level, title, text, page_num, position, metadata)
                VALUES ('{node_id}', '{node_data.get("doc_id", "")}', '{node_data.get("parent_id", "") or ""}',
                        {node_data.get("level", 0)}, '{node_data.get("title", "").replace("'", "''")}',
                        '{node_data.get("text", "").replace("'", "''")}', {node_data.get("page_num") or "NULL"},
                        {node_data.get("position") or 0}, '{json.dumps(node_data.get("metadata", {}))}')
            """)

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT * FROM {self._full_table_name('_vectrix_nodes')} WHERE node_id = '{node_id}'")
                row = self._cursor.fetchone()
                if row:
                    cols = [desc[0] for desc in self._cursor.description]
                    return dict(zip(cols, row))
            except:
                pass
            return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT * FROM {self._full_table_name('_vectrix_nodes')} WHERE doc_id = '{doc_id}' ORDER BY position")
                cols = [desc[0] for desc in self._cursor.description]
                return [dict(zip(cols, row)) for row in self._cursor.fetchall()]
            except:
                return []

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                self._cursor.execute(f"SELECT * FROM {self._full_table_name('_vectrix_nodes')} WHERE parent_id = '{parent_id}' ORDER BY position")
                cols = [desc[0] for desc in self._cursor.description]
                return [dict(zip(cols, row)) for row in self._cursor.fetchall()]
            except:
                return []

    def delete_document_nodes(self, doc_id: str) -> int:
        with self._lock:
            self._cursor.execute(f"DELETE FROM {self._full_table_name('_vectrix_nodes')} WHERE doc_id = '{doc_id}'")
            return 0  # Delta Lake doesn't return row count easily


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
    elif config.backend == StorageBackend.LAKEBASE:
        storage = LakebaseStorage(config)
        storage.connect()
        return storage
    elif config.backend == StorageBackend.DELTA_LAKE:
        storage = DeltaLakeStorage(config)
        storage.connect()
        return storage
    else:
        raise ValueError(f"Unknown storage backend: {config.backend}")
