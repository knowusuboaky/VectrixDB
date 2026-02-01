"""
VectrixDB Database - Main database interface.

Manages collections and provides a unified interface with:
- Multiple storage backends (memory, SQLite, Cosmos DB)
- Caching layer (memory LRU, Redis, hybrid)
- Auto-scaling and resource management
- Thread-safe operations

Author: Daddy Nyame Owusu - Boakye
"""

import json
import os
import sqlite3
import threading
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, Callable, Any, List

from .collection import Collection
from .types import CollectionInfo, DatabaseInfo, DistanceMetric, IndexConfig
from .storage import (
    StorageBackend,
    StorageConfig,
    BaseStorage,
    create_storage,
)
from .cache import (
    CacheBackend,
    CacheConfig,
    BaseCache,
    VectorCache,
    create_cache,
)
from .scaling import (
    ScalingStrategy,
    ScalingConfig,
    AutoScaler,
    ResourceMonitor,
)

# GraphRAG support (optional import)
try:
    from .graphrag import (
        GraphRAGConfig,
        GraphRAGPipeline,
        GraphSearchResult,
        create_pipeline,
    )
    GRAPHRAG_AVAILABLE = True
except ImportError:
    GRAPHRAG_AVAILABLE = False
    GraphRAGConfig = None
    GraphRAGPipeline = None
    GraphSearchResult = None

# Version - imported from main package
from .. import __version__


class VectrixDB:
    """
    VectrixDB - Where vectors come alive.

    A modern, high-performance vector database with enterprise features.

    Example:
        >>> db = VectrixDB("./my_vectors")
        >>> collection = db.create_collection("documents", dimension=384)
        >>> collection.add(ids=["doc1"], vectors=[[0.1, 0.2, ...]])
        >>> results = collection.search(query=[0.1, 0.2, ...], limit=10)

    With Redis caching:
        >>> from vectrixdb import CacheConfig, CacheBackend
        >>> cache_config = CacheConfig(backend=CacheBackend.REDIS, redis_host="localhost")
        >>> db = VectrixDB("./my_vectors", cache_config=cache_config)

    With Azure Cosmos DB:
        >>> from vectrixdb import StorageConfig, StorageBackend
        >>> storage_config = StorageConfig(
        ...     backend=StorageBackend.COSMOSDB,
        ...     cosmos_endpoint="https://xxx.documents.azure.com:443/",
        ...     cosmos_key="your-key"
        ... )
        >>> db = VectrixDB(storage_config=storage_config)

    With auto-scaling:
        >>> from vectrixdb import ScalingConfig, ScalingStrategy
        >>> scaling_config = ScalingConfig(strategy=ScalingStrategy.BALANCED)
        >>> db = VectrixDB("./my_vectors", scaling_config=scaling_config)

    Features:
        - Multiple collections with HNSW indexing
        - Hybrid search (vector + keyword)
        - Pluggable storage (memory, SQLite, Cosmos DB)
        - Multi-tier caching (memory, Redis, hybrid)
        - Auto-scaling and resource management
        - Thread-safe operations
        - WAL for crash recovery
    """

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        storage_config: Optional[StorageConfig] = None,
        cache_config: Optional[CacheConfig] = None,
        scaling_config: Optional[ScalingConfig] = None,
        graphrag_config: Optional["GraphRAGConfig"] = None,
    ):
        """
        Initialize VectrixDB.

        Args:
            path: Storage path. None for in-memory database.
            storage_config: Storage backend configuration (overrides path).
            cache_config: Caching layer configuration.
            scaling_config: Auto-scaling configuration.

        Example:
            # Persistent database with SQLite
            db = VectrixDB("./my_vectors")

            # In-memory database
            db = VectrixDB()

            # With Redis caching
            cache_config = CacheConfig(backend=CacheBackend.REDIS, redis_host="localhost")
            db = VectrixDB("./my_vectors", cache_config=cache_config)

            # With Azure Cosmos DB storage
            storage_config = StorageConfig(
                backend=StorageBackend.COSMOSDB,
                cosmos_endpoint="https://xxx.documents.azure.com:443/",
                cosmos_key="your-key"
            )
            db = VectrixDB(storage_config=storage_config)
        """
        self.path = Path(path) if path else None
        self._collections: dict[str, Collection] = {}
        self._lock = threading.RLock()
        self._created_at = datetime.utcnow()

        # Configuration
        self._storage_config = storage_config
        self._cache_config = cache_config or CacheConfig()
        self._scaling_config = scaling_config or ScalingConfig()

        # Initialize storage backend
        if storage_config:
            self._storage = create_storage(storage_config)
        else:
            # Default to SQLite if path provided, else memory
            if self.path:
                self._storage_config = StorageConfig(
                    backend=StorageBackend.SQLITE,
                    sqlite_path=str(self.path),  # Pass directory path, not file path
                )
            else:
                self._storage_config = StorageConfig(backend=StorageBackend.MEMORY)
            self._storage = create_storage(self._storage_config)

        # Initialize cache
        self._cache: BaseCache = create_cache(self._cache_config)
        self._vector_cache = VectorCache(
            cache=self._cache,
            prefix="vectrix",
        )

        # Initialize auto-scaler
        self._resource_monitor = ResourceMonitor(self._scaling_config)
        self._auto_scaler: Optional[AutoScaler] = None
        if self._scaling_config.strategy != ScalingStrategy.NONE:
            self._auto_scaler = AutoScaler(
                config=self._scaling_config,
                resource_monitor=self._resource_monitor,
            )

        # Initialize main database storage
        self._init_storage()
        self._load_collections()

        # Start auto-scaler if enabled
        if self._auto_scaler:
            self._start_auto_scaler()

        # Initialize GraphRAG if configured
        self._graphrag_config = graphrag_config
        self._graphrag_pipeline: Optional["GraphRAGPipeline"] = None
        if graphrag_config and GRAPHRAG_AVAILABLE:
            if graphrag_config.enabled:
                self._init_graphrag()

    def _init_storage(self) -> None:
        """Initialize the main database metadata storage."""
        if self.path:
            os.makedirs(self.path, exist_ok=True)
            db_path = self.path / "_vectrixdb.db"
            self._db = sqlite3.connect(str(db_path), check_same_thread=False)
            # Enable WAL mode for crash recovery
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        else:
            self._db = sqlite3.connect(":memory:", check_same_thread=False)

        self._db.row_factory = sqlite3.Row

        # Create tables
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS collections (
                name TEXT PRIMARY KEY,
                dimension INTEGER NOT NULL,
                metric TEXT NOT NULL,
                description TEXT,
                index_config TEXT,
                tags TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS database_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS scaling_stats (
                timestamp TEXT PRIMARY KEY,
                cpu_percent REAL,
                memory_percent REAL,
                total_vectors INTEGER,
                operations_per_sec REAL
            );
        """)

        # Store version and config
        self._db.execute(
            "INSERT OR REPLACE INTO database_meta (key, value) VALUES (?, ?)",
            ("version", __version__),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO database_meta (key, value) VALUES (?, ?)",
            ("storage_backend", self._storage_config.backend.value if self._storage_config else "sqlite"),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO database_meta (key, value) VALUES (?, ?)",
            ("cache_backend", self._cache_config.backend.value),
        )
        self._db.commit()

    def _start_auto_scaler(self) -> None:
        """Start auto-scaler background tasks."""
        if self._auto_scaler:
            # Register all collections for scaling
            for name, collection in self._collections.items():
                self._auto_scaler.register_index(name, collection._index)

    def _stop_auto_scaler(self) -> None:
        """Stop auto-scaler."""
        if self._auto_scaler:
            self._auto_scaler.stop()

    def _load_collections(self) -> None:
        """Load existing collections from storage."""
        cursor = self._db.execute("SELECT * FROM collections")
        for row in cursor:
            try:
                # Parse index config if available
                index_config = None
                if row["index_config"]:
                    try:
                        config_data = json.loads(row["index_config"])
                        from .types import IndexType
                        index_config = IndexConfig(
                            index_type=IndexType(config_data.get("type", "hnsw")),
                            hnsw_m=config_data.get("m", 16),
                            hnsw_ef_construction=config_data.get("ef_construction", 200),
                            hnsw_ef_search=config_data.get("ef_search", 50),
                        )
                    except (json.JSONDecodeError, KeyError):
                        pass

                # Parse tags if available
                tags = None
                if row["tags"]:
                    try:
                        tags = json.loads(row["tags"])
                    except (json.JSONDecodeError, TypeError):
                        tags = []

                # Skip demo collections - they should not persist across restarts
                if tags and "demo" in tags:
                    # Delete demo collection from database and files
                    self._db.execute("DELETE FROM collections WHERE name = ?", (row["name"],))
                    self._db.commit()
                    if self.path:
                        collection_path = self.path / row["name"]
                        if collection_path.exists():
                            import shutil
                            shutil.rmtree(collection_path)
                    print(f"[VectrixDB] Removed demo collection: {row['name']}")
                    continue

                collection = Collection(
                    name=row["name"],
                    dimension=row["dimension"],
                    path=self.path / row["name"] if self.path else None,
                    metric=DistanceMetric(row["metric"]),
                    description=row["description"],
                    ef_construction=index_config.hnsw_ef_construction if index_config else 200,
                    m=index_config.hnsw_m if index_config else 16,
                    tags=tags,
                )

                # Integrate cache
                collection._cache = self._vector_cache

                self._collections[row["name"]] = collection
            except Exception as e:
                print(f"Warning: Failed to load collection '{row['name']}': {e}")

    def create_collection(
        self,
        name: str,
        dimension: int,
        metric: Union[DistanceMetric, str] = DistanceMetric.COSINE,
        description: Optional[str] = None,
        index_config: Optional[IndexConfig] = None,
        ef_construction: int = 200,
        m: int = 16,
        enable_text_index: bool = False,
        tags: Optional[List[str]] = None,
    ) -> Collection:
        """
        Create a new collection.

        Args:
            name: Collection name (must be unique)
            dimension: Vector dimension
            metric: Distance metric ("cosine", "euclidean", "dot")
            description: Optional description
            index_config: Advanced index configuration
            ef_construction: HNSW build parameter (legacy, use index_config)
            m: HNSW connectivity parameter (legacy, use index_config)
            enable_text_index: Enable BM25 text index for hybrid search
            tags: Capability tags (Dense, Sparse, Hybrid, Ultimate, Graph)

        Returns:
            The created Collection

        Raises:
            ValueError: If collection already exists

        Example:
            # Basic collection
            collection = db.create_collection("docs", dimension=384)

            # With hybrid search enabled
            collection = db.create_collection(
                "docs",
                dimension=384,
                enable_text_index=True,
                tags=["Dense", "Hybrid"]
            )

            # With custom index config
            from vectrixdb import IndexConfig, IndexType
            index_config = IndexConfig(
                index_type=IndexType.HNSW,
                hnsw_m=32,
                hnsw_ef_construction=400
            )
            collection = db.create_collection(
                "high_recall",
                dimension=384,
                index_config=index_config
            )
        """
        if isinstance(metric, str):
            metric = DistanceMetric(metric)

        # Build index config from legacy params if not provided
        if index_config is None:
            from .types import IndexType
            index_config = IndexConfig(
                index_type=IndexType.HNSW,
                hnsw_m=m,
                hnsw_ef_construction=ef_construction,
            )

        with self._lock:
            if name in self._collections:
                raise ValueError(f"Collection '{name}' already exists")

            # Create collection directory
            collection_path = self.path / name if self.path else None
            if collection_path:
                os.makedirs(collection_path, exist_ok=True)

            # Create collection with cache integration
            collection = Collection(
                name=name,
                dimension=dimension,
                path=collection_path,
                metric=metric,
                description=description,
                ef_construction=index_config.hnsw_ef_construction,
                m=index_config.hnsw_m,
                enable_text_index=enable_text_index,
                tags=tags,
            )

            # Integrate cache with collection
            collection._cache = self._vector_cache

            # Store in database
            now = datetime.utcnow().isoformat()
            index_config_json = json.dumps({
                "type": index_config.index_type.value,
                "m": index_config.hnsw_m,
                "ef_construction": index_config.hnsw_ef_construction,
                "ef_search": index_config.hnsw_ef_search,
            })

            # Store tags as JSON
            tags_json = json.dumps(tags) if tags else None

            self._db.execute(
                """
                INSERT INTO collections (name, dimension, metric, description, index_config, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, dimension, metric.value, description, index_config_json, tags_json, now),
            )
            self._db.commit()

            self._collections[name] = collection

            # Register with auto-scaler
            if self._auto_scaler:
                self._auto_scaler.register_index(name, collection._index)

            # Invalidate any cached collection info
            self._cache.delete(f"vectrix:collections:list")

            return collection

    def get_collection(self, name: str) -> Collection:
        """
        Get a collection by name.

        Args:
            name: Collection name

        Returns:
            The Collection

        Raises:
            KeyError: If collection doesn't exist
        """
        with self._lock:
            if name not in self._collections:
                raise KeyError(f"Collection '{name}' not found")
            return self._collections[name]

    def get_or_create_collection(
        self,
        name: str,
        dimension: int,
        metric: Union[DistanceMetric, str] = DistanceMetric.COSINE,
        description: Optional[str] = None,
    ) -> Collection:
        """
        Get a collection, creating it if it doesn't exist.

        Args:
            name: Collection name
            dimension: Vector dimension (used if creating)
            metric: Distance metric (used if creating)
            description: Description (used if creating)

        Returns:
            The Collection
        """
        with self._lock:
            if name in self._collections:
                return self._collections[name]
            return self.create_collection(name, dimension, metric, description)

    def delete_collection(self, name: str) -> bool:
        """
        Delete a collection.

        Args:
            name: Collection name

        Returns:
            True if deleted, False if not found
        """
        with self._lock:
            if name not in self._collections:
                return False

            collection = self._collections[name]

            # Unregister from auto-scaler
            if self._auto_scaler:
                self._auto_scaler.unregister_index(name)

            collection.close()

            # Remove from database
            self._db.execute("DELETE FROM collections WHERE name = ?", (name,))
            self._db.commit()

            # Invalidate cache entries for this collection
            self._cache.delete(f"vectrix:{name}:*")
            self._cache.delete("vectrix:collections:list")

            # Remove files
            if self.path:
                collection_path = self.path / name
                if collection_path.exists():
                    import shutil
                    shutil.rmtree(collection_path)

            del self._collections[name]
            return True

    def list_collections(self) -> list[CollectionInfo]:
        """
        List all collections.

        Returns:
            List of CollectionInfo
        """
        with self._lock:
            return [c.info() for c in self._collections.values()]

    def has_collection(self, name: str) -> bool:
        """Check if a collection exists."""
        return name in self._collections

    def info(self) -> DatabaseInfo:
        """
        Get database information.

        Returns:
            DatabaseInfo with stats
        """
        total_vectors = sum(c.count() for c in self._collections.values())
        total_size = 0

        if self.path:
            for f in self.path.rglob("*"):
                if f.is_file():
                    total_size += f.stat().st_size

        return DatabaseInfo(
            path=str(self.path) if self.path else ":memory:",
            version=__version__,
            collections_count=len(self._collections),
            total_vectors=total_vectors,
            total_size_bytes=total_size,
            created_at=self._created_at,
        )

    def extended_info(self) -> dict:
        """
        Get extended database information including storage, cache, and scaling stats.

        Returns:
            Dict with comprehensive stats
        """
        base_info = self.info()

        # Get cache stats
        cache_stats = self._cache.stats

        # Get resource stats
        resource_stats = self._resource_monitor.get_current_stats()

        # Get scaling stats if enabled
        scaling_stats = None
        if self._auto_scaler:
            scaling_stats = {
                "strategy": self._scaling_config.strategy.value,
                "recommendations": [],
            }

        return {
            "database": {
                "path": base_info.path,
                "version": base_info.version,
                "collections_count": base_info.collections_count,
                "total_vectors": base_info.total_vectors,
                "total_size_bytes": base_info.total_size_bytes,
                "created_at": base_info.created_at.isoformat(),
            },
            "storage": {
                "backend": self._storage_config.backend.value if self._storage_config else "sqlite",
                "wal_enabled": True if self.path else False,
            },
            "cache": {
                "backend": self._cache_config.backend.value,
                "hits": cache_stats.hits,
                "misses": cache_stats.misses,
                "hit_rate": cache_stats.hit_rate,
                "size": cache_stats.size,
                "memory_bytes": cache_stats.memory_bytes,
            },
            "resources": resource_stats,
            "scaling": scaling_stats,
        }

    def get_cache_stats(self) -> dict:
        """Get current cache statistics."""
        stats = self._cache.stats
        return {
            "hits": stats.hits,
            "misses": stats.misses,
            "hit_rate": stats.hit_rate,
            "size": stats.size,
            "memory_bytes": stats.memory_bytes,
        }

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()

    def get_resource_stats(self) -> dict:
        """Get current resource utilization stats."""
        return self._resource_monitor.get_current_stats()

    def save(self) -> None:
        """Save all collections to disk."""
        with self._lock:
            for collection in self._collections.values():
                collection.save()
            self._db.commit()

    def close(self) -> None:
        """Close the database and all collections, cleaning up resources."""
        with self._lock:
            # Stop auto-scaler
            self._stop_auto_scaler()

            # Close GraphRAG pipeline
            if self._graphrag_pipeline:
                self._graphrag_pipeline.close()

            # Close all collections
            for collection in self._collections.values():
                collection.close()

            # Close cache
            if hasattr(self._cache, 'close'):
                self._cache.close()

            # Close storage
            if hasattr(self._storage, 'close'):
                self._storage.close()

            # Close metadata database
            self._db.close()

    # =========================================================================
    # GraphRAG Methods
    # =========================================================================

    def _init_graphrag(self) -> None:
        """Initialize GraphRAG pipeline."""
        if not GRAPHRAG_AVAILABLE:
            raise ImportError(
                "GraphRAG components not available. "
                "Ensure all GraphRAG dependencies are installed."
            )

        self._graphrag_pipeline = create_pipeline(
            config=self._graphrag_config,
            path=self.path,
        )

    def add_documents(
        self,
        documents: list[str],
        metadata: Optional[list[dict]] = None,
        doc_ids: Optional[list[str]] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict:
        """
        Add documents for GraphRAG processing.

        This processes documents through the GraphRAG pipeline:
        1. Chunking
        2. Entity extraction
        3. Knowledge graph construction
        4. Community detection
        5. Community summarization

        Args:
            documents: List of document texts
            metadata: Optional metadata for each document
            doc_ids: Optional IDs for each document
            on_progress: Progress callback(current, total, stage)

        Returns:
            Dict with processing statistics

        Raises:
            RuntimeError: If GraphRAG is not enabled

        Example:
            >>> db = VectrixDB("./kb", graphrag_config=GraphRAGConfig(enabled=True))
            >>> stats = db.add_documents(["Document 1 text...", "Document 2 text..."])
            >>> print(f"Processed {stats['documents_processed']} docs")
        """
        if not self._graphrag_pipeline:
            raise RuntimeError(
                "GraphRAG not enabled. Initialize with graphrag_config=GraphRAGConfig(enabled=True)"
            )

        stats = self._graphrag_pipeline.add_documents(
            documents=documents,
            metadata=metadata,
            doc_ids=doc_ids,
            on_progress=on_progress,
        )

        return {
            "documents_processed": stats.documents_processed,
            "chunks_created": stats.chunks_created,
            "entities_extracted": stats.entities_extracted,
            "relationships_extracted": stats.relationships_extracted,
            "communities_detected": stats.communities_detected,
            "processing_time_ms": stats.processing_time_ms,
        }

    def graph_search(
        self,
        query: str,
        query_vector: Optional[list[float]] = None,
        k: int = 10,
        search_type: Optional[str] = None,
    ) -> "GraphSearchResult":
        """
        Search using the knowledge graph.

        Args:
            query: Search query text
            query_vector: Optional query embedding
            k: Number of results
            search_type: "local", "global", or "hybrid" (default: from config)

        Returns:
            GraphSearchResult with entities, communities, and context

        Raises:
            RuntimeError: If GraphRAG is not enabled or graph not built

        Example:
            >>> # Specific entity search
            >>> results = db.graph_search("What is machine learning?", search_type="local")
            >>>
            >>> # Broad thematic search
            >>> results = db.graph_search("What are the main themes?", search_type="global")
            >>>
            >>> # Auto-routed hybrid search
            >>> results = db.graph_search("How does AI relate to healthcare?")
        """
        if not self._graphrag_pipeline:
            raise RuntimeError(
                "GraphRAG not enabled. Initialize with graphrag_config=GraphRAGConfig(enabled=True)"
            )

        import numpy as np
        qv = np.array(query_vector, dtype=np.float32) if query_vector else None

        # Convert search_type string to enum if provided
        st = None
        if search_type:
            from .graphrag import GraphSearchType
            st = GraphSearchType(search_type)

        return self._graphrag_pipeline.search(
            query=query,
            query_vector=qv,
            k=k,
            search_type=st,
        )

    def get_graph_info(self) -> dict:
        """
        Get information about the knowledge graph.

        Returns:
            Dict with graph statistics

        Example:
            >>> info = db.get_graph_info()
            >>> print(f"Graph has {info['entities']} entities and {info['relationships']} relationships")
        """
        if not self._graphrag_pipeline:
            return {
                "enabled": False,
                "entities": 0,
                "relationships": 0,
                "communities": 0,
                "is_built": False,
            }

        info = self._graphrag_pipeline.get_graph_info()
        info["enabled"] = True
        return info

    def get_entity(self, name: str):
        """
        Get an entity from the knowledge graph by name.

        Args:
            name: Entity name

        Returns:
            Entity object or None if not found
        """
        if not self._graphrag_pipeline:
            return None
        return self._graphrag_pipeline.get_entity(name)

    def get_entity_neighbors(self, entity_name: str, depth: int = 1) -> dict:
        """
        Get neighbors of an entity in the knowledge graph.

        Args:
            entity_name: Name of the entity
            depth: Traversal depth

        Returns:
            Dict mapping neighbor IDs to distances
        """
        if not self._graphrag_pipeline:
            return {}
        return self._graphrag_pipeline.get_neighbors(entity_name, depth)

    @property
    def graphrag_enabled(self) -> bool:
        """Check if GraphRAG is enabled."""
        return self._graphrag_pipeline is not None

    def __getitem__(self, name: str) -> Collection:
        """Get collection by name using bracket notation."""
        return self.get_collection(name)

    def __contains__(self, name: str) -> bool:
        """Check if collection exists."""
        return self.has_collection(name)

    def __len__(self) -> int:
        """Number of collections."""
        return len(self._collections)

    def __repr__(self) -> str:
        path = str(self.path) if self.path else ":memory:"
        storage = self._storage_config.backend.value if self._storage_config else "sqlite"
        cache = self._cache_config.backend.value
        return f"VectrixDB(path='{path}', collections={len(self._collections)}, storage={storage}, cache={cache})"

    def __enter__(self) -> "VectrixDB":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # Async support methods
    async def async_search(
        self,
        collection_name: str,
        query: list[float],
        limit: int = 10,
        filter: Optional[Any] = None,
    ):
        """
        Async search in a collection.

        Args:
            collection_name: Name of collection to search
            query: Query vector
            limit: Max results
            filter: Optional filter

        Returns:
            SearchResults
        """
        collection = self.get_collection(collection_name)
        # Run in executor to not block event loop
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: collection.search(query, limit=limit, filter=filter)
        )

    async def async_add(
        self,
        collection_name: str,
        ids: list[str],
        vectors: list[list[float]],
        metadata: Optional[list[dict]] = None,
    ):
        """
        Async add vectors to a collection.

        Args:
            collection_name: Name of collection
            ids: Vector IDs
            vectors: Vectors to add
            metadata: Optional metadata for each vector

        Returns:
            Number of vectors added
        """
        collection = self.get_collection(collection_name)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: collection.add(ids, vectors, metadata)
        )

    # Convenience factory methods
    @classmethod
    def memory(cls) -> "VectrixDB":
        """Create an in-memory database."""
        return cls(storage_config=StorageConfig(backend=StorageBackend.MEMORY))

    @classmethod
    def sqlite(cls, path: Union[str, Path]) -> "VectrixDB":
        """
        Create a SQLite-backed database.

        Args:
            path: Path to database directory
        """
        return cls(path=path)

    @classmethod
    def with_redis_cache(
        cls,
        path: Optional[Union[str, Path]] = None,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: Optional[str] = None,
    ) -> "VectrixDB":
        """
        Create database with Redis caching.

        Args:
            path: Optional storage path
            redis_host: Redis host
            redis_port: Redis port
            redis_password: Redis password

        Example:
            # With local Redis
            db = VectrixDB.with_redis_cache("./data")

            # With Azure Redis
            db = VectrixDB.with_redis_cache(
                "./data",
                redis_host="myredis.redis.cache.windows.net",
                redis_port=6380,
                redis_password="your-key"
            )
        """
        cache_config = CacheConfig(
            backend=CacheBackend.REDIS,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_password=redis_password,
            redis_ssl=redis_port == 6380,  # Azure Redis uses SSL on 6380
        )
        return cls(path=path, cache_config=cache_config)

    @classmethod
    def with_cosmos_db(
        cls,
        endpoint: str,
        key: str,
        database_name: str = "vectrixdb",
        cache_config: Optional[CacheConfig] = None,
    ) -> "VectrixDB":
        """
        Create database with Azure Cosmos DB storage.

        Args:
            endpoint: Cosmos DB endpoint URL
            key: Cosmos DB primary key
            database_name: Database name in Cosmos DB
            cache_config: Optional cache configuration

        Example:
            db = VectrixDB.with_cosmos_db(
                endpoint="https://myaccount.documents.azure.com:443/",
                key="your-primary-key"
            )
        """
        storage_config = StorageConfig(
            backend=StorageBackend.COSMOSDB,
            cosmos_endpoint=endpoint,
            cosmos_key=key,
            cosmos_database=database_name,
        )
        return cls(storage_config=storage_config, cache_config=cache_config)

    @classmethod
    def with_auto_scaling(
        cls,
        path: Optional[Union[str, Path]] = None,
        strategy: ScalingStrategy = ScalingStrategy.BALANCED,
        max_memory_percent: float = 80.0,
        cache_config: Optional[CacheConfig] = None,
    ) -> "VectrixDB":
        """
        Create database with auto-scaling enabled.

        Args:
            path: Optional storage path
            strategy: Scaling strategy
            max_memory_percent: Max memory usage before scaling
            cache_config: Optional cache configuration
        """
        scaling_config = ScalingConfig(
            strategy=strategy,
            max_memory_percent=max_memory_percent,
        )
        return cls(
            path=path,
            scaling_config=scaling_config,
            cache_config=cache_config
        )

    @classmethod
    def with_graphrag(
        cls,
        path: Union[str, Path],
        extractor: str = "hybrid",
        llm_provider: str = "openai",
        llm_model: str = "gpt-4o-mini",
        cache_config: Optional[CacheConfig] = None,
        **graphrag_kwargs,
    ) -> "VectrixDB":
        """
        Create database with GraphRAG enabled.

        GraphRAG provides knowledge graph capabilities on top of vector search:
        - Entity and relationship extraction
        - Hierarchical community detection
        - Local, global, and hybrid search strategies

        Args:
            path: Storage path (required for GraphRAG persistence)
            extractor: Extraction method ("nlp", "llm", or "hybrid")
            llm_provider: LLM provider ("openai", "ollama", "azure_openai", "aws_bedrock")
            llm_model: Model name
            cache_config: Optional cache configuration
            **graphrag_kwargs: Additional GraphRAG config options

        Returns:
            VectrixDB with GraphRAG enabled

        Example:
            >>> # With OpenAI
            >>> db = VectrixDB.with_graphrag("./my_kb")
            >>> db.add_documents(["Document 1...", "Document 2..."])
            >>> results = db.graph_search("What are the main themes?")
            >>>
            >>> # With local Ollama (no API costs)
            >>> db = VectrixDB.with_graphrag(
            ...     "./my_kb",
            ...     extractor="nlp",  # Free NLP extraction
            ...     llm_provider="ollama",
            ...     llm_model="llama3.2"
            ... )
            >>>
            >>> # NLP-only (completely free)
            >>> db = VectrixDB.with_graphrag("./my_kb", extractor="nlp")
        """
        if not GRAPHRAG_AVAILABLE:
            raise ImportError(
                "GraphRAG not available. Ensure graphrag dependencies are installed."
            )

        from .graphrag import GraphRAGConfig, LLMProvider, ExtractorType

        graphrag_config = GraphRAGConfig(
            enabled=True,
            extractor=ExtractorType(extractor),
            llm_provider=LLMProvider(llm_provider),
            llm_model=llm_model,
            **graphrag_kwargs,
        )

        return cls(
            path=path,
            cache_config=cache_config,
            graphrag_config=graphrag_config,
        )
