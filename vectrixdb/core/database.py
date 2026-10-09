"""
VectrixDB Database - Main database interface.

Manages collections and provides a unified interface with:
- Multiple storage backends (memory, SQLite, Cosmos DB, Lakebase)
- Caching layer (memory LRU, Redis, hybrid)
- Auto-scaling and resource management
- Thread-safe operations

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

import json
import logging
import os
import sqlite3
import threading
import warnings
from datetime import datetime
from .._time import utcnow
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union, Callable, Any, Dict, List

from .collection import Collection
from .types import CollectionInfo, DatabaseInfo, DistanceMetric, IndexConfig
from ..exceptions import (
    CollectionLoadWarning,
    InvalidCollectionName,
    StorageOperationError,
)
from .storage import (
    RESERVED_DATABASE_NAMES,
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
from .document_index import (
    DocumentIndex,
    DocumentInfo,
    DocumentNode,
    DocumentType,
    ChunkInfo,
)


__all__ = [
    "VectrixDB",
]


# ============================================================================
# SETTINGS: the logger, and the names Windows reserves
# ============================================================================
#
# One logger for the database's lines, and the device names a collection
# cannot be called on Windows.

logger = logging.getLogger(__name__)

# Windows keeps these as device names whatever the extension, so a collection
# called "con" would be a file that cannot be created or opened.
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{stem}{n}" for stem in ("com", "lpt") for n in range(1, 10)
}


# ============================================================================
# A COLLECTION'S NAME
# ============================================================================
#
# INPUT   a name
# OUTPUT  checked as safe for a directory name, or refused with the reason
#
# A name that cannot be a directory is refused before anything is made.


def validate_collection_name(name: str) -> str:
    """Check a collection name is safe to use as a directory name.

    The name becomes a path under the database directory. Unvalidated, a
    name containing a separator or a parent reference wrote the collection's
    files outside the directory the caller designated, and the REST create
    route accepted one over the wire.
    """
    if not isinstance(name, str):
        raise InvalidCollectionName(f"Collection name must be a string, got {type(name).__name__}.")
    if not name.strip():
        raise InvalidCollectionName("Collection name must not be empty or only whitespace.")
    if name != name.strip():
        raise InvalidCollectionName(
            f"Collection name must not start or end with whitespace: {name!r}"
        )
    if "\x00" in name:
        raise InvalidCollectionName("Collection name must not contain a null byte.")
    if "/" in name or "\\" in name:
        raise InvalidCollectionName(
            f"Collection name must not contain a path separator: {name!r}. "
            f"The name is used as a directory under the database path."
        )
    if name in (".", "..") or name.startswith(".."):
        raise InvalidCollectionName(f"Collection name must not be a parent reference: {name!r}")
    if ":" in name:
        raise InvalidCollectionName(
            f"Collection name must not contain a drive or stream separator: {name!r}"
        )
    lowered = name.lower()
    if lowered in RESERVED_DATABASE_NAMES:
        raise InvalidCollectionName(
            f"{name!r} is reserved: VectrixDB keeps its own {name}.db beside the collections."
        )
    # A collection's files sit beside the others' in the database directory:
    # "docs.db" is the file of a collection "docs", and "docs.documents" its
    # documents, so a name ending like that collided with them.
    for suffix in (".db", ".db-wal", ".db-shm", ".db-journal", ".documents"):
        if lowered.endswith(suffix):
            raise InvalidCollectionName(
                f"Collection name must not end in {suffix!r}: {name!r} would collide "
                f"with another collection's files."
            )
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        raise InvalidCollectionName(f"{name!r} is a reserved device name on Windows.")
    if len(name) > 255:
        raise InvalidCollectionName(
            f"Collection name must be 255 characters or fewer, got {len(name)}."
        )
    return name


if TYPE_CHECKING:  # pragma: no cover
    from .graphrag import GraphRAGConfig, GraphRAGPipeline, GraphSearchResult


# ============================================================================
# GRAPHRAG ON FIRST USE
# ============================================================================
#
# INPUT   nothing
# OUTPUT  the GraphRAG package, imported once, or None when it cannot be
#
# Imported when a graph is first asked for, so a database without one pays
# nothing for it.


def _graphrag():
    """The GraphRAG package on first use, or None when it cannot be imported.

    Importing it eagerly cost every user of this module the whole GraphRAG
    import tree, extractors included, whether or not a graph was ever built.
    """
    try:
        from . import graphrag
    except ImportError:
        return None
    return graphrag


# Version - imported from main package
from .. import __version__


# ============================================================================
# THE DATABASE
# ============================================================================
#
# INPUT   a path and a storage configuration
# OUTPUT  collections made, opened, listed and deleted over one backend:
#         memory, SQLite, Cosmos DB, Lakebase, Delta Lake, OpenSearch, Aurora
#         or Azure AI Search, with caching and auto-scaling
#
# Thread-safe, and the one interface the easy API and the server build on.


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

    With Databricks Lakebase:
        >>> db = VectrixDB.with_lakebase(
        ...     host="your-workspace.cloud.databricks.com",
        ...     token="dapi_xxxxx"
        ... )

    With auto-scaling:
        >>> from vectrixdb import ScalingConfig, ScalingStrategy
        >>> scaling_config = ScalingConfig(strategy=ScalingStrategy.BALANCED)
        >>> db = VectrixDB("./my_vectors", scaling_config=scaling_config)

    Features:
        - Multiple collections with HNSW indexing
        - Hybrid search (vector + keyword)
        - Pluggable storage (memory, SQLite, Cosmos DB, Lakebase)
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
        readonly: bool = False,
        chunk_store: Any = None,
        collection_store: Any = None,
        follow_shared: bool = False,
    ):
        """
        Initialize VectrixDB.

        Args:
            path: Storage path. None for in-memory database.
            storage_config: Storage backend configuration (overrides path).
            cache_config: Caching layer configuration.
            scaling_config: Auto-scaling configuration.
            chunk_store: Where every collection keeps a copy of its chunks for
                the collection pages, when more than one process writes:
                cosmos://<account>.documents.azure.com/<database>/<container>,
                or a store of your own. See vectrixdb.chunk_store.
            collection_store: Where every collection's rules are kept, its
                policy, visibility and masking, shared by every server: a
                path, sqlite:///, postgresql://, cosmos:// or dynamodb://, or
                a store already made. See vectrixdb.collection_records.
            follow_shared: Open the collections a shared backend holds as
                they are asked for, the ones another process made: at start,
                in ``list_collections`` and on a lookup by name. What a server
                beside an ingest worker wants. Off by default, so a handle
                that creates its collection on first use, as ``Vectrix``
                does, still creates it with its own options. See
                ``open_shared``.

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
        self._failed_collections: dict[str, str] = {}
        self._lock = threading.RLock()
        self._created_at = utcnow()

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

        # Read-only opens map each index file instead of loading it, so a
        # collection larger than memory can still be searched. Set before the
        # storage opens, which writes nothing when it is set.
        self.readonly = readonly
        # Initialize main database storage
        self._init_storage()
        # The copy of every chunk the collection pages read when more than one
        # process writes. Opened once; each collection gets its own view of it.
        from ..chunk_store import open_chunk_store

        self._chunk_store: Any = open_chunk_store(chunk_store)
        # Every collection's rules, shared by every server. Opened once, before
        # the collections, so the first search of any of them already has it.
        from ..collection_records import open_collection_store

        self._collection_store: Any = open_collection_store(collection_store)
        self._load_collections()
        self._follow_shared = follow_shared
        if follow_shared:
            self.open_shared()

        # Start auto-scaler if enabled
        if self._auto_scaler:
            self._start_auto_scaler()

        # Initialize GraphRAG if configured
        self._graphrag_config = graphrag_config
        self._graphrag_pipeline: Optional["GraphRAGPipeline"] = None
        if graphrag_config and _graphrag() is not None:
            if graphrag_config.enabled:
                self._init_graphrag()

        # Initialize Document Index
        self._document_index: Optional[DocumentIndex] = None

    def _init_storage(self) -> None:
        """Initialize the main database metadata storage."""
        if self.path:
            os.makedirs(self.path, exist_ok=True)
            db_path = self.path / "_vectrixdb.db"
            # timeout is the busy handler: another process holding the write
            # lock makes this connection wait, not fail with "database is
            # locked". Readers that open while a writer is mid-transaction
            # were failing exactly that way.
            self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
            # Enable WAL mode for crash recovery. Changing the journal mode
            # takes an exclusive lock, so only ask when it is not WAL already.
            mode = self._db.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
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

        # Store version and config. Not on a read-only open, which writes
        # nothing.
        if getattr(self, "readonly", False):
            return
        self._db.execute(
            "INSERT OR REPLACE INTO database_meta (key, value) VALUES (?, ?)",
            ("version", __version__),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO database_meta (key, value) VALUES (?, ?)",
            (
                "storage_backend",
                self._storage_config.backend.value if self._storage_config else "sqlite",
            ),
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
                text_boosts = None
                text_language = "en"
                shard_size = None
                # A row written before this was kept opens with the text
                # index, which is what every such collection had.
                enable_text_index = True
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
                        text_boosts = config_data.get("text_boosts") or None
                        text_language = config_data.get("text_language") or "en"
                        # How the collection was built. A row written before
                        # sharding existed has none, and opens unsharded.
                        shard_size = config_data.get("shard_size")
                        enable_text_index = bool(config_data.get("enable_text_index", True))
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
                if tags and "demo" in tags and self.readonly:
                    # Skipped, not deleted: a read-only open writes nothing.
                    continue
                if tags and "demo" in tags:
                    # Delete demo collection from database and files
                    self._db.execute("DELETE FROM collections WHERE name = ?", (row["name"],))
                    self._db.commit()
                    if self.path:
                        collection_path = self.path / row["name"]
                        if collection_path.exists():
                            import shutil

                            shutil.rmtree(collection_path)
                    logger.info("removed demo collection %r", row["name"])
                    continue

                collection = Collection(
                    shard_size=shard_size,
                    name=row["name"],
                    dimension=row["dimension"],
                    path=self.path / row["name"] if self.path else None,
                    metric=DistanceMetric(row["metric"]),
                    description=row["description"],
                    index_config=index_config,
                    ef_construction=index_config.hnsw_ef_construction if index_config else 200,
                    m=index_config.hnsw_m if index_config else 16,
                    enable_text_index=enable_text_index,
                    tags=tags,
                    storage_backend=self._storage,  # Pass storage backend for vector persistence
                    text_boosts=text_boosts,
                    text_language=text_language,
                    readonly=self.readonly,
                    chunk_store=self._chunks_for(row["name"]),
                    collection_store=getattr(self, "_collection_store", None),
                )

                # Integrate cache
                collection._cache = self._vector_cache

                self._collections[row["name"]] = collection
            except Exception as e:
                # A collection that will not load used to be dropped from the
                # database object after a printed line, so listing it showed
                # nothing and a caller could reasonably conclude the data was
                # gone and re-ingest over it. One bad collection should not
                # stop the others opening, so this still carries on, but the
                # failure is recorded, warned about, and raised by name if
                # anyone asks for that collection.
                self._failed_collections[row["name"]] = f"{type(e).__name__}: {e}"
                logger.warning("collection %r failed to load: %s", row["name"], e, exc_info=True)
                warnings.warn(
                    f"Collection {row['name']!r} failed to load and is not available: {e}. "
                    f"The data on disk was left alone. See VectrixDB.failed_collections.",
                    CollectionLoadWarning,
                    stacklevel=2,
                )

    #: Backends that live with this process alone. Every other one is shared,
    #: and what another process made in it is this one's to open.
    _LOCAL_BACKENDS = (StorageBackend.SQLITE, StorageBackend.MEMORY)

    def open_shared(self, name: Optional[str] = None) -> List[str]:
        """Open the collections a shared backend holds that this process has not: the ones another process made.

        Each process keeps its own list of collections, beside it on disk. A
        server and an ingest worker reading one Azure AI Search service are
        two readers of one index, but the server knew only what it had made
        itself: it listed none of the worker's collections and answered a
        search on one with "not found". A database made with
        ``follow_shared=True``, as the server's is, calls this at start, when
        it lists collections, and on a lookup of a name it does not know.

        ``name`` reads that one collection's record rather than the whole
        list. Returns the names opened. A backend on this machine alone,
        SQLite or memory, has nothing another process made, and is left
        alone. A backend that cannot be read leaves what is open alone; a
        record that will not open goes in ``failed_collections``, as a local
        collection that will not load does.

        Nothing here is written to this process's own list, so a collection
        another process deletes is not kept alive here after a restart, and
        opening one with ``Vectrix`` still creates it with that handle's
        options when it is not open.
        """
        backend = getattr(self._storage_config, "backend", None)
        if self._storage is None or backend in self._LOCAL_BACKENDS:
            return []
        try:
            names = [name] if name is not None else list(self._storage.list_collections())
        except Exception as exc:  # noqa: BLE001 - an unreachable backend leaves what is open alone
            logger.warning("the collections in the shared backend could not be listed: %s", exc)
            return []
        opened: List[str] = []
        for each in names:
            with self._lock:
                if each in self._collections or each in self._failed_collections:
                    continue
            try:
                config = self._storage.get_collection_config(each)
            except Exception as exc:  # noqa: BLE001 - one record that cannot be read leaves the others
                logger.warning(
                    "collection %r in the shared backend could not be read: %s", each, exc
                )
                continue
            if not config:
                continue
            try:
                self._open_shared(each, config)
                opened.append(each)
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the others opening
                with self._lock:
                    self._failed_collections[each] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "collection %r in the shared backend failed to open: %s",
                    each,
                    exc,
                    exc_info=True,
                )
        return opened

    def _open_shared(self, name: str, config: Dict[str, Any]) -> None:
        """One collection from its record in a shared backend, opened as ``_load_collections`` opens a local one."""
        if not config.get("dimension"):
            raise ValueError("its record in the backend names no dimension")
        collection_path = self.path / name if self.path else None
        if collection_path:
            os.makedirs(collection_path, exist_ok=True)
        tags = config.get("tags") or None
        collection = Collection(
            name=name,
            dimension=int(config["dimension"]),
            path=collection_path,
            metric=DistanceMetric(config.get("metric") or DistanceMetric.COSINE.value),
            description=config.get("description") or None,
            tags=list(tags) if tags else None,
            storage_backend=self._storage,
            readonly=self.readonly,
            chunk_store=self._chunks_for(name),
            collection_store=getattr(self, "_collection_store", None),
        )
        collection._cache = self._vector_cache
        with self._lock:
            # Another thread may have opened it while this one read the record.
            self._collections.setdefault(name, collection)

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
        text_boosts: Optional[dict] = None,
        shard_size: Optional[int] = None,
        text_language: str = "en",
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
        validate_collection_name(name)

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
            if name in self._failed_collections:
                # Registered and on disk, just not loadable. Making it again
                # here opened the same broken files.
                raise ValueError(
                    f"Collection '{name}' already exists but failed to load "
                    f"({self._failed_collections[name]}); delete_collection() it first"
                )

            # Create collection directory
            collection_path = self.path / name if self.path else None
            if collection_path:
                os.makedirs(collection_path, exist_ok=True)

            # Create collection with cache integration and storage backend
            collection = Collection(
                shard_size=shard_size,
                name=name,
                dimension=dimension,
                path=collection_path,
                metric=metric,
                description=description,
                # The whole config, or its ef_search never reached the index.
                index_config=index_config,
                ef_construction=index_config.hnsw_ef_construction,
                m=index_config.hnsw_m,
                enable_text_index=enable_text_index,
                tags=tags,
                storage_backend=self._storage,  # Pass storage backend for vector persistence
                text_boosts=text_boosts,
                text_language=text_language,
                chunk_store=self._chunks_for(name),
                collection_store=getattr(self, "_collection_store", None),
            )

            # Integrate cache with collection
            collection._cache = self._vector_cache

            # Store in database
            now = utcnow().isoformat()
            index_config_json = json.dumps(
                {
                    "type": index_config.index_type.value,
                    "m": index_config.hnsw_m,
                    "ef_construction": index_config.hnsw_ef_construction,
                    "ef_search": index_config.hnsw_ef_search,
                    # Kept with the index config so the text index is rebuilt
                    # with the same weights on every open.
                    "text_boosts": dict(text_boosts or {}),
                    # And with the same tokenisation: a collection cut for
                    # German must not be reopened with English stemming.
                    "text_language": text_language,
                    # And so a sharded collection reopens sharded.
                    "shard_size": shard_size,
                    # And with or without its text index, as it was made.
                    "enable_text_index": bool(enable_text_index),
                }
            )

            # Store tags as JSON
            tags_json = json.dumps(tags) if tags else None

            try:
                self._db.execute(
                    """
                    INSERT INTO collections (name, dimension, metric, description, index_config, tags, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, dimension, metric.value, description, index_config_json, tags_json, now),
                )
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                # Another process registered the same name between our
                # in-memory check and this insert. Say so in the same words
                # the in-memory check uses rather than leaking a UNIQUE
                # constraint traceback.
                self._db.rollback()
                raise ValueError(f"Collection '{name}' already exists") from exc

            # Persist to storage backend (Lakebase/CosmosDB/DeltaLake)
            if self._storage:
                try:
                    # Infer mode from tags if provided
                    mode = "dense"
                    if tags:
                        tags_lower = [t.lower() for t in tags]
                        if "graph" in tags_lower:
                            mode = "graph"
                        elif "ultimate" in tags_lower:
                            mode = "ultimate"
                        elif "hybrid" in tags_lower:
                            mode = "hybrid"
                    self._storage.create_collection(
                        name,
                        {
                            "dimension": dimension,
                            "description": description or "",
                            "metric": metric.value,
                            "tags": tags,
                            "mode": mode,
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "collection %r was created locally but could not be persisted "
                        "to the storage backend: %s",
                        name,
                        e,
                    )

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
        self._follow(name)
        with self._lock:
            if name not in self._collections:
                # "failed to open" and "never existed" are different answers
                # and the caller needs to know which one they got.
                if name in self._failed_collections:
                    raise StorageOperationError(
                        "load",
                        "collection",
                        f"{self._failed_collections[name]}. Its data on disk was not touched.",
                        collection=name,
                    )
                raise KeyError(f"Collection '{name}' not found")
            return self._collections[name]

    @property
    def failed_collections(self) -> dict[str, str]:
        """Collections that are registered but would not open, and why.

        Empty in the ordinary case. A name in here is on disk and was left
        alone; it is simply not loaded.
        """
        with self._lock:
            return dict(self._failed_collections)

    @property
    def chunk_store(self) -> Any:
        """Where the collections here keep the copy of their chunks the pages read, or None. See vectrixdb.chunk_store."""
        return getattr(self, "_chunk_store", None)

    def use_chunk_store(self, where: Any, *, key: Optional[str] = None) -> Any:
        """Keep a copy of every chunk in ``where`` from now on, for every collection here.

        An address, ``cosmos://<account>.documents.azure.com/<database>/<container>``,
        or a store of your own; None stops it. This is how a database opened
        by somebody else gets one: ``Vectrix(chunk_store=...)`` handed a
        database as its storage backend. What was written before is not
        copied. Returns the store.
        """
        from ..chunk_store import open_chunk_store

        store = open_chunk_store(where, key=key)
        with self._lock:
            self._chunk_store = store
            for name, collection in self._collections.items():
                collection._chunk_store = store.collection(name) if store is not None else None
        return store

    def _chunks_for(self, name: str) -> Any:
        """One collection's view of the chunk store, or None when there is no store."""
        store = getattr(self, "_chunk_store", None)
        return store.collection(name) if store is not None else None

    @property
    def collection_store(self) -> Any:
        """Where every collection's rules are kept, shared by every server, or None. See vectrixdb.collection_records."""
        return getattr(self, "_collection_store", None)

    @property
    def sources_store(self) -> Any:
        """Where the feeds and pages each collection keeps up with are kept. See vectrixdb.sources.

        Beside the collection records when there is a store for them, so
        every server sees the same sources and one refresh at a time reads
        each; in this database's own file otherwise, or in memory for a
        database with no path.
        """
        shared = getattr(self, "_collection_store", None)
        if shared is not None:
            return shared.records
        with self._lock:
            held = getattr(self, "_sources_records", None)
            if held is None:
                from ..sources import local_store

                held = self._sources_records = local_store(self.path)
            return held

    def _forget_sources(self, name: str) -> None:
        """A deleted collection's sources, and what they wrote down, gone with it.

        Left behind, a collection made again under the name would be
        refreshed into as if it were the old one, its entries taken as
        already written.
        """
        from ..sources import forget_collection

        try:
            forget_collection(self.sources_store, name)
        except Exception as exc:  # the store's own error, whatever database it is
            logger.warning("the sources of %s could not be forgotten: %s", name, exc)

    def use_collection_store(self, where: Any, *, key: Optional[str] = None) -> Any:
        """Read every collection's rules from ``where`` from now on, for every collection here.

        An address or a store already made; None goes back to each collection
        keeping its policy in its own metadata. This is how a database opened
        by somebody else gets one: ``Vectrix(collection_store=...)`` handed a
        database as its storage backend. Returns the store.
        """
        from ..collection_records import open_collection_store

        store = open_collection_store(where, key=key)
        with self._lock:
            self._collection_store = store
            for collection in self._collections.values():
                collection._collection_store = store
        return store

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
            if name in self._failed_collections and name not in self._collections:
                # Nothing to close, but its record and files are there, and
                # this was the only way to remove them.
                self._delete_failed_collection(name)
                return True
            if name not in self._collections:
                return False

            collection = self._collections[name]

            # The chunk store's rows first, or a collection made again under
            # this name shows the old one's chunks on every page. First, so a
            # store that refuses leaves the collection as it was, and asking
            # again starts from the beginning rather than from a closed one.
            if getattr(collection, "_chunk_store", None) is not None:
                collection._share("delete_collection", lambda store: store.clear())

            # Its record, in the store every server reads, or one made again
            # under this name, by any server, starts with the old one's rules.
            # Gone, the new one answers nobody until it is given a policy,
            # which is the safe way to start. Before anything is closed, for
            # the same reason as the chunks.
            if getattr(self, "_collection_store", None) is not None:
                self._collection_store.delete(name)
            self._forget_sources(name)

            # Unregister from auto-scaler
            if self._auto_scaler:
                self._auto_scaler.unregister_index(name)

            collection.close()

            # The store holds this collection's vector rows, and nothing else
            # removes them. Left behind, they outlive the collection: an empty
            # index defers to the store, so a collection cleared or recreated
            # under the same name answered with the old ids and no text.
            if self._storage is not None:
                self._storage.delete_collection(name)

            # Remove from database
            self._db.execute("DELETE FROM collections WHERE name = ?", (name,))
            self._db.commit()

            # Invalidate cache entries for this collection
            self._cache.delete(f"vectrix:{name}:*")
            self._cache.delete("vectrix:collections:list")
            # The line above asks an exact-match cache for a wildcard, so it
            # never removed a cached search. This does.
            try:
                self._vector_cache.invalidate_collection(name)
            except Exception:  # pragma: no cover - a cache that is down
                pass

            # Remove files
            if self.path:
                collection_path = self.path / name
                if collection_path.exists():
                    import shutil

                    shutil.rmtree(collection_path)

            del self._collections[name]
            return True

    def _delete_failed_collection(self, name: str) -> None:
        """What delete_collection does, for one that never opened."""
        chunks = self._chunks_for(name)
        if chunks is not None:
            chunks.clear()
        if getattr(self, "_collection_store", None) is not None:
            self._collection_store.delete(name)
        self._forget_sources(name)
        if self._storage is not None:
            self._storage.delete_collection(name)
        self._db.execute("DELETE FROM collections WHERE name = ?", (name,))
        self._db.commit()
        self._cache.delete("vectrix:collections:list")
        try:
            self._vector_cache.invalidate_collection(name)
        except Exception:  # pragma: no cover - a cache that is down
            pass
        if self.path:
            collection_path = self.path / name
            if collection_path.exists():
                import shutil

                shutil.rmtree(collection_path)
        del self._failed_collections[name]

    def list_collections(self) -> list[CollectionInfo]:
        """
        List all collections.

        Returns:
            List of CollectionInfo
        """
        if getattr(self, "_follow_shared", False):
            self.open_shared()
        with self._lock:
            return [c.info() for c in self._collections.values()]

    def has_collection(self, name: str) -> bool:
        """Check if a collection exists."""
        self._follow(name)
        return name in self._collections

    def _follow(self, name: str) -> None:
        """With ``follow_shared``, a name this process does not know is looked for in the shared backend."""
        if (
            getattr(self, "_follow_shared", False)
            and name not in self._collections
            and name not in self._failed_collections
        ):
            self.open_shared(name)

    def info(self) -> DatabaseInfo:
        """
        Get database information.

        Returns:
            DatabaseInfo with stats
        """
        total_vectors = sum(c._count_raw() for c in self._collections.values())
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
        scaling_stats: Optional[Dict[str, Any]] = None
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
            if hasattr(self._cache, "close"):
                self._cache.close()

            # Close storage
            if hasattr(self._storage, "close"):
                self._storage.close()

            # The sources table's own connection, when one was opened
            held = getattr(self, "_sources_records", None)
            if held is not None:
                held.close()
                self._sources_records = None

            # Close metadata database
            self._db.close()

    # =========================================================================
    # GraphRAG Methods
    # =========================================================================

    def _init_graphrag(self) -> None:
        """Initialize GraphRAG pipeline."""
        graphrag = _graphrag()
        if graphrag is None:
            raise ImportError(
                "GraphRAG components not available. Ensure all GraphRAG dependencies are installed."
            )

        self._graphrag_pipeline = graphrag.create_pipeline(
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

        # ``is not None``: every embedder returns a numpy array, and an
        # array in a boolean context raises rather than answering. An
        # empty list was also silently dropped to None.
        qv = np.array(query_vector, dtype=np.float32) if query_vector is not None else None

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

    # =========================================================================
    # Document Index Methods
    # =========================================================================

    @property
    def documents(self) -> DocumentIndex:
        """
        Access the document index for hierarchical document storage.

        The document index provides:
        - Tree structure from documents (PDF pages, markdown headings)
        - Smart chunking for vectorization
        - Page/section navigation

        Example:
            >>> db = VectrixDB("./data")
            >>> # Index a document
            >>> db.documents.index_file("guide.pdf")
            >>> # Get chunks for vectorization
            >>> chunks = db.documents.get_chunks("guide")
            >>> # Navigate to page 5
            >>> page = db.documents.get_page("guide", 5)
        """
        if self._document_index is None:
            self._document_index = DocumentIndex(self._storage)
        return self._document_index

    def index_document(
        self,
        file_path: str,
        doc_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> DocumentInfo:
        """
        Index a document file (convenience method).

        Args:
            file_path: Path to the file.
            doc_id: Document ID (defaults to filename).
            doc_type: Document type (auto-detected if not provided).
            chunk_size: Size of chunks for vectorization.
            chunk_overlap: Overlap between chunks.

        Returns:
            DocumentInfo with indexing results.

        Example:
            >>> db = VectrixDB("./data")
            >>> doc = db.index_document("products.pdf")
            >>> print(f"Indexed {doc.page_count} pages")
        """
        return self.documents.index_file(
            file_path=file_path,
            doc_id=doc_id,
            doc_type=doc_type,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def index_text(
        self,
        doc_id: str,
        content: str,
        title: Optional[str] = None,
        doc_type: str = "markdown",
    ) -> DocumentInfo:
        """
        Index text content directly (convenience method).

        Args:
            doc_id: Unique document ID.
            content: Document text content.
            title: Document title.
            doc_type: Type ("markdown", "pdf", "text").

        Returns:
            DocumentInfo with indexing results.

        Example:
            >>> db = VectrixDB("./data")
            >>> doc = db.index_text("readme", "# Welcome\\n...", doc_type="markdown")
        """
        return self.documents.index_text(
            doc_id=doc_id,
            content=content,
            title=title,
            doc_type=doc_type,
        )

    def get_page(self, doc_id: str, page_num: int) -> Optional[DocumentNode]:
        """
        Get a specific page from an indexed document.

        Args:
            doc_id: Document ID.
            page_num: Page number (1-indexed).

        Returns:
            DocumentNode for the page or None.

        Example:
            >>> page = db.get_page("products", 5)
            >>> print(page.text)
        """
        return self.documents.get_page(doc_id, page_num)

    def get_section(self, doc_id: str, section_title: str) -> Optional[DocumentNode]:
        """
        Get a section by title from an indexed document.

        Args:
            doc_id: Document ID.
            section_title: Section title to find.

        Returns:
            DocumentNode for the section or None.

        Example:
            >>> intro = db.get_section("readme", "Introduction")
            >>> print(intro.text)
        """
        return self.documents.get_section(doc_id, section_title)

    def get_chunks(
        self,
        doc_id: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> List[ChunkInfo]:
        """
        Get chunks for vectorization from an indexed document.

        Args:
            doc_id: Document ID.
            chunk_size: Maximum chunk size.
            chunk_overlap: Overlap between chunks.

        Returns:
            List of ChunkInfo objects ready for embedding.

        Example:
            >>> chunks = db.get_chunks("products")
            >>> for chunk in chunks:
            ...     vector = embed(chunk.text)
            ...     collection.add([chunk.chunk_id], [vector], [{"doc_id": chunk.doc_id}])
        """
        return self.documents.get_chunks(doc_id, chunk_size, chunk_overlap)

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
        import asyncio  # only the async API needs it

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: collection.search(query, limit=limit, filter=filter)
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
        import asyncio  # only the async API needs it

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: collection.add(ids, vectors, metadata))

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
    def with_lakebase(
        cls,
        host: str,
        database: str = "vectrixdb",
        token: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        port: int = 5432,
        ssl: bool = True,
        cache_config: Optional[CacheConfig] = None,
    ) -> "VectrixDB":
        """
        Create database with Databricks Lakebase storage (PostgreSQL + pgvector).

        Lakebase is Databricks' managed PostgreSQL with pgvector support,
        ideal for production vector databases with enterprise features.

        Args:
            host: Lakebase host URL
            database: Database name in Lakebase
            token: Databricks Personal Access Token (preferred auth)
            user: PostgreSQL username (fallback auth)
            password: PostgreSQL password (fallback auth)
            port: PostgreSQL port (default: 5432)
            ssl: Enable SSL (default: True, required for Databricks)
            cache_config: Optional cache configuration

        Example:
            # With Databricks token (recommended)
            db = VectrixDB.with_lakebase(
                host="your-workspace.cloud.databricks.com",
                token="dapi_xxxxx"
            )

            # With user/password
            db = VectrixDB.with_lakebase(
                host="your-lakebase-host.com",
                user="admin",
                password="your-password"
            )
        """
        storage_config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host=host,
            lakebase_port=port,
            lakebase_database=database,
            lakebase_token=token,
            lakebase_user=user,
            lakebase_password=password,
            lakebase_ssl=ssl,
        )
        return cls(storage_config=storage_config, cache_config=cache_config)

    @classmethod
    def with_delta_lake(
        cls,
        workspace_url: str,
        token: str,
        catalog: str = "main",
        schema: str = "vectrixdb",
        warehouse_id: Optional[str] = None,
        http_path: Optional[str] = None,
        cache_config: Optional[CacheConfig] = None,
    ) -> "VectrixDB":
        """
        Create database with Databricks Delta Lake + Unity Catalog storage.

        Delta Lake provides governed, ACID-compliant storage with time travel
        and lineage tracking. Best for batch workloads and compliance.

        NOTE: Vector search is SLOW in Delta Lake (full scan). For real-time
        search, use with_lakebase() or sync Delta Lake to Lakebase.

        Args:
            workspace_url: Databricks workspace URL (e.g., https://adb-123.azuredatabricks.net)
            token: Databricks Personal Access Token
            catalog: Unity Catalog name (must exist, default: "main")
            schema: Schema name (created if not exists, default: "vectrixdb")
            warehouse_id: SQL Warehouse ID (optional, for serverless)
            http_path: HTTP path for SQL Warehouse (optional)
            cache_config: Optional cache configuration

        Example:
            db = VectrixDB.with_delta_lake(
                workspace_url="https://adb-123456789.azuredatabricks.net",
                token="dapi_xxxxx",
                catalog="main",
                schema="agent_registry"
            )

            # Create collection (stored in Delta Lake)
            db.create_collection("embeddings", dimension=384)

            # Index documents (stored in Delta Lake)
            db.documents.index_text("Hello world", title="Doc1")
        """
        storage_config = StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url=workspace_url,
            delta_token=token,
            delta_catalog=catalog,
            delta_schema=schema,
            delta_warehouse_id=warehouse_id,
            delta_http_path=http_path,
        )
        return cls(storage_config=storage_config, cache_config=cache_config)

    @classmethod
    def with_opensearch(
        cls,
        endpoint: str,
        region: str = "us-east-1",
        service: str = "aoss",
        index_prefix: str = "vectrix",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        cache_config: Optional[CacheConfig] = None,
        filter_fields: Optional[dict] = None,
        knn_engine: str = "nmslib",
        embeddings: str = "vectrixdb",
        embed_fn: Any = None,
        embed_dimensions: Optional[int] = None,
        vector_weights: Optional[dict] = None,
        score_formula: str = "auto",
    ) -> "VectrixDB":
        """
        Create database with AWS OpenSearch Serverless storage.

        OpenSearch provides managed vector search with k-NN support.

        NOTE: OpenSearch supports dense and hybrid modes ONLY.
        For ultimate/graph modes, use with_aurora_postgresql().

        Args:
            endpoint: OpenSearch endpoint (e.g., https://xxx.us-east-1.aoss.amazonaws.com)
            region: AWS region (default: us-east-1)
            service: Service type - "aoss" for Serverless, "es" for managed
            index_prefix: Prefix for index names (default: vectrix)
            aws_access_key_id: AWS access key (optional, uses boto3 chain if not provided)
            aws_secret_access_key: AWS secret key
            aws_session_token: AWS session token (for temporary credentials)
            cache_config: Optional cache configuration

        ``filter_fields`` names metadata paths to map as filterable index
        fields, path to kind (``"string"``, ``"strings"``, ``"number"``,
        ``"boolean"``), so a filter or an entitlement policy over them runs
        inside OpenSearch; decided when the index is created. ``knn_engine``
        is the engine for new indexes: ``"nmslib"`` filters after the search
        and ``"lucene"`` or ``"faiss"`` during it, which keeps recall under a
        selective filter.

        ``embeddings`` says whose dense vectors the index holds:
        ``"vectrixdb"``, the collection's own model; ``"bedrock"``, a Bedrock
        model in its place; or ``"both"``, side by side and fused by rank, so
        a search can ask for either with ``vectors=``. ``embed_fn`` is the
        Bedrock embedder, ``vectrixdb.models.bedrock.BedrockEmbedder`` built
        around your ``bedrock-runtime`` client, and ``embed_dimensions`` its
        width when it does not say. OpenSearch has nothing here that embeds a
        question for it, so Bedrock is called from this process at ingest
        and at query. ``vector_weights``, ``{"vectrixdb": 1.0, "bedrock":
        2.0}``, is how much each counts in the fusion.

        Example:
            db = VectrixDB.with_opensearch(
                endpoint="https://xxx.us-east-1.aoss.amazonaws.com",
                region="us-east-1",
            )
        """
        storage_config = StorageConfig(
            backend=StorageBackend.OPENSEARCH,
            opensearch_endpoint=endpoint,
            opensearch_region=region,
            opensearch_service=service,
            opensearch_index_prefix=index_prefix,
            opensearch_aws_access_key_id=aws_access_key_id,
            opensearch_aws_secret_access_key=aws_secret_access_key,
            opensearch_aws_session_token=aws_session_token,
            opensearch_filter_fields=filter_fields,
            opensearch_knn_engine=knn_engine,
            opensearch_embeddings=embeddings,
            opensearch_embed_fn=embed_fn,
            opensearch_embed_dimensions=embed_dimensions,
            opensearch_vector_weights=vector_weights,
            opensearch_score_formula=score_formula,
        )
        return cls(storage_config=storage_config, cache_config=cache_config)

    @classmethod
    def with_azure_search(
        cls,
        endpoint: str,
        key: Optional[str] = None,
        index_prefix: str = "vectrix",
        semantic: bool = False,
        cache_config: Optional[CacheConfig] = None,
        filter_fields: Optional[dict] = None,
        embeddings: str = "vectrixdb",
        azure_embedding: Optional[dict] = None,
        embed_fn: Any = None,
        vector_weights: Optional[dict] = None,
        relevance: bool = True,
    ) -> "VectrixDB":
        """
        Create database with Azure AI Search storage.

        One Azure index per collection with vector, BM25 and hybrid queries
        served by the service; dense and hybrid modes. Pass ``key`` (an admin
        key) or leave it None to authenticate with ``DefaultAzureCredential``
        from azure-identity (managed identity, CLI login, environment).
        ``semantic=True`` adds Azure's semantic ranker to text and hybrid
        queries, which needs a tier that includes it. ``filter_fields`` names
        metadata paths to promote to filterable index fields, path to kind
        (``"string"``, ``"strings"``, ``"number"``, ``"boolean"``), so a filter or
        a policy over them runs inside the service.

        ``embeddings`` says whose dense vectors the index holds.
        ``"vectrixdb"``, the default, is the collection's own model.
        ``"azure"`` is an Azure OpenAI deployment: the collection embeds with
        it at ingest and the service embeds the question itself. ``"both"``
        keeps the two side by side in one index, and the service fuses them
        in one request, under one filter and one ranker; a search can then
        ask for either with ``vectors=``. Azure AI Search owns no embedding
        model: ``azure_embedding`` names the deployment, ``{"endpoint": ...,
        "deployment": ..., "dimensions": 3072}``, with ``api_key`` when it is
        not keyless. ``embed_fn`` replaces the client built from it, texts to
        vectors. ``vector_weights``, ``{"vectrixdb": 1.0, "azure": 2.0}``, is
        how much each counts in the service's fusion.

        Example:
            db = VectrixDB.with_azure_search(
                endpoint="https://my-service.search.windows.net",
                key=os.environ["AZURE_SEARCH_KEY"],
            )
        """
        storage_config = StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_endpoint=endpoint,
            azure_search_key=key,
            azure_search_index_prefix=index_prefix,
            azure_search_semantic=semantic,
            azure_search_filter_fields=filter_fields,
            azure_search_embeddings=embeddings,
            azure_search_vectorizer=azure_embedding,
            azure_search_embed_fn=embed_fn,
            azure_search_vector_weights=vector_weights,
            azure_search_relevance=relevance,
        )
        return cls(storage_config=storage_config, cache_config=cache_config)

    @classmethod
    def with_aurora_postgresql(
        cls,
        host: str,
        database: str = "vectrixdb",
        user: Optional[str] = None,
        password: Optional[str] = None,
        port: int = 5432,
        ssl: bool = True,
        schema: str = "public",
        cache_config: Optional[CacheConfig] = None,
    ) -> "VectrixDB":
        """
        Create database with AWS Aurora PostgreSQL + pgvector storage.

        Aurora PostgreSQL supports ALL modes including ultimate (ColBERT) and graph.

        Args:
            host: Aurora cluster endpoint
            database: Database name (default: vectrixdb)
            user: Database user
            password: Database password
            port: Port (default: 5432)
            ssl: Use SSL connection (default: True)
            schema: Schema name (default: public)
            cache_config: Optional cache configuration

        Example:
            db = VectrixDB.with_aurora_postgresql(
                host="cluster.xxx.us-east-1.rds.amazonaws.com",
                database="vectrixdb",
                user="admin",
                password="password",
            )
        """
        storage_config = StorageConfig(
            backend=StorageBackend.AURORA_POSTGRESQL,
            aurora_host=host,
            aurora_database=database,
            aurora_user=user,
            aurora_password=password,
            aurora_port=port,
            aurora_ssl=ssl,
            aurora_schema=schema,
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
            memory_high_watermark=max_memory_percent,
        )
        return cls(path=path, scaling_config=scaling_config, cache_config=cache_config)

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
        if _graphrag() is None:
            raise ImportError("GraphRAG not available. Ensure graphrag dependencies are installed.")

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
