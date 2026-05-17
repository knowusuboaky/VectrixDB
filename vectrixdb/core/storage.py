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
    In-memory storage backend with adaptive schema support.

    Fastest option, no persistence. Use for:
    - Testing
    - Temporary collections
    - As a cache layer

    Supports adaptive schema based on mode:
    - dense: dense_embedding only
    - hybrid: dense_embedding + sparse_embedding
    - ultimate/graph: + late_interaction_embedding
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
        """Insert with support for multi-embedding storage."""
        with self._lock:
            if collection in self._collections:
                # Normalize embedding field names
                if "_embedding" in data:
                    data["dense_embedding"] = data.pop("_embedding")
                self._collections[collection][id] = data

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        with self._lock:
            if collection not in self._collections:
                return 0
            count = 0
            for id, data in documents:
                # Normalize embedding field names
                if "_embedding" in data:
                    data["dense_embedding"] = data.pop("_embedding")
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

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Dense vector search using cosine similarity."""
        import math
        results = []
        with self._lock:
            if collection not in self._collections:
                return []
            for id_, data in self._collections[collection].items():
                embedding = data.get("dense_embedding") or data.get("_embedding")
                if embedding:
                    # Cosine similarity
                    dot = sum(a * b for a, b in zip(query_vector, embedding))
                    norm_q = math.sqrt(sum(a * a for a in query_vector))
                    norm_e = math.sqrt(sum(a * a for a in embedding))
                    if norm_q > 0 and norm_e > 0:
                        similarity = dot / (norm_q * norm_e)
                        distance = 1 - similarity
                        result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                        results.append((id_, result_data, distance))
        results.sort(key=lambda x: x[2])
        return results[:limit]

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Hybrid search using dense + sparse with RRF fusion."""
        import math
        prefetch = limit * 10

        # Dense search
        dense_results = self.vector_search(collection, dense_query, prefetch)

        # Sparse search
        sparse_results = []
        with self._lock:
            if collection in self._collections:
                for id_, data in self._collections[collection].items():
                    sparse_emb = data.get("sparse_embedding")
                    if sparse_emb:
                        score = sum(sparse_query.get(int(k), 0) * v for k, v in sparse_emb.items())
                        if score > 0:
                            result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                            sparse_results.append((id_, result_data, score))
        sparse_results.sort(key=lambda x: x[2], reverse=True)
        sparse_results = sparse_results[:prefetch]

        # RRF Fusion
        rrf_k = 60
        scores = {}
        data_map = {}

        for rank, (id_, data, _) in enumerate(dense_results):
            scores[id_] = {"dense": 1.0 / (rrf_k + rank + 1), "sparse": 0}
            data_map[id_] = data

        for rank, (id_, data, _) in enumerate(sparse_results):
            if id_ not in scores:
                scores[id_] = {"dense": 0, "sparse": 0}
                data_map[id_] = data
            scores[id_]["sparse"] = 1.0 / (rrf_k + rank + 1)

        # Combined scores with intersection boost
        results = []
        for id_, s in scores.items():
            combined = 0.5 * s["dense"] + 0.5 * s["sparse"]
            if s["dense"] > 0 and s["sparse"] > 0:
                combined *= 1.15  # 15% boost for appearing in both
            results.append((id_, data_map[id_], combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

    def ultimate_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        late_interaction_query: List[List[float]],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Ultimate search using dense + sparse + late interaction (ColBERT)."""
        import numpy as np

        # Get hybrid results first
        hybrid_results = self.hybrid_search(collection, dense_query, sparse_query, limit * 3)

        if not hybrid_results:
            return []

        # Score with ColBERT MaxSim
        results = []
        query_emb = np.array(late_interaction_query)

        with self._lock:
            for id_, data, hybrid_score in hybrid_results:
                doc_data = self._collections.get(collection, {}).get(id_, {})
                late_interaction_emb = doc_data.get("late_interaction_embedding")

                if late_interaction_emb:
                    doc_emb = np.array(late_interaction_emb)
                    # MaxSim scoring
                    sim = np.dot(query_emb, doc_emb.T)
                    max_sim = np.max(sim, axis=1)
                    colbert_score = float(np.sum(max_sim))

                    # Combine hybrid + colbert scores
                    max_colbert = max(colbert_score, 1e-6)
                    combined = 0.6 * hybrid_score + 0.4 * (colbert_score / max_colbert)
                else:
                    combined = hybrid_score

                results.append((id_, data, combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

    # =========================================================================
    # Document Index Methods (for Graph mode)
    # =========================================================================

    def ensure_document_tables(self) -> None:
        """Create document and node storage for graph mode."""
        with self._lock:
            if "_documents" not in self._collections:
                self._collections["_documents"] = {}
            if "_nodes" not in self._collections:
                self._collections["_nodes"] = {}

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        """Save document metadata."""
        self.ensure_document_tables()
        doc_id = doc_data.get("doc_id")
        if doc_id:
            with self._lock:
                self._collections["_documents"][doc_id] = {
                    **doc_data,
                    "updated_at": datetime.utcnow().isoformat()
                }

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        with self._lock:
            return self._collections.get("_documents", {}).get(doc_id)

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents."""
        with self._lock:
            return list(self._collections.get("_documents", {}).values())

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document."""
        with self._lock:
            if "_documents" in self._collections:
                return self._collections["_documents"].pop(doc_id, None) is not None
            return False

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""
        self.ensure_document_tables()
        node_id = node_data.get("node_id")
        if node_id:
            with self._lock:
                self._collections["_nodes"][node_id] = {
                    **node_data,
                    "updated_at": datetime.utcnow().isoformat()
                }

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""
        with self._lock:
            return self._collections.get("_nodes", {}).get(node_id)

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document."""
        with self._lock:
            nodes = self._collections.get("_nodes", {})
            return [n for n in nodes.values() if n.get("doc_id") == doc_id]

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent."""
        with self._lock:
            nodes = self._collections.get("_nodes", {})
            return [n for n in nodes.values() if n.get("parent_id") == parent_id]

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document."""
        with self._lock:
            nodes = self._collections.get("_nodes", {})
            to_delete = [nid for nid, n in nodes.items() if n.get("doc_id") == doc_id]
            for nid in to_delete:
                del nodes[nid]
            return len(to_delete)


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
    # Vector Search Methods (Adaptive Schema Support)
    # =========================================================================

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Dense vector search using cosine similarity."""
        import math
        results = []
        for id_, data in self.scan(collection, limit=10000):
            embedding = data.get("dense_embedding") or data.get("_embedding")
            if embedding:
                # Cosine similarity
                dot = sum(a * b for a, b in zip(query_vector, embedding))
                norm_q = math.sqrt(sum(a * a for a in query_vector))
                norm_e = math.sqrt(sum(a * a for a in embedding))
                if norm_q > 0 and norm_e > 0:
                    similarity = dot / (norm_q * norm_e)
                    distance = 1 - similarity
                    result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                    results.append((id_, result_data, distance))
        results.sort(key=lambda x: x[2])
        return results[:limit]

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Hybrid search using dense + sparse with RRF fusion."""
        prefetch = limit * 10

        # Dense search
        dense_results = self.vector_search(collection, dense_query, prefetch)

        # Sparse search
        sparse_results = []
        for id_, data in self.scan(collection, limit=10000):
            sparse_emb = data.get("sparse_embedding")
            if sparse_emb:
                score = sum(sparse_query.get(int(k), 0) * v for k, v in sparse_emb.items())
                if score > 0:
                    result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                    sparse_results.append((id_, result_data, score))
        sparse_results.sort(key=lambda x: x[2], reverse=True)
        sparse_results = sparse_results[:prefetch]

        # RRF Fusion
        rrf_k = 60
        scores = {}
        data_map = {}

        for rank, (id_, data, _) in enumerate(dense_results):
            scores[id_] = {"dense": 1.0 / (rrf_k + rank + 1), "sparse": 0}
            data_map[id_] = data

        for rank, (id_, data, _) in enumerate(sparse_results):
            if id_ not in scores:
                scores[id_] = {"dense": 0, "sparse": 0}
                data_map[id_] = data
            scores[id_]["sparse"] = 1.0 / (rrf_k + rank + 1)

        # Combined scores with intersection boost
        results = []
        for id_, s in scores.items():
            combined = 0.5 * s["dense"] + 0.5 * s["sparse"]
            if s["dense"] > 0 and s["sparse"] > 0:
                combined *= 1.15  # 15% boost for appearing in both
            results.append((id_, data_map[id_], combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

    def ultimate_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        late_interaction_query: List[List[float]],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Ultimate search using dense + sparse + late interaction (ColBERT)."""
        import numpy as np

        # Get hybrid results first
        hybrid_results = self.hybrid_search(collection, dense_query, sparse_query, limit * 3)

        if not hybrid_results:
            return []

        # Score with ColBERT MaxSim
        results = []
        query_emb = np.array(late_interaction_query)

        for id_, data, hybrid_score in hybrid_results:
            full_data = self.get(collection, id_)
            late_interaction_emb = full_data.get("late_interaction_embedding") if full_data else None

            if late_interaction_emb:
                doc_emb = np.array(late_interaction_emb)
                # MaxSim scoring
                sim = np.dot(query_emb, doc_emb.T)
                max_sim = np.max(sim, axis=1)
                colbert_score = float(np.sum(max_sim))

                # Combine hybrid + colbert scores
                max_colbert = max(colbert_score, 1e-6)
                combined = 0.6 * hybrid_score + 0.4 * (colbert_score / max_colbert)
            else:
                combined = hybrid_score

            results.append((id_, data, combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

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

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """
        Vector search using cosine similarity.
        NOTE: Cosmos DB doesn't have native vector search - this does client-side search.
        For production, use Lakebase (pgvector) for fast vector search.
        """
        import math
        results = []
        for id_, data in self.scan(collection, limit=10000):  # Scan up to 10k documents
            embedding = data.get("dense_embedding") or data.get("_embedding")
            if embedding:
                # Cosine similarity
                dot = sum(a * b for a, b in zip(query_vector, embedding))
                norm_q = math.sqrt(sum(a * a for a in query_vector))
                norm_e = math.sqrt(sum(a * a for a in embedding))
                if norm_q > 0 and norm_e > 0:
                    similarity = dot / (norm_q * norm_e)
                    distance = 1 - similarity
                    result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                    results.append((id_, result_data, distance))

        results.sort(key=lambda x: x[2])
        return results[:limit]

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Hybrid search using dense + sparse with RRF fusion."""
        prefetch = limit * 10

        # Dense search
        dense_results = self.vector_search(collection, dense_query, prefetch)

        # Sparse search
        sparse_results = []
        for id_, data in self.scan(collection, limit=10000):
            sparse_emb = data.get("sparse_embedding")
            if sparse_emb:
                score = sum(sparse_query.get(int(k), 0) * v for k, v in sparse_emb.items())
                if score > 0:
                    result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                    sparse_results.append((id_, result_data, score))
        sparse_results.sort(key=lambda x: x[2], reverse=True)
        sparse_results = sparse_results[:prefetch]

        # RRF Fusion
        rrf_k = 60
        scores = {}
        data_map = {}

        for rank, (id_, data, _) in enumerate(dense_results):
            scores[id_] = {"dense": 1.0 / (rrf_k + rank + 1), "sparse": 0}
            data_map[id_] = data

        for rank, (id_, data, _) in enumerate(sparse_results):
            if id_ not in scores:
                scores[id_] = {"dense": 0, "sparse": 0}
                data_map[id_] = data
            scores[id_]["sparse"] = 1.0 / (rrf_k + rank + 1)

        # Combined scores with intersection boost
        results = []
        for id_, s in scores.items():
            combined = 0.5 * s["dense"] + 0.5 * s["sparse"]
            if s["dense"] > 0 and s["sparse"] > 0:
                combined *= 1.15
            results.append((id_, data_map[id_], combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

    def ultimate_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        late_interaction_query: List[List[float]],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Ultimate search using dense + sparse + late interaction (ColBERT)."""
        import numpy as np

        # Get hybrid results first
        hybrid_results = self.hybrid_search(collection, dense_query, sparse_query, limit * 3)

        if not hybrid_results:
            return []

        # Score with ColBERT MaxSim
        results = []
        query_emb = np.array(late_interaction_query)

        for id_, data, hybrid_score in hybrid_results:
            full_data = self.get(collection, id_)
            late_interaction_emb = full_data.get("late_interaction_embedding") if full_data else None

            if late_interaction_emb:
                doc_emb = np.array(late_interaction_emb)
                sim = np.dot(query_emb, doc_emb.T)
                max_sim = np.max(sim, axis=1)
                colbert_score = float(np.sum(max_sim))

                max_colbert = max(colbert_score, 1e-6)
                combined = 0.6 * hybrid_score + 0.4 * (colbert_score / max_colbert)
            else:
                combined = hybrid_score

            results.append((id_, data, combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

    # =========================================================================
    # Document Index Methods (for Graph mode)
    # =========================================================================

    def ensure_document_tables(self) -> None:
        """Create document and node containers for graph mode."""
        from azure.cosmos import PartitionKey
        from azure.cosmos.exceptions import CosmosResourceExistsError

        # Documents container
        try:
            self._containers["_documents"] = self._database.create_container(
                id="_documents",
                partition_key=PartitionKey(path="/doc_type"),
                offer_throughput=self.config.cosmos_throughput
            )
        except CosmosResourceExistsError:
            self._containers["_documents"] = self._database.get_container_client("_documents")

        # Nodes container
        try:
            self._containers["_nodes"] = self._database.create_container(
                id="_nodes",
                partition_key=PartitionKey(path="/doc_id"),
                offer_throughput=self.config.cosmos_throughput
            )
        except CosmosResourceExistsError:
            self._containers["_nodes"] = self._database.get_container_client("_nodes")

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        """Save document metadata."""
        self.ensure_document_tables()
        container = self._get_container("_documents")
        item = {
            "id": doc_data.get("doc_id"),
            "doc_type": doc_data.get("doc_type", "text"),
            **doc_data,
            "updated_at": datetime.utcnow().isoformat()
        }
        container.upsert_item(item)

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        try:
            container = self._get_container("_documents")
            # Query across partitions since we don't know doc_type
            query = f"SELECT * FROM c WHERE c.id = '{doc_id}'"
            items = list(container.query_items(query, enable_cross_partition_query=True))
            if items:
                return {k: v for k, v in items[0].items() if not k.startswith("_")}
        except:
            pass
        return None

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents."""
        try:
            container = self._get_container("_documents")
            query = "SELECT * FROM c"
            items = container.query_items(query, enable_cross_partition_query=True)
            return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]
        except:
            return []

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document."""
        try:
            doc = self.get_document(doc_id)
            if doc:
                container = self._get_container("_documents")
                container.delete_item(item=doc_id, partition_key=doc.get("doc_type", "text"))
                return True
        except:
            pass
        return False

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""
        self.ensure_document_tables()
        container = self._get_container("_nodes")
        item = {
            "id": node_data.get("node_id"),
            "doc_id": node_data.get("doc_id"),
            **node_data,
            "updated_at": datetime.utcnow().isoformat()
        }
        container.upsert_item(item)

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""
        try:
            container = self._get_container("_nodes")
            query = f"SELECT * FROM c WHERE c.id = '{node_id}'"
            items = list(container.query_items(query, enable_cross_partition_query=True))
            if items:
                return {k: v for k, v in items[0].items() if not k.startswith("_")}
        except:
            pass
        return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document."""
        try:
            container = self._get_container("_nodes")
            query = f"SELECT * FROM c WHERE c.doc_id = '{doc_id}' ORDER BY c.position"
            items = container.query_items(query, partition_key=doc_id)
            return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]
        except:
            return []

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent."""
        try:
            container = self._get_container("_nodes")
            query = f"SELECT * FROM c WHERE c.parent_id = '{parent_id}' ORDER BY c.position"
            items = container.query_items(query, enable_cross_partition_query=True)
            return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]
        except:
            return []

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document."""
        try:
            container = self._get_container("_nodes")
            nodes = self.get_document_nodes(doc_id)
            for node in nodes:
                container.delete_item(item=node["node_id"], partition_key=doc_id)
            return len(nodes)
        except:
            return 0


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

        # Create metadata table with proper schema qualification
        schema = self.config.lakebase_schema or "public"
        collections_table = f'"{schema}"._vectrix_collections'
        with self._conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {collections_table} (
                    name TEXT PRIMARY KEY,
                    dimension INTEGER,
                    description TEXT,
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

    def _ensure_collection_table(self, name: str, dimension: int = None, mode: str = "dense") -> None:
        """Create collection table with adaptive schema based on mode.

        Schema adapts based on mode:
        - dense: dense_embedding only
        - hybrid: dense_embedding + sparse_embedding
        - ultimate/graph: dense_embedding + sparse_embedding + late_interaction_embedding
        """
        with self._lock:
            with self._conn.cursor() as cur:
                # Get schema for all table operations
                schema = self.config.lakebase_schema or "public"
                table_ref = f'"{schema}"."{name}"'

                # Get config from collection if not provided
                if dimension is None:
                    collections_table = self._collections_table_ref()
                    cur.execute(f"SELECT config FROM {collections_table} WHERE name = %s", (name,))
                    row = cur.fetchone()
                    if row and row["config"]:
                        dimension = row["config"].get("dimension", 384)
                        mode = row["config"].get("mode", "dense")
                    else:
                        dimension = 384  # Default dimension

                # Check if table exists and has correct schema
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                """, (schema, name,))
                existing_columns = {row["column_name"] for row in cur.fetchall()}

                # If table exists but missing dense_embedding, drop and recreate
                if existing_columns and "dense_embedding" not in existing_columns:
                    cur.execute(f'DROP TABLE IF EXISTS {table_ref} CASCADE')
                    self._conn.commit()
                    existing_columns = set()

                # Create table if not exists (with explicit schema)
                if not existing_columns:
                    # Ensure schema exists
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                    cur.execute(f"""
                        CREATE TABLE {table_ref} (
                            id TEXT PRIMARY KEY,
                            text_content TEXT,
                            metadata JSONB,
                            dense_embedding vector({dimension}),
                            sparse_embedding JSONB,
                            late_interaction_embedding JSONB,
                            created_at TIMESTAMP DEFAULT NOW(),
                            updated_at TIMESTAMP DEFAULT NOW()
                        )
                    """)
                    self._conn.commit()
                else:
                    # Add missing columns to existing table
                    if "dense_embedding" not in existing_columns:
                        cur.execute(f'ALTER TABLE {table_ref} ADD COLUMN dense_embedding vector({dimension})')
                    if "sparse_embedding" not in existing_columns:
                        cur.execute(f'ALTER TABLE {table_ref} ADD COLUMN sparse_embedding JSONB')
                    if "late_interaction_embedding" not in existing_columns:
                        cur.execute(f'ALTER TABLE {table_ref} ADD COLUMN late_interaction_embedding JSONB')
                    if "metadata" not in existing_columns:
                        cur.execute(f'ALTER TABLE {table_ref} ADD COLUMN metadata JSONB')
                    if "text_content" not in existing_columns:
                        cur.execute(f'ALTER TABLE {table_ref} ADD COLUMN text_content TEXT')
                    self._conn.commit()

                # Create indexes (use HNSW instead of IVFFlat - doesn't require data)
                try:
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS "{name}_dense_idx"
                        ON {table_ref} USING hnsw (dense_embedding vector_cosine_ops)
                    """)
                except Exception:
                    pass  # Index may already exist or pgvector not configured for HNSW

                # JSONB index for metadata filtering
                try:
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS "{name}_metadata_idx"
                        ON {table_ref} USING GIN (metadata)
                    """)
                except Exception:
                    pass  # Index may already exist

                # Sparse embedding index for hybrid search
                try:
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS "{name}_sparse_idx"
                        ON {table_ref} USING GIN (sparse_embedding)
                    """)
                except Exception:
                    pass  # Index may already exist

                self._conn.commit()

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        dimension = config.get("dimension", 384)
        description = config.get("description", "")
        mode = config.get("mode", "dense")

        with self._lock:
            collections_table = self._collections_table_ref()
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {collections_table} (name, dimension, description, config, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (name) DO UPDATE SET
                        dimension = %s,
                        description = %s,
                        config = %s,
                        updated_at = NOW()
                """, (name, dimension, description, json.dumps(config),
                      dimension, description, json.dumps(config)))
                self._conn.commit()
            self._ensure_collection_table(name, dimension=dimension, mode=mode)

    def delete_collection(self, name: str) -> None:
        schema = self.config.lakebase_schema or "public"
        table_ref = f'"{schema}"."{name}"'
        collections_table = self._collections_table_ref()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {collections_table} WHERE name = %s", (name,))
                cur.execute(f'DROP TABLE IF EXISTS {table_ref}')
                self._conn.commit()

    def list_collections(self) -> List[str]:
        collections_table = self._collections_table_ref()
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT name FROM {collections_table}")
            return [row["name"] for row in cur.fetchall()]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        collections_table = self._collections_table_ref()
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT config FROM {collections_table} WHERE name = %s", (name,))
            row = cur.fetchone()
            return row["config"] if row else None

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        self._ensure_collection_table(collection)

        # Extract special fields
        dense_embedding = data.pop("_embedding", None) or data.pop("dense_embedding", None)
        sparse_embedding = data.pop("sparse_embedding", None)
        late_interaction_embedding = data.pop("late_interaction_embedding", None)
        text_content = data.pop("text_content", None)
        metadata = data  # Remaining fields are metadata

        # Convert embeddings to proper format
        dense_str = f"[{','.join(map(str, dense_embedding))}]" if dense_embedding else None
        sparse_json = json.dumps(sparse_embedding) if sparse_embedding else None
        late_interaction_json = json.dumps([e.tolist() if hasattr(e, 'tolist') else e for e in late_interaction_embedding]) if late_interaction_embedding else None

        with self._lock:
            with self._conn.cursor() as cur:
                # Check which columns exist
                schema = self.config.lakebase_schema or "public"
                table_ref = f'"{schema}"."{collection}"'
                cur.execute(f"""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                """, (schema, collection,))
                columns = {row["column_name"] for row in cur.fetchall()}

                # Build dynamic INSERT based on available columns
                if "sparse_embedding" in columns and "late_interaction_embedding" in columns:
                    cur.execute(f"""
                        INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, late_interaction_embedding, metadata, text_content, updated_at)
                        VALUES (%s, %s::vector, %s::jsonb, %s::jsonb, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),
                            sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),
                            late_interaction_embedding = COALESCE(%s::jsonb, {table_ref}.late_interaction_embedding),
                            metadata = %s,
                            text_content = COALESCE(%s, {table_ref}.text_content),
                            updated_at = NOW()
                    """, (id, dense_str, sparse_json, late_interaction_json, json.dumps(metadata), text_content,
                          dense_str, sparse_json, late_interaction_json, json.dumps(metadata), text_content))
                elif "sparse_embedding" in columns:
                    cur.execute(f"""
                        INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, metadata, text_content, updated_at)
                        VALUES (%s, %s::vector, %s::jsonb, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),
                            sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),
                            metadata = %s,
                            text_content = COALESCE(%s, {table_ref}.text_content),
                            updated_at = NOW()
                    """, (id, dense_str, sparse_json, json.dumps(metadata), text_content,
                          dense_str, sparse_json, json.dumps(metadata), text_content))
                else:
                    cur.execute(f"""
                        INSERT INTO {table_ref} (id, dense_embedding, metadata, text_content, updated_at)
                        VALUES (%s, %s::vector, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),
                            metadata = %s,
                            text_content = COALESCE(%s, {table_ref}.text_content),
                            updated_at = NOW()
                    """, (id, dense_str, json.dumps(metadata), text_content,
                          dense_str, json.dumps(metadata), text_content))
                self._conn.commit()

    def _table_ref(self, name: str) -> str:
        """Get schema-qualified table reference."""
        schema = self.config.lakebase_schema or "public"
        return f'"{schema}"."{name}"'

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        self._ensure_collection_table(collection)
        table_ref = self._table_ref(collection)

        with self._lock:
            with self._conn.cursor() as cur:
                # Check which columns exist
                schema = self.config.lakebase_schema or "public"
                cur.execute(f"""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                """, (schema, collection,))
                columns = {row["column_name"] for row in cur.fetchall()}
                has_sparse = "sparse_embedding" in columns
                has_late_interaction = "late_interaction_embedding" in columns

                count = 0
                for id, data in documents:
                    # Extract special fields
                    dense_embedding = data.pop("_embedding", None) or data.pop("dense_embedding", None)
                    sparse_embedding = data.pop("sparse_embedding", None)
                    late_interaction_embedding = data.pop("late_interaction_embedding", None)
                    text_content = data.pop("text_content", None)
                    metadata = data  # Remaining fields are metadata

                    # Convert embeddings to proper format
                    dense_str = f"[{','.join(map(str, dense_embedding))}]" if dense_embedding else None
                    sparse_json = json.dumps(sparse_embedding) if sparse_embedding else None
                    late_interaction_json = json.dumps([e.tolist() if hasattr(e, 'tolist') else e for e in late_interaction_embedding]) if late_interaction_embedding else None

                    if has_sparse and has_late_interaction:
                        cur.execute(f"""
                            INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, late_interaction_embedding, metadata, text_content, updated_at)
                            VALUES (%s, %s::vector, %s::jsonb, %s::jsonb, %s, %s, NOW())
                            ON CONFLICT (id) DO UPDATE SET
                                dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),
                                sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),
                                late_interaction_embedding = COALESCE(%s::jsonb, {table_ref}.late_interaction_embedding),
                                metadata = %s,
                                text_content = COALESCE(%s, {table_ref}.text_content),
                                updated_at = NOW()
                        """, (id, dense_str, sparse_json, late_interaction_json, json.dumps(metadata), text_content,
                              dense_str, sparse_json, late_interaction_json, json.dumps(metadata), text_content))
                    elif has_sparse:
                        cur.execute(f"""
                            INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, metadata, text_content, updated_at)
                            VALUES (%s, %s::vector, %s::jsonb, %s, %s, NOW())
                            ON CONFLICT (id) DO UPDATE SET
                                dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),
                                sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),
                                metadata = %s,
                                text_content = COALESCE(%s, {table_ref}.text_content),
                                updated_at = NOW()
                        """, (id, dense_str, sparse_json, json.dumps(metadata), text_content,
                              dense_str, sparse_json, json.dumps(metadata), text_content))
                    else:
                        cur.execute(f"""
                            INSERT INTO {table_ref} (id, dense_embedding, metadata, text_content, updated_at)
                            VALUES (%s, %s::vector, %s, %s, NOW())
                            ON CONFLICT (id) DO UPDATE SET
                                dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),
                                metadata = %s,
                                text_content = COALESCE(%s, {table_ref}.text_content),
                                updated_at = NOW()
                        """, (id, dense_str, json.dumps(metadata), text_content,
                              dense_str, json.dumps(metadata), text_content))
                    count += 1
                self._conn.commit()
                return count

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        table_ref = self._table_ref(collection)
        try:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT metadata, text_content FROM {table_ref} WHERE id = %s', (id,))
                row = cur.fetchone()
                if row:
                    result = row["metadata"] or {}
                    if row["text_content"]:
                        result["text_content"] = row["text_content"]
                    return result
                return None
        except:
            return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        table_ref = self._table_ref(collection)
        try:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT id, metadata, text_content FROM {table_ref} WHERE id = ANY(%s)', (ids,))
                results = {}
                for row in cur.fetchall():
                    data = row["metadata"] or {}
                    if row["text_content"]:
                        data["text_content"] = row["text_content"]
                    results[row["id"]] = data
                return [results.get(id) for id in ids]
        except:
            return [None] * len(ids)

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        table_ref = self._table_ref(collection)
        existing = self.get(collection, id)
        if existing:
            existing.update(data)
            embedding = existing.pop("_embedding", None)
            text_content = existing.pop("text_content", None)
            metadata = existing

            with self._lock:
                with self._conn.cursor() as cur:
                    cur.execute(f"""
                        UPDATE {table_ref} SET
                            metadata = %s,
                            embedding = COALESCE(%s::vector, embedding),
                            text_content = COALESCE(%s, text_content),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (json.dumps(metadata), embedding, text_content, id))
                    self._conn.commit()
                    return cur.rowcount > 0
        return False

    def delete(self, collection: str, id: str) -> bool:
        table_ref = self._table_ref(collection)
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f'DELETE FROM {table_ref} WHERE id = %s', (id,))
                self._conn.commit()
                return cur.rowcount > 0

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        table_ref = self._table_ref(collection)
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f'DELETE FROM {table_ref} WHERE id = ANY(%s)', (ids,))
                self._conn.commit()
                return cur.rowcount

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[callable] = None
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        table_ref = self._table_ref(collection)
        try:
            with self._conn.cursor() as cur:
                if filter_func:
                    # Fetch all and filter in Python
                    cur.execute(f'SELECT id, metadata, text_content FROM {table_ref} ORDER BY created_at')
                    count = 0
                    skipped = 0
                    for row in cur:
                        data = row["metadata"] or {}
                        if row["text_content"]:
                            data["text_content"] = row["text_content"]
                        if filter_func(data):
                            if skipped < offset:
                                skipped += 1
                                continue
                            yield (row["id"], data)
                            count += 1
                            if count >= limit:
                                break
                else:
                    cur.execute(
                        f'SELECT id, metadata, text_content FROM {table_ref} ORDER BY created_at LIMIT %s OFFSET %s',
                        (limit, offset)
                    )
                    for row in cur:
                        data = row["metadata"] or {}
                        if row["text_content"]:
                            data["text_content"] = row["text_content"]
                        yield (row["id"], data)
        except:
            return

    def count(self, collection: str) -> int:
        table_ref = self._table_ref(collection)
        try:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) as cnt FROM {table_ref}')
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
        Perform vector similarity search using pgvector (dense only).

        Args:
            collection: Collection name
            query_vector: Query embedding vector
            limit: Max results to return
            filter_sql: Optional SQL WHERE clause for metadata filtering

        Returns:
            List of (id, metadata, distance) tuples ordered by similarity
        """
        table_ref = self._table_ref(collection)
        try:
            with self._conn.cursor() as cur:
                where_clause = f"AND {filter_sql}" if filter_sql else ""
                cur.execute(f"""
                    SELECT id, metadata, text_content, dense_embedding <=> %s::vector AS distance
                    FROM {table_ref}
                    WHERE dense_embedding IS NOT NULL {where_clause}
                    ORDER BY distance
                    LIMIT %s
                """, (query_vector, limit))

                results = []
                for row in cur.fetchall():
                    data = row["metadata"] or {}
                    if row["text_content"]:
                        data["text_content"] = row["text_content"]
                    results.append((row["id"], data, row["distance"]))
                return results
        except Exception as e:
            print(f"Vector search error: {e}")
            return []

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10,
        filter_sql: Optional[str] = None
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """
        Hybrid search using dense + sparse embeddings with RRF fusion.

        Args:
            collection: Collection name
            dense_query: Dense query vector
            sparse_query: Sparse query (token_id -> weight)
            limit: Max results to return
            filter_sql: Optional SQL WHERE clause

        Returns:
            List of (id, metadata, score) tuples
        """
        table_ref = self._table_ref(collection)
        try:
            with self._conn.cursor() as cur:
                where_clause = f"AND {filter_sql}" if filter_sql else ""
                prefetch = limit * 10

                # Dense search
                cur.execute(f"""
                    SELECT id, metadata, text_content, dense_embedding <=> %s::vector AS distance
                    FROM {table_ref}
                    WHERE dense_embedding IS NOT NULL {where_clause}
                    ORDER BY distance
                    LIMIT %s
                """, (dense_query, prefetch))
                dense_results = [(row["id"], row["metadata"], row["text_content"], row["distance"]) for row in cur.fetchall()]

                # Sparse search (if sparse embeddings exist)
                cur.execute(f"""
                    SELECT id, metadata, text_content, sparse_embedding
                    FROM {table_ref}
                    WHERE sparse_embedding IS NOT NULL {where_clause}
                    LIMIT %s
                """, (prefetch,))

                sparse_results = []
                for row in cur.fetchall():
                    doc_sparse = row["sparse_embedding"] or {}
                    # Compute sparse similarity (dot product of matching tokens)
                    score = sum(sparse_query.get(int(k), 0) * v for k, v in doc_sparse.items())
                    if score > 0:
                        sparse_results.append((row["id"], row["metadata"], row["text_content"], score))

                # Sort sparse by score descending
                sparse_results.sort(key=lambda x: x[3], reverse=True)
                sparse_results = sparse_results[:prefetch]

                # RRF Fusion
                rrf_k = 60
                scores = {}
                metadata_map = {}
                text_map = {}

                for rank, (id, meta, text, _) in enumerate(dense_results):
                    scores[id] = {"dense": 1.0 / (rrf_k + rank + 1), "sparse": 0}
                    metadata_map[id] = meta
                    text_map[id] = text

                for rank, (id, meta, text, _) in enumerate(sparse_results):
                    if id not in scores:
                        scores[id] = {"dense": 0, "sparse": 0}
                        metadata_map[id] = meta
                        text_map[id] = text
                    scores[id]["sparse"] = 1.0 / (rrf_k + rank + 1)

                # Combined scores with intersection boost
                results = []
                for id, s in scores.items():
                    combined = 0.5 * s["dense"] + 0.5 * s["sparse"]
                    if s["dense"] > 0 and s["sparse"] > 0:
                        combined *= 1.15  # 15% boost for appearing in both
                    data = metadata_map.get(id) or {}
                    if text_map.get(id):
                        data["text_content"] = text_map[id]
                    results.append((id, data, combined))

                results.sort(key=lambda x: x[2], reverse=True)
                return results[:limit]
        except Exception as e:
            print(f"Hybrid search error: {e}")
            return self.vector_search(collection, dense_query, limit, filter_sql)

    def ultimate_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        late_interaction_query: List[List[float]],
        limit: int = 10,
        filter_sql: Optional[str] = None
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """
        Ultimate search using dense + sparse + late interaction embeddings.

        Args:
            collection: Collection name
            dense_query: Dense query vector
            sparse_query: Sparse query (token_id -> weight)
            late_interaction_query: Late interaction query (list of token vectors)
            limit: Max results to return
            filter_sql: Optional SQL WHERE clause

        Returns:
            List of (id, metadata, score) tuples
        """
        import numpy as np

        try:
            # First get hybrid results
            hybrid_results = self.hybrid_search(collection, dense_query, sparse_query, limit * 3, filter_sql)

            if not hybrid_results:
                return []

            # Get late interaction embeddings for candidates
            candidate_ids = [r[0] for r in hybrid_results]
            table_ref = self._table_ref(collection)

            with self._conn.cursor() as cur:
                cur.execute(f"""
                    SELECT id, late_interaction_embedding
                    FROM {table_ref}
                    WHERE id = ANY(%s) AND late_interaction_embedding IS NOT NULL
                """, (candidate_ids,))

                late_interaction_map = {}
                for row in cur.fetchall():
                    late_interaction_map[row["id"]] = row["late_interaction_embedding"]

            # Score with ColBERT MaxSim if late interaction embeddings exist
            results = []
            query_emb = np.array(late_interaction_query)

            for id, data, hybrid_score in hybrid_results:
                if id in late_interaction_map and late_interaction_map[id]:
                    doc_emb = np.array(late_interaction_map[id])
                    # MaxSim scoring
                    sim = np.dot(query_emb, doc_emb.T)
                    max_sim = np.max(sim, axis=1)
                    colbert_score = float(np.sum(max_sim))

                    # Combine hybrid + colbert scores
                    max_colbert = max(colbert_score, 1e-6)
                    combined = 0.6 * hybrid_score + 0.4 * (colbert_score / max_colbert)
                else:
                    combined = hybrid_score

                results.append((id, data, combined))

            results.sort(key=lambda x: x[2], reverse=True)
            return results[:limit]
        except Exception as e:
            print(f"Ultimate search error: {e}")
            return self.hybrid_search(collection, dense_query, sparse_query, limit, filter_sql)

    # =========================================================================
    # Schema-Qualified Table References
    # =========================================================================

    def _collections_table_ref(self) -> str:
        """Get schema-qualified collections metadata table reference."""
        schema = self.config.lakebase_schema or "public"
        return f'"{schema}"._vectrix_collections'

    def _doc_table_ref(self) -> str:
        """Get schema-qualified document table reference."""
        schema = self.config.lakebase_schema or "public"
        return f'"{schema}"._vectrix_documents'

    def _node_table_ref(self) -> str:
        """Get schema-qualified node table reference."""
        schema = self.config.lakebase_schema or "public"
        return f'"{schema}"._vectrix_nodes'

    def ensure_document_tables(self) -> None:
        """Create document and node tables if they don't exist."""
        schema = self.config.lakebase_schema or "public"
        doc_table = self._doc_table_ref()
        node_table = self._node_table_ref()

        with self._lock:
            with self._conn.cursor() as cur:
                # Ensure schema exists
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

                # Documents table
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {doc_table} (
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
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {node_table} (
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
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_vectrix_docs_type
                    ON {doc_table}(doc_type)
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_vectrix_nodes_doc
                    ON {node_table}(doc_id)
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_vectrix_nodes_parent
                    ON {node_table}(parent_id)
                """)

                self._conn.commit()

    def save_document(self, doc_data: Dict[str, Any]) -> None:
        """Save document metadata."""
        doc_table = self._doc_table_ref()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {doc_table}
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
        doc_table = self._doc_table_ref()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {doc_table} WHERE doc_id = %s",
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
        doc_table = self._doc_table_ref()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {doc_table} ORDER BY indexed_at DESC"
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
        doc_table = self._doc_table_ref()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {doc_table} WHERE doc_id = %s",
                    (doc_id,)
                )
                self._conn.commit()
                return cur.rowcount > 0

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""
        node_table = self._node_table_ref()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {node_table}
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
        node_table = self._node_table_ref()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {node_table} WHERE node_id = %s",
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
        node_table = self._node_table_ref()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {node_table} WHERE doc_id = %s ORDER BY position",
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
        node_table = self._node_table_ref()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {node_table} WHERE parent_id = %s ORDER BY position",
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
        node_table = self._node_table_ref()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {node_table} WHERE doc_id = %s",
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

    def _ensure_collection_table(self, name: str, mode: str = "dense") -> None:
        """Create collection table with adaptive schema based on mode.

        Schema adapts based on mode:
        - dense: dense_embedding only
        - hybrid: dense_embedding + sparse_embedding
        - ultimate/graph: dense_embedding + sparse_embedding + late_interaction_embedding
        """
        with self._lock:
            full_name = self._full_table_name(name)

            # Check if table exists
            try:
                self._cursor.execute(f"DESCRIBE TABLE {full_name}")
                existing_columns = {row[0] for row in self._cursor.fetchall()}
            except Exception:
                existing_columns = set()

            # If table exists but missing dense_embedding, drop and recreate
            if existing_columns and "dense_embedding" not in existing_columns:
                self._cursor.execute(f"DROP TABLE IF EXISTS {full_name}")
                existing_columns = set()

            # Create table with all columns
            if not existing_columns:
                self._cursor.execute(f"""
                    CREATE TABLE {full_name} (
                        id STRING NOT NULL,
                        data STRING,
                        dense_embedding ARRAY<DOUBLE>,
                        sparse_embedding STRING,
                        late_interaction_embedding STRING,
                        metadata STRING,
                        text_content STRING,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    ) USING DELTA
                """)
            else:
                # Add missing columns
                for col, col_type in [
                    ("dense_embedding", "ARRAY<DOUBLE>"),
                    ("sparse_embedding", "STRING"),
                    ("late_interaction_embedding", "STRING"),
                    ("metadata", "STRING"),
                    ("text_content", "STRING"),
                ]:
                    if col not in existing_columns:
                        try:
                            self._cursor.execute(f"ALTER TABLE {full_name} ADD COLUMN {col} {col_type}")
                        except Exception:
                            pass  # Column may already exist

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:
        mode = config.get("mode", "dense")
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
            self._ensure_collection_table(name, mode=mode)

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
        dense_embedding = data.pop("_embedding", None) or data.pop("dense_embedding", None)
        sparse_embedding = data.pop("sparse_embedding", None)
        late_interaction_embedding = data.pop("late_interaction_embedding", None)
        text_content = data.pop("text_content", None)
        now = datetime.now().isoformat()

        with self._lock:
            dense_str = f"ARRAY({','.join(map(str, dense_embedding))})" if dense_embedding else "NULL"
            sparse_str = f"'{json.dumps(sparse_embedding)}'" if sparse_embedding else "NULL"
            late_str = f"'{json.dumps([e.tolist() if hasattr(e, 'tolist') else e for e in late_interaction_embedding])}'" if late_interaction_embedding else "NULL"
            text_str = f"'{text_content}'" if text_content else "NULL"

            self._cursor.execute(f"""
                MERGE INTO {self._full_table_name(collection)} AS target
                USING (SELECT '{id}' AS id) AS source
                ON target.id = source.id
                WHEN MATCHED THEN UPDATE SET
                    data = '{json.dumps(data)}',
                    dense_embedding = {dense_str},
                    sparse_embedding = {sparse_str},
                    late_interaction_embedding = {late_str},
                    text_content = {text_str},
                    updated_at = '{now}'
                WHEN NOT MATCHED THEN INSERT (id, data, dense_embedding, sparse_embedding, late_interaction_embedding, text_content, created_at, updated_at)
                VALUES ('{id}', '{json.dumps(data)}', {dense_str}, {sparse_str}, {late_str}, {text_str}, '{now}', '{now}')
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

    def vector_search(self, collection: str, query_vector: List[float], limit: int = 10) -> List[Tuple[str, Dict[str, Any], float]]:
        """
        Vector search using cosine similarity (dense only).
        NOTE: This is SLOW in Delta Lake (full table scan). Use Lakebase for fast search.
        """
        import math
        results = []
        for id_, data in self.iterate(collection):
            embedding = data.get("dense_embedding") or data.get("_embedding")
            if embedding:
                dot = sum(a * b for a, b in zip(query_vector, embedding))
                norm_q = math.sqrt(sum(a * a for a in query_vector))
                norm_e = math.sqrt(sum(a * a for a in embedding))
                if norm_q > 0 and norm_e > 0:
                    similarity = dot / (norm_q * norm_e)
                    distance = 1 - similarity
                    result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                    results.append((id_, result_data, distance))

        results.sort(key=lambda x: x[2])
        return results[:limit]

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Hybrid search using dense + sparse with RRF fusion."""
        prefetch = limit * 10

        # Dense search
        dense_results = self.vector_search(collection, dense_query, prefetch)

        # Sparse search
        sparse_results = []
        for id_, data in self.iterate(collection):
            sparse_emb = data.get("sparse_embedding")
            if sparse_emb:
                if isinstance(sparse_emb, str):
                    sparse_emb = json.loads(sparse_emb)
                score = sum(sparse_query.get(int(k), 0) * v for k, v in sparse_emb.items())
                if score > 0:
                    result_data = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                    sparse_results.append((id_, result_data, score))
        sparse_results.sort(key=lambda x: x[2], reverse=True)
        sparse_results = sparse_results[:prefetch]

        # RRF Fusion
        rrf_k = 60
        scores = {}
        data_map = {}

        for rank, (id_, data, _) in enumerate(dense_results):
            scores[id_] = {"dense": 1.0 / (rrf_k + rank + 1), "sparse": 0}
            data_map[id_] = data

        for rank, (id_, data, _) in enumerate(sparse_results):
            if id_ not in scores:
                scores[id_] = {"dense": 0, "sparse": 0}
                data_map[id_] = data
            scores[id_]["sparse"] = 1.0 / (rrf_k + rank + 1)

        results = []
        for id_, s in scores.items():
            combined = 0.5 * s["dense"] + 0.5 * s["sparse"]
            if s["dense"] > 0 and s["sparse"] > 0:
                combined *= 1.15
            results.append((id_, data_map[id_], combined))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:limit]

    def ultimate_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        late_interaction_query: List[List[float]],
        limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Ultimate search using dense + sparse + late interaction (ColBERT)."""
        import numpy as np

        hybrid_results = self.hybrid_search(collection, dense_query, sparse_query, limit * 3)

        if not hybrid_results:
            return []

        results = []
        query_emb = np.array(late_interaction_query)

        for id_, data, hybrid_score in hybrid_results:
            full_data = self.get(collection, id_)
            late_interaction_emb = full_data.get("late_interaction_embedding") if full_data else None

            if late_interaction_emb:
                if isinstance(late_interaction_emb, str):
                    late_interaction_emb = json.loads(late_interaction_emb)
                doc_emb = np.array(late_interaction_emb)
                sim = np.dot(query_emb, doc_emb.T)
                max_sim = np.max(sim, axis=1)
                colbert_score = float(np.sum(max_sim))

                max_colbert = max(colbert_score, 1e-6)
                combined = 0.6 * hybrid_score + 0.4 * (colbert_score / max_colbert)
            else:
                combined = hybrid_score

            results.append((id_, data, combined))

        results.sort(key=lambda x: x[2], reverse=True)
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
