"""

VectrixDB Storage Backends - Pluggable persistence layer.



Supports multiple storage backends:

- InMemory: Fastest, no persistence (for testing/caching)

- SQLite: Local disk persistence (default)

- Azure Cosmos DB: Cloud-scale persistence

- PostgreSQL: Enterprise SQL backend



Author: Kwadwo Daddy Nyame Owusu - Boakye

"""

import json
import struct

import logging

import os

import time

import contextlib
import contextvars
import threading
import warnings

import numpy as np

from abc import ABC, abstractmethod

from dataclasses import dataclass

from datetime import datetime, timezone

from .._time import utcnow, utcnow_iso

from enum import Enum

from pathlib import Path

import re
from contextlib import contextmanager
from typing import Callable, Any, ClassVar, Dict, Iterator, List, Optional, Tuple

from .types import FilterPushdown
from . import relevance as _relevance

import sqlite3


from ..exceptions import (
    ConfigurationError,
    SearchError,
    StorageConnectionError,
    StorageOperationError,
)


__all__ = [
    "StorageBackend",
    "StorageConfig",
    "BaseStorage",
    "InMemoryStorage",
    "SQLiteStorage",
    "CosmosDBStorage",
    "LakebaseStorage",
    "DeltaLakeStorage",
    "OpenSearchStorage",
    "AuroraPostgreSQLStorage",
    "create_storage",
]


# ============================================================================
# SETTINGS: the logger
# ============================================================================
#
# One logger for every backend's lines.

logger = logging.getLogger(__name__)


# ============================================================================
# NOW, NOT FOUND, AND A SQL LITERAL
# ============================================================================
#
# INPUT   an exception from a driver; a value
# OUTPUT  a timezone-aware UTC timestamp; whether the exception means the
#         thing asked for does not exist, by the names each driver gives it;
#         the value escaped for inline use in a SQL string
#
# Each driver says not found its own way; one function reads them all.


def _utcnow_iso() -> str:
    """Timezone-aware UTC timestamp in ISO-8601 form.



    Replaces ``utcnow()``, which is deprecated and returns a naive

    datetime that silently compares wrong against aware ones.

    """

    return datetime.now(timezone.utc).isoformat()


#: Exception class names used by backend SDKs to signal "this resource is absent".

#: Matched by name so that optional SDKs are never imported just to catch an error.

_NOT_FOUND_NAMES = frozenset(
    {
        "CosmosResourceNotFoundError",  # azure-cosmos
        "ResourceNotFoundError",  # azure-core
        "NotFoundError",  # opensearch-py, elasticsearch
        "NoSuchTableError",  # deltalake / databricks
        "TableNotFoundError",
        "UndefinedTable",  # psycopg
        "ObjectNotFound",
    }
)


#: The databases VectrixDB creates for its own bookkeeping. Only these skip the

#: generic per-collection schema; a user collection may legitimately start with

#: an underscore and must still get its `documents` table.

_INTERNAL_DATABASES = frozenset({"_meta", "_documents", "_nodes"})


def _is_not_found(exc: BaseException) -> bool:
    """True when ``exc`` means "the thing you asked for does not exist".



    A genuine absence is a normal result and may be reported as ``None``/``False``.

    Anything else is a real failure and must propagate, so that callers can never

    confuse an unreachable backend with an empty collection.

    """

    if getattr(exc, "status_code", None) == 404:
        return True

    if getattr(getattr(exc, "response", None), "status_code", None) == 404:
        return True

    return type(exc).__name__ in _NOT_FOUND_NAMES


def _sql_literal(value: str) -> str:
    """Escape a value for inline use in a SQL string literal.



    The Delta Lake backend no longer uses this: it binds ``:name`` parameters,

    which databricks-sql-connector 3.x supports natively. It stays for callers

    who build their own Databricks SQL and need a value inline.

    Doubling single quotes is the ANSI escape; backslashes are doubled because

    Spark SQL honours backslash escapes inside string literals, and control

    characters are rejected outright rather than silently mangled.

    """

    if not isinstance(value, str):
        value = str(value)

    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise ConfigurationError(
            "Identifier or value contains control characters and cannot be used in a query"
        )

    return value.replace("\\", "\\\\").replace("'", "''")


# ============================================================================
# THE BACKENDS, AND THE CONFIG
# ============================================================================
#
# INPUT   a choice of backend and its settings
# OUTPUT  the enum of backends; one configuration every backend reads its own
#         part of
#
# One config object, so a caller switches backends by changing one field.


class StorageBackend(str, Enum):
    """Available storage backends."""

    MEMORY = "memory"

    SQLITE = "sqlite"

    COSMOSDB = "cosmosdb"

    # No backend of its own. Plain PostgreSQL with pgvector is reached
    # through AURORA_POSTGRESQL, whose class speaks the same protocol;
    # create_storage says so rather than failing with a bare name.
    POSTGRESQL = "postgresql"

    LAKEBASE = "lakebase"  # Databricks Lakebase (PostgreSQL + pgvector)

    DELTA_LAKE = "delta_lake"  # Databricks Delta Lake + Unity Catalog

    OPENSEARCH = "opensearch"  # AWS OpenSearch Serverless

    AURORA_POSTGRESQL = "aurora_postgresql"  # AWS Aurora PostgreSQL with pgvector

    AZURE_SEARCH = "azure_search"  # Azure AI Search (vector, BM25, hybrid, semantic ranker)


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

    # OpenSearch config (AWS OpenSearch Serverless)

    opensearch_endpoint: Optional[str] = None  # e.g., "https://xxx.us-east-1.aoss.amazonaws.com"

    opensearch_region: str = "us-east-1"

    opensearch_service: str = "aoss"  # "aoss" for Serverless, "es" for managed

    opensearch_index_prefix: str = "vectrix"
    # Metadata paths to map as real, filterable index fields, path to kind:
    # "string", "strings", "number" or "boolean". A filter or a policy over
    # them then runs inside OpenSearch. Decided when the index is created.
    opensearch_filter_fields: Optional[dict] = None
    # The k-NN engine for new indexes. "nmslib" is what every index so far
    # was built with, and filters after the search; "lucene" and "faiss"
    # filter during it, which keeps recall under a selective filter.
    opensearch_knn_engine: str = "nmslib"
    #: How the cluster turns a cosine distance into a score: "half" is
    #: (2 - d) / 2, "reciprocal" is 1 / (1 + d). "auto" works it out from a hit.
    opensearch_score_formula: str = "auto"
    # Whose dense vectors the index holds: "vectrixdb", the collection's own
    # model; "bedrock", a Bedrock embedding model in its place; or "both",
    # side by side in one index and fused by rank. OpenSearch has nothing
    # that embeds a question for it here, so Bedrock is called from this
    # process at ingest and at query.
    opensearch_embeddings: str = "vectrixdb"
    # texts -> vectors for the Bedrock model, a vectrixdb.models.bedrock
    # BedrockEmbedder built around the host's client, and its dimensions.
    opensearch_embed_fn: Any = None
    opensearch_embed_dimensions: Optional[int] = None
    opensearch_vector_weights: Optional[dict] = None

    opensearch_aws_access_key_id: Optional[str] = None

    opensearch_aws_secret_access_key: Optional[str] = None

    opensearch_aws_session_token: Optional[str] = None

    # Aurora PostgreSQL config (AWS Aurora with pgvector)

    aurora_host: Optional[str] = None

    aurora_port: int = 5432

    aurora_database: str = "vectrixdb"

    aurora_user: Optional[str] = None

    aurora_password: Optional[str] = None

    aurora_ssl: bool = True

    aurora_schema: str = "public"

    # Azure AI Search config

    azure_search_endpoint: Optional[str] = None  # e.g. "https://<service>.search.windows.net"

    azure_search_key: Optional[str] = None  # admin key; None uses DefaultAzureCredential

    azure_search_index_prefix: str = "vectrix"  # index names: <prefix>-<collection>

    azure_search_semantic: bool = False  # add Azure's semantic ranker to text and hybrid queries

    # Metadata paths promoted to real, filterable index fields, so a filter or
    # an entitlement policy over them runs inside the service rather than over
    # the rows it returned. Path to kind: "string", "strings" (a list),
    # "number" or "boolean"; dotted paths work. Every collection index gets
    # the same fields. A policy whose rules all name promoted fields opens
    # with require_pushdown=True; one that does not is still POST.
    azure_search_filter_fields: Optional[dict] = None
    # Whose dense vectors the index holds. "vectrixdb": the collection's own
    # model, one field. "azure": an Azure OpenAI deployment, one field, and
    # the service embeds the question itself. "both": two fields, fused by
    # the service in one request. See AzureSearchStorage.
    azure_search_embeddings: str = "vectrixdb"
    # The Azure OpenAI deployment behind "azure" and "both": endpoint,
    # deployment, model, dimensions, and api_key when not keyless.
    azure_search_vectorizer: Optional[dict] = None
    # texts -> vectors for that deployment, used at ingest. Left out, one is
    # built from azure_search_vectorizer. Tests hand in a fake.
    azure_search_embed_fn: Any = None
    # {"vectrixdb": 1.0, "azure": 1.0}: how much each vector counts in the
    # service's fusion, where the service version supports weights.
    azure_search_vector_weights: Optional[dict] = None
    #: Report how similar each hit is, from 0 to 1. A hybrid search, and a
    #: search over two vectors, come back from the service as fused ranks
    #: with no similarity in them, so this costs one more vector query there.
    azure_search_relevance: bool = True

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
            delta_workspace_url=os.getenv("DATABRICKS_HOST")
            or os.getenv("VECTRIX_DELTA_WORKSPACE_URL"),
            delta_token=os.getenv("DATABRICKS_TOKEN") or os.getenv("VECTRIX_DELTA_TOKEN"),
            delta_catalog=os.getenv("VECTRIX_DELTA_CATALOG", "main"),
            delta_schema=os.getenv("VECTRIX_DELTA_SCHEMA", "vectrixdb"),
            delta_warehouse_id=os.getenv("VECTRIX_DELTA_WAREHOUSE_ID"),
            delta_http_path=os.getenv("VECTRIX_DELTA_HTTP_PATH"),
        )


# ============================================================================
# A ROLE'S NAME
# ============================================================================
#
# INPUT   a role
# OUTPUT  validated and quoted for SQL, or refused
#
# A role name goes into a statement as an identifier, so it is checked like
# one, not like a value.

#: A PostgreSQL role name. Identifiers cannot be bind parameters, so this is
#: checked rather than parameterised, and the check is deliberately narrow:
#: letters, digits and underscores, starting with a letter or underscore.
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _quoted_role(role: str) -> str:
    """`role`, validated and quoted, or raise.

    The only part of this statement that is not a literal, and it cannot be
    bound, so it is validated instead. Anything that is not a plain
    identifier is refused rather than escaped: a role name with a quote in it
    is not a role name anybody meant to use.
    """
    if not isinstance(role, str) or not _ROLE_NAME.match(role):
        raise ValueError(
            f"{role!r} is not a usable PostgreSQL role name. It goes into the statement "
            f"as an identifier, which cannot be a bind parameter, so only letters, digits "
            f"and underscores are accepted."
        )
    return f'"{role}"'


# ============================================================================
# THE BASE
# ============================================================================
#
# INPUT   any backend
# OUTPUT  the methods every backend implements: collections, points, search,
#         filters, counts
#
# Abstract, so a store that misses one is caught at import rather than in
# production.


class BaseStorage(ABC):
    """Abstract base class for storage backends."""

    #: Where a filter runs on this backend, as VectrixDB actually drives it.
    #: Every backend is POST today: the collection calls ``vector_search``
    #: with a collection, a vector and a limit and no filter, so rows come
    #: back and are filtered afterwards.
    #:
    #: A backend that starts pushing filters into its query changes this to
    #: ENGINE, and ``test_pushdown.py`` is what stops the declaration and the
    #: behaviour drifting apart.
    FILTER_PUSHDOWN: ClassVar[FilterPushdown] = FilterPushdown.POST

    def vector_names(self) -> tuple:
        """Which dense vectors this store holds per document, by name. Empty
        for a store with the one unnamed vector every backend has."""
        return ()

    def named_embedders(self) -> dict:
        """``{name: (label, texts -> vectors)}`` for every vector the store
        holds beyond the collection's own, so the caller can embed them
        through its cache and hand them over with ``stage_named_vectors``."""
        return {}

    def filter_fields(self, collection: str) -> frozenset:
        """The metadata paths a filter can run on inside this store.

        Empty for a store that hands rows back for the library to filter. A
        backend that names fields here also accepts ``filter=`` on its
        search methods, carrying whatever ``compile_filter`` returned, and
        the collection reports ENGINE pushdown for a policy whose rules all
        name these fields.
        """
        return frozenset()

    def compile_filter(self, collection: str, filter_dict: dict) -> Optional[Any]:
        """This filter in the store's own language, or None if it cannot run there."""
        return None

    #: Whether this backend can run a read as a named database role, so the
    #: store enforces rather than the library. Only the PostgreSQL backends
    #: can; everywhere else `session_role` is a no-op and asking for a role
    #: raises rather than pretending.
    SUPPORTS_SESSION_ROLE: ClassVar[bool] = False

    @contextmanager
    def session_role(self, role: Optional[str]):
        """Run the block with the connection assuming ``role``.

        A no-op on a backend that cannot do it, and an error if a role is
        asked for anyway: silently not assuming it would mean the caller
        believes the database is enforcing when nothing is.
        """
        if role is not None and not self.SUPPORTS_SESSION_ROLE:
            raise ValueError(
                f"{type(self).__name__} cannot run a read as a database role, and a "
                f"policy asked for {role!r}. Row level security needs one of the "
                f"PostgreSQL backends; anywhere else the policy filter is the only "
                f"control and should not be mistaken for enforcement."
            )
        yield

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
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
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


# ============================================================================
# IN MEMORY
# ============================================================================
#
# INPUT   nothing
# OUTPUT  the fastest backend, with adaptive schema support and no persistence
#
# For tests and caches.


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
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:

        with self._lock:
            if collection not in self._collections:
                return

            items = list(self._collections[collection].items())

            if filter_func:
                items = [(k, v) for k, v in items if filter_func(v)]

            for item in items[offset : offset + limit]:
                yield item

    def count(self, collection: str) -> int:

        with self._lock:
            return len(self._collections.get(collection, {}))

    def flush(self) -> None:

        pass  # No-op for in-memory

    def vector_search(
        self, collection: str, query_vector: List[float], limit: int = 10
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

                        result_data = {
                            k: v for k, v in data.items() if not k.endswith("_embedding")
                        }

                        results.append((id_, result_data, distance))

        results.sort(key=lambda x: x[2])

        return results[:limit]

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10,
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
                            result_data = {
                                k: v for k, v in data.items() if not k.endswith("_embedding")
                            }

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
        limit: int = 10,
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
                    "updated_at": utcnow().isoformat(),
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
                    "updated_at": utcnow().isoformat(),
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


# ============================================================================
# SQLITE
# ============================================================================
#
# INPUT   a file
# OUTPUT  local persistence in WAL mode, so a restart mid-write leaves a whole
#         file
#
# The default.


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

        # sqlite3 connection objects are not safe to use from several threads at

        # once. Sharing one across threads (with check_same_thread=False, as this

        # backend does) corrupts the connection's internal statement state and

        # surfaces as "InterfaceError: bad parameter or other API misuse".

        #

        # Each thread therefore gets its own connection to the same file. SQLite

        # in WAL mode is built for exactly this: concurrent readers alongside one

        # writer, with real transaction isolation between connections.

        #

        # `_all_connections` records every handle so that close(), flush() and

        # delete_collection() can reach the connections owned by other threads.

        self._local = threading.local()

        self._all_connections: List[Tuple[str, sqlite3.Connection]] = []

        self._lock = threading.RLock()

    @property
    def _connections(self) -> Dict[str, sqlite3.Connection]:
        """This thread's open connections, keyed by collection."""

        conns = getattr(self._local, "conns", None)

        if conns is None:
            conns = self._local.conns = {}

        return conns

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

                # timeout is the busy handler: wait for another process's
                # write lock instead of failing with "database is locked".
                conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)

                conn.row_factory = sqlite3.Row

                # Enable WAL mode for safe restarts. Switching journal modes
                # takes an exclusive lock, so skip it when already in WAL.

                if self.config.sqlite_wal_mode:
                    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    if str(current).lower() != "wal":
                        conn.execute("PRAGMA journal_mode=WAL")

                    conn.execute("PRAGMA synchronous=NORMAL")

                    conn.execute("PRAGMA wal_autocheckpoint=1000")

                # Performance optimizations

                conn.execute("PRAGMA cache_size=10000")

                conn.execute("PRAGMA temp_store=MEMORY")

                # Create the generic per-collection schema, but only for real

                # collections. The three internal databases in _INTERNAL_DATABASES

                # define their own tables; giving

                # them this one meant "_documents" already had an incompatible

                # `documents` table, so ensure_document_tables()'s CREATE TABLE IF

                # NOT EXISTS did nothing and its index then failed with

                # "no such column: doc_type", disabling the document index entirely.

                if collection not in _INTERNAL_DATABASES:
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
                if collection not in _INTERNAL_DATABASES:
                    self._ensure_vector_columns(conn)

                self._connections[collection] = conn

                with self._lock:
                    self._all_connections.append((collection, conn))

            return self._connections[collection]

    def close(self) -> None:

        with self._lock:
            for _name, conn in self._all_connections:
                # Checkpoint WAL before closing

                if self.config.sqlite_wal_mode:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                    except sqlite3.Error as exc:
                        logger.debug("WAL checkpoint before close failed: %s", exc)

                conn.close()

            self._all_connections.clear()

            self._connections.clear()

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:

        main_db = self._get_connection("_meta")

        now = utcnow().isoformat()

        main_db.execute(
            "INSERT OR REPLACE INTO collections (name, config, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(config), now, now),
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
            for entry in [e for e in self._all_connections if e[0] == name]:
                entry[1].close()

                self._all_connections.remove(entry)

            self._connections.pop(name, None)

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

    # ------------------------------------------------------------------
    # Vector columns
    # ------------------------------------------------------------------

    _VECTOR_KEYS = ("_embedding", "dense_embedding")
    _LATE_KEY = "late_interaction_embedding"

    @staticmethod
    def _ensure_vector_columns(conn: sqlite3.Connection) -> None:
        """Add the blob columns to a `documents` table that predates them."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "vector" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN vector BLOB")
        if "late" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN late BLOB")
        conn.commit()

    @staticmethod
    def _pack_vector(vector: Any) -> Optional[bytes]:
        if vector is None:
            return None
        return np.asarray(vector, dtype=np.float32).ravel().tobytes()

    @staticmethod
    def _unpack_vector(blob: Optional[bytes]) -> Optional[List[float]]:
        if not blob:
            return None
        return np.frombuffer(blob, dtype=np.float32).tolist()

    @staticmethod
    def _pack_late(matrix: Any) -> Optional[bytes]:
        """A (tokens, dim) float32 matrix behind a two-uint32 shape header."""
        if matrix is None:
            return None
        arr = np.asarray(matrix, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        return struct.pack("<II", *arr.shape) + arr.tobytes()

    @staticmethod
    def _unpack_late(blob: Optional[bytes]) -> Optional[List[List[float]]]:
        if not blob:
            return None
        rows, cols = struct.unpack("<II", blob[:8])
        return np.frombuffer(blob[8:], dtype=np.float32).reshape(rows, cols).tolist()

    @classmethod
    def _split(
        cls, data: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Optional[bytes], Optional[bytes]]:
        """Pull the vector fields out of a document, for the blob columns."""
        rest = dict(data)
        vector = None
        for key in cls._VECTOR_KEYS:
            value = rest.pop(key, None)
            if value is not None and vector is None:
                vector = value
        late = rest.pop(cls._LATE_KEY, None)
        return rest, cls._pack_vector(vector), cls._pack_late(late)

    @classmethod
    def _rehydrate(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """A row back into the dict the rest of the package expects.

        Rows from before 2.2 carry the vector inside the JSON; those still
        read, since the blob is only consulted when present.
        """
        data = json.loads(row["data"])
        vector = cls._unpack_vector(row["vector"]) if "vector" in row.keys() else None
        if vector is not None:
            data["_embedding"] = vector
        late = cls._unpack_late(row["late"]) if "late" in row.keys() else None
        if late is not None:
            data[cls._LATE_KEY] = late
        return data

    def compact_vectors(self, collection: str) -> int:
        """Move JSON-stored vectors from earlier versions into the blob columns.

        Returns the number of rows rewritten. Safe to run repeatedly.
        """
        conn = self._get_connection(collection)
        rows = conn.execute(
            "SELECT id, data FROM documents WHERE vector IS NULL AND "
            "(data LIKE '%\"_embedding\"%' OR data LIKE '%\"dense_embedding\"%' "
            "OR data LIKE '%\"late_interaction_embedding\"%')"
        ).fetchall()
        moved = 0
        for row in rows:
            rest, vector, late = self._split(json.loads(row["data"]))
            if vector is None and late is None:
                continue
            conn.execute(
                "UPDATE documents SET data = ?, vector = ?, late = ? WHERE id = ?",
                (json.dumps(rest), vector, late, row["id"]),
            )
            moved += 1
        conn.commit()
        return moved

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        conn = self._get_connection(collection)
        now = utcnow().isoformat()
        rest, vector, late = self._split(data)
        conn.execute(
            "INSERT OR REPLACE INTO documents (id, data, vector, late, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (id, json.dumps(rest), vector, late, now, now),
        )
        conn.commit()

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        conn = self._get_connection(collection)
        now = utcnow().isoformat()
        rows = []
        for id_, data in documents:
            rest, vector, late = self._split(data)
            rows.append((id_, json.dumps(rest), vector, late, now, now))
        conn.executemany(
            "INSERT OR REPLACE INTO documents (id, data, vector, late, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(documents)

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection(collection)
        row = conn.execute(
            "SELECT data, vector, late FROM documents WHERE id = ?", (id,)
        ).fetchone()
        if row:
            return self._rehydrate(row)
        return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        conn = self._get_connection(collection)
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(
            f"SELECT id, data, vector, late FROM documents WHERE id IN ({placeholders})", ids
        )
        results = {row["id"]: self._rehydrate(row) for row in cursor}
        return [results.get(id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        conn = self._get_connection(collection)
        now = utcnow().isoformat()
        # Get existing data and merge
        existing = self.get(collection, id)
        if existing:
            existing.update(data)
            rest, vector, late = self._split(existing)
            conn.execute(
                "UPDATE documents SET data = ?, vector = ?, late = ?, updated_at = ? WHERE id = ?",
                (json.dumps(rest), vector, late, now, id),
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
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        conn = self._get_connection(collection)
        if filter_func:
            # Need to scan all and filter in Python
            cursor = conn.execute("SELECT id, data, vector, late FROM documents ORDER BY rowid")
            count = 0
            skipped = 0
            for row in cursor:
                data = self._rehydrate(row)
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
                "SELECT id, data, vector, late FROM documents ORDER BY rowid LIMIT ? OFFSET ?",
                (limit, offset),
            )
            for row in cursor:
                yield (row["id"], self._rehydrate(row))

    def count(self, collection: str) -> int:

        conn = self._get_connection(collection)

        row = conn.execute("SELECT COUNT(*) as cnt FROM documents").fetchone()

        return row["cnt"]

    def flush(self) -> None:

        with self._lock:
            for _name, conn in self._all_connections:
                conn.commit()

                if self.config.sqlite_wal_mode:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

                    except sqlite3.Error as exc:
                        logger.debug("WAL checkpoint on flush failed: %s", exc)

    # =========================================================================

    # Vector Search Methods (Adaptive Schema Support)

    # =========================================================================

    def vector_search(
        self, collection: str, query_vector: List[float], limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Dense vector search by cosine, over the blob column in one NumPy pass.

        This is the fallback for when the ANN index is empty; it used to walk
        every row in Python and sum products in a loop.
        """
        conn = self._get_connection(collection)
        rows = conn.execute("SELECT id, data, vector FROM documents ORDER BY rowid").fetchall()
        if not rows:
            return []

        ids: List[str] = []
        payloads: List[Dict[str, Any]] = []
        vectors: List[np.ndarray] = []
        for row in rows:
            data = json.loads(row["data"])
            if row["vector"]:
                vec = np.frombuffer(row["vector"], dtype=np.float32)
            else:
                legacy = data.pop("dense_embedding", None) or data.pop("_embedding", None)
                if not legacy:
                    continue
                vec = np.asarray(legacy, dtype=np.float32)
            ids.append(row["id"])
            payloads.append({k: v for k, v in data.items() if not k.endswith("_embedding")})
            vectors.append(vec)

        if not vectors:
            return []
        matrix = np.vstack(vectors)
        query = np.asarray(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(query)
        norms = np.linalg.norm(matrix, axis=1)
        if q_norm == 0:
            return []
        valid = norms > 0
        similarity = np.zeros(len(ids), dtype=np.float32)
        similarity[valid] = (matrix[valid] @ query) / (norms[valid] * q_norm)
        order = np.argsort(-similarity)[:limit]
        return [(ids[i], payloads[i], float(1.0 - similarity[i])) for i in order if valid[i]]

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10,
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
        limit: int = 10,
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

            late_interaction_emb = (
                full_data.get("late_interaction_embedding") if full_data else None
            )

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

        # Databases created before 2.2.0 have a generic `documents` table here

        # (id, data, created_at, updated_at) left by _get_connection. It shadows

        # the real schema below and breaks the document index. It is never

        # written to, so drop it when it is empty; if somehow it is not, leave it

        # alone and say so rather than destroying data.

        columns = {row[1] for row in doc_conn.execute("PRAGMA table_info(documents)").fetchall()}

        if columns and "doc_type" not in columns:
            rows = doc_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

            if rows == 0:
                doc_conn.execute("DROP TABLE documents")

                doc_conn.commit()

                logger.info("Replaced the legacy placeholder `documents` table in _documents.db")

            else:
                raise StorageOperationError(
                    "ensure_document_tables",
                    "SQLite",
                    f"_documents.db holds a legacy `documents` table with {rows} rows and no "
                    "doc_type column. Back it up and drop it to enable the document index.",
                )

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

        now = utcnow().isoformat()

        conn.execute(
            """

            INSERT OR REPLACE INTO documents

            (doc_id, title, doc_type, source_path, etag, content_hash,

             page_count, section_count, node_count, indexed_at, last_synced,

             metadata, created_at, updated_at)

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        """,
            (
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
            ),
        )

        conn.commit()

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""

        # The document tables are created lazily, so a store that has never
        # had a document written to it had no `documents` table and this
        # raised sqlite3.OperationalError: a raw driver error, out of a read
        # whose honest answer is "there are none". ensure_document_tables is
        # CREATE TABLE IF NOT EXISTS, so calling it here costs nothing.
        self.ensure_document_tables()

        conn = self._get_connection("_documents")

        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()

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

        # The document tables are created lazily, so a store that has never
        # had a document written to it had no `documents` table and this
        # raised sqlite3.OperationalError: a raw driver error, out of a read
        # whose honest answer is "there are none". ensure_document_tables is
        # CREATE TABLE IF NOT EXISTS, so calling it here costs nothing.
        self.ensure_document_tables()

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

        # Same reason as the readers above: deleting from a store that never
        # held a document is "there was nothing to delete", not a driver error.
        self.ensure_document_tables()

        conn = self._get_connection("_documents")

        cursor = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

        conn.commit()

        return cursor.rowcount > 0

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""

        conn = self._get_connection("_nodes")

        now = utcnow().isoformat()

        conn.execute(
            """

            INSERT OR REPLACE INTO nodes

            (node_id, doc_id, parent_id, level, title, text, summary,

             page_num, position, metadata, created_at, updated_at)

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        """,
            (
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
            ),
        )

        conn.commit()

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""

        conn = self._get_connection("_nodes")

        row = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()

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

        cursor = conn.execute("SELECT * FROM nodes WHERE doc_id = ? ORDER BY position", (doc_id,))

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
            "SELECT * FROM nodes WHERE parent_id = ? ORDER BY position", (parent_id,)
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


# ============================================================================
# AZURE COSMOS DB
# ============================================================================
#
# INPUT   an account, a database and a container
# OUTPUT  cloud-scale persistence; items read back without the fields Cosmos
#         itself adds
#
# The bookkeeping fields Cosmos adds are stripped, so a document round-trips
# as it was stored.
# What Cosmos adds to every document it stores. Everything else in an item
# belongs to the caller, underscore or not.

_COSMOS_SYSTEM_FIELDS = frozenset(
    {"_rid", "_self", "_etag", "_attachments", "_ts", "_lsn", "_metadata"}
)


def _without_cosmos_bookkeeping(item: Dict[str, Any]) -> Dict[str, Any]:
    """A Cosmos item without the fields Cosmos itself adds.

    This used to drop every key beginning with an underscore, which took the
    caller's ``_embedding`` with it. Collection keeps the vector under that
    name, so a document written to Cosmos came back without one, and
    vector_search, which reads through scan, returned nothing for every
    query on every collection.
    """
    return {k: v for k, v in item.items() if k not in _COSMOS_SYSTEM_FIELDS}


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

        self._client: Any = None

        self._database: Any = None

        self._containers: Dict[str, Any] = {}

    def connect(self) -> None:

        try:
            from azure.cosmos import CosmosClient, PartitionKey

            from azure.cosmos.exceptions import CosmosResourceExistsError

        except ImportError:
            raise ImportError("azure-cosmos is required. Install with: pip install azure-cosmos")

        if not self.config.cosmos_endpoint or not self.config.cosmos_key:
            raise ValueError("Cosmos DB endpoint and key are required")

        self._client = CosmosClient(self.config.cosmos_endpoint, self.config.cosmos_key)

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
                offer_throughput=self.config.cosmos_throughput,
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
                offer_throughput=self.config.cosmos_throughput,
            )

            self._containers[name] = container

        except CosmosResourceExistsError:
            self._containers[name] = self._database.get_container_client(name)

        # Store metadata

        meta_container = self._get_container("_meta")

        meta_container.upsert_item(
            {"id": name, "type": "collection", "config": config, "created_at": utcnow().isoformat()}
        )

    def delete_collection(self, name: str) -> None:

        try:
            self._database.delete_container(name)

            self._containers.pop(name, None)

        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError("delete_collection", "CosmosDB", str(exc)) from exc

        # Remove metadata

        try:
            meta_container = self._get_container("_meta")

            meta_container.delete_item(item=name, partition_key="collection")

        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError(
                    "delete_collection_metadata", "CosmosDB", str(exc)
                ) from exc

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

        except Exception as exc:
            if _is_not_found(exc):
                return None

            raise StorageOperationError("get_collection_config", "CosmosDB", str(exc)) from exc

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:

        container = self._get_container(collection)

        item = {
            "id": id,
            "partition_key": id[:2] if len(id) >= 2 else id,  # Simple partition strategy
            **data,
            "created_at": utcnow().isoformat(),
        }

        container.upsert_item(item)

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:

        container = self._get_container(collection)

        count = 0

        now = utcnow().isoformat()

        # Cosmos DB doesn't have native batch insert, but we can use parallel operations

        for id, data in documents:
            item = {
                "id": id,
                "partition_key": id[:2] if len(id) >= 2 else id,
                **data,
                "created_at": now,
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

            return _without_cosmos_bookkeeping(item)

        except Exception as exc:
            if _is_not_found(exc):
                return None

            raise StorageOperationError("get", "CosmosDB", str(exc)) from exc

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:

        return [self.get(collection, id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:

        existing = self.get(collection, id)

        if existing:
            existing.update(data)

            existing["updated_at"] = utcnow().isoformat()

            self.insert(collection, id, existing)

            return True

        return False

    def delete(self, collection: str, id: str) -> bool:

        try:
            container = self._get_container(collection)

            partition_key = id[:2] if len(id) >= 2 else id

            container.delete_item(item=id, partition_key=partition_key)

            return True

        except Exception as exc:
            if _is_not_found(exc):
                return False

            raise StorageOperationError("delete", "CosmosDB", str(exc)) from exc

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
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:

        container = self._get_container(collection)

        query = "SELECT * FROM c ORDER BY c._ts OFFSET @offset LIMIT @limit"

        params = [{"name": "@offset", "value": offset}, {"name": "@limit", "value": limit}]

        items = container.query_items(
            query=query, parameters=params, enable_cross_partition_query=True
        )

        for item in items:
            data = _without_cosmos_bookkeeping(item)

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
        self, collection: str, query_vector: List[float], limit: int = 10
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
        limit: int = 10,
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
        limit: int = 10,
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

            late_interaction_emb = (
                full_data.get("late_interaction_embedding") if full_data else None
            )

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
        """Create document and node containers for graph mode, once.

        save_document and save_node call this on every write, so it returns
        at once when both are open: asking Cosmos to create them again cost
        two requests answered 409 for every document and node written.
        """
        if "_documents" in self._containers and "_nodes" in self._containers:
            return

        from azure.cosmos import PartitionKey

        from azure.cosmos.exceptions import CosmosResourceExistsError

        # Documents container

        try:
            self._containers["_documents"] = self._database.create_container(
                id="_documents",
                partition_key=PartitionKey(path="/doc_type"),
                offer_throughput=self.config.cosmos_throughput,
            )

        except CosmosResourceExistsError:
            self._containers["_documents"] = self._database.get_container_client("_documents")

        # Nodes container

        try:
            self._containers["_nodes"] = self._database.create_container(
                id="_nodes",
                partition_key=PartitionKey(path="/doc_id"),
                offer_throughput=self.config.cosmos_throughput,
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
            "updated_at": utcnow().isoformat(),
        }

        container.upsert_item(item)

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""

        try:
            container = self._get_container("_documents")

            # Query across partitions since we don't know doc_type

            items = list(
                container.query_items(
                    query="SELECT * FROM c WHERE c.id = @doc_id",
                    parameters=[{"name": "@doc_id", "value": doc_id}],
                    enable_cross_partition_query=True,
                )
            )

            if items:
                return _without_cosmos_bookkeeping(items[0])

        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError("get_document", "CosmosDB", str(exc)) from exc

        return None

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents."""

        try:
            container = self._get_container("_documents")

            query = "SELECT * FROM c"

            items = container.query_items(query, enable_cross_partition_query=True)

            return [_without_cosmos_bookkeeping(item) for item in items]

        except Exception as exc:
            raise StorageOperationError("list_documents", "CosmosDB", str(exc)) from exc

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document."""

        try:
            doc = self.get_document(doc_id)

            if doc:
                container = self._get_container("_documents")

                container.delete_item(item=doc_id, partition_key=doc.get("doc_type", "text"))

                return True

        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError("delete_document", "CosmosDB", str(exc)) from exc

        return False

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""

        self.ensure_document_tables()

        container = self._get_container("_nodes")

        item = {
            "id": node_data.get("node_id"),
            "doc_id": node_data.get("doc_id"),
            **node_data,
            "updated_at": utcnow().isoformat(),
        }

        container.upsert_item(item)

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""

        try:
            container = self._get_container("_nodes")

            items = list(
                container.query_items(
                    query="SELECT * FROM c WHERE c.id = @node_id",
                    parameters=[{"name": "@node_id", "value": node_id}],
                    enable_cross_partition_query=True,
                )
            )

            if items:
                return _without_cosmos_bookkeeping(items[0])

        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError("get_node", "CosmosDB", str(exc)) from exc

        return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document."""

        try:
            container = self._get_container("_nodes")

            items = container.query_items(
                query="SELECT * FROM c WHERE c.doc_id = @doc_id ORDER BY c.position",
                parameters=[{"name": "@doc_id", "value": doc_id}],
                partition_key=doc_id,
            )

            return [_without_cosmos_bookkeeping(item) for item in items]

        except Exception as exc:
            raise StorageOperationError("get_document_nodes", "CosmosDB", str(exc)) from exc

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent."""

        try:
            container = self._get_container("_nodes")

            items = container.query_items(
                query="SELECT * FROM c WHERE c.parent_id = @parent_id ORDER BY c.position",
                parameters=[{"name": "@parent_id", "value": parent_id}],
                enable_cross_partition_query=True,
            )

            return [_without_cosmos_bookkeeping(item) for item in items]

        except Exception as exc:
            raise StorageOperationError("get_child_nodes", "CosmosDB", str(exc)) from exc

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document."""

        try:
            container = self._get_container("_nodes")

            nodes = self.get_document_nodes(doc_id)

            for node in nodes:
                container.delete_item(item=node["node_id"], partition_key=doc_id)

            return len(nodes)

        except Exception as exc:
            raise StorageOperationError("delete_document_nodes", "CosmosDB", str(exc)) from exc


# ============================================================================
# DATABRICKS LAKEBASE
# ============================================================================
#
# INPUT   a PostgreSQL connection with pgvector
# OUTPUT  a pgvector column read back as floats; vectors and metadata in
#         Lakebase
#
# PostgreSQL underneath, so the Aurora backend and this one share most of
# their SQL.


def _as_float_list(value: Any) -> Optional[List[float]]:
    """A pgvector column as a list of floats.

    psycopg returns the column as a list when pgvector's adapter is
    registered and as the text "[1,2,3]" when it is not, so both shapes are
    accepted here rather than assuming one.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return [float(part) for part in value.strip("[]").split(",") if part.strip()]
        except ValueError:
            return None
    try:
        return [float(part) for part in value]
    except (TypeError, ValueError):
        return None


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

    SUPPORTS_SESSION_ROLE: ClassVar[bool] = True

    #: What a DBA runs once, so there is something for the role to be refused
    #: by. VectrixDB never issues this: a connection that can create a policy
    #: can drop one.
    RLS_SETUP = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

-- One policy, in SQL, owned by the database. Shape it to your entitlements;
-- what matters is that it is evaluated by PostgreSQL for current_user rather
-- than by the application asking nicely.
CREATE POLICY {table}_read ON {table}
    FOR SELECT
    USING (
        metadata->'entitlements'->>'lob' = current_setting('vectrix.lob', true)
    );

GRANT SELECT ON {table} TO {role};
"""

    @contextmanager
    def session_role(self, role: Optional[str]):
        """Assume ``role`` for one transaction, then give it back.

        ``SET LOCAL`` rather than ``SET``: it lasts until the transaction
        ends and no longer, so the pooled connection handed to the next
        request has already forgotten it. A role that outlived the request
        would be worse than none, because the next caller would run as
        somebody else.
        """
        if role is None:
            yield
            return

        conn = self._conn
        if conn is None:
            raise RuntimeError("not connected")

        quoted = _quoted_role(role)
        # SET LOCAL outside a transaction applies to nothing, and PostgreSQL
        # mentions it only in a notice. This backend may well be in
        # autocommit, where psycopg2 manages no transaction of its own, so
        # one is opened explicitly rather than assumed.
        explicit = bool(getattr(conn, "autocommit", False))
        with conn.cursor() as cur:
            if explicit:
                cur.execute("BEGIN")
            cur.execute(f"SET LOCAL ROLE {quoted}")
        try:
            yield
        finally:
            # Ending the transaction is what gives the role back, which is
            # the reason for SET LOCAL: the next request on this pooled
            # connection must not inherit it. A read has nothing to keep, so
            # roll back rather than commit.
            if explicit:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK")
            else:
                conn.rollback()

    def __init__(self, config: StorageConfig):

        self.config = config

        self._conn: Any = None

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

    def _ensure_collection_table(
        self, name: str, dimension: Optional[int] = None, mode: str = "dense"
    ) -> None:
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

                cur.execute(
                    """

                    SELECT column_name FROM information_schema.columns

                    WHERE table_schema = %s AND table_name = %s

                """,
                    (
                        schema,
                        name,
                    ),
                )

                existing_columns = {row["column_name"] for row in cur.fetchall()}

                # If table exists but missing dense_embedding, drop and recreate

                if existing_columns and "dense_embedding" not in existing_columns:
                    cur.execute(f"DROP TABLE IF EXISTS {table_ref} CASCADE")

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
                        cur.execute(
                            f"ALTER TABLE {table_ref} ADD COLUMN dense_embedding vector({dimension})"
                        )

                    if "sparse_embedding" not in existing_columns:
                        cur.execute(f"ALTER TABLE {table_ref} ADD COLUMN sparse_embedding JSONB")

                    if "late_interaction_embedding" not in existing_columns:
                        cur.execute(
                            f"ALTER TABLE {table_ref} ADD COLUMN late_interaction_embedding JSONB"
                        )

                    if "metadata" not in existing_columns:
                        cur.execute(f"ALTER TABLE {table_ref} ADD COLUMN metadata JSONB")

                    if "text_content" not in existing_columns:
                        cur.execute(f"ALTER TABLE {table_ref} ADD COLUMN text_content TEXT")

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
                cur.execute(
                    f"""

                    INSERT INTO {collections_table} (name, dimension, description, config, updated_at)

                    VALUES (%s, %s, %s, %s, NOW())

                    ON CONFLICT (name) DO UPDATE SET

                        dimension = %s,

                        description = %s,

                        config = %s,

                        updated_at = NOW()

                """,
                    (
                        name,
                        dimension,
                        description,
                        json.dumps(config),
                        dimension,
                        description,
                        json.dumps(config),
                    ),
                )

                self._conn.commit()

            self._ensure_collection_table(name, dimension=dimension, mode=mode)

    def delete_collection(self, name: str) -> None:

        schema = self.config.lakebase_schema or "public"

        table_ref = f'"{schema}"."{name}"'

        collections_table = self._collections_table_ref()

        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {collections_table} WHERE name = %s", (name,))

                cur.execute(f"DROP TABLE IF EXISTS {table_ref}")

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

        late_interaction_json = (
            json.dumps(
                [e.tolist() if hasattr(e, "tolist") else e for e in late_interaction_embedding]
            )
            if late_interaction_embedding
            else None
        )

        with self._lock:
            with self._conn.cursor() as cur:
                # Check which columns exist

                schema = self.config.lakebase_schema or "public"

                table_ref = f'"{schema}"."{collection}"'

                cur.execute(
                    f"""

                    SELECT column_name FROM information_schema.columns

                    WHERE table_schema = %s AND table_name = %s

                """,
                    (
                        schema,
                        collection,
                    ),
                )

                columns = {row["column_name"] for row in cur.fetchall()}

                # Build dynamic INSERT based on available columns

                if "sparse_embedding" in columns and "late_interaction_embedding" in columns:
                    cur.execute(
                        f"""

                        INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, late_interaction_embedding, metadata, text_content, updated_at)

                        VALUES (%s, %s::vector, %s::jsonb, %s::jsonb, %s, %s, NOW())

                        ON CONFLICT (id) DO UPDATE SET

                            dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),

                            sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),

                            late_interaction_embedding = COALESCE(%s::jsonb, {table_ref}.late_interaction_embedding),

                            metadata = %s,

                            text_content = COALESCE(%s, {table_ref}.text_content),

                            updated_at = NOW()

                    """,
                        (
                            id,
                            dense_str,
                            sparse_json,
                            late_interaction_json,
                            json.dumps(metadata),
                            text_content,
                            dense_str,
                            sparse_json,
                            late_interaction_json,
                            json.dumps(metadata),
                            text_content,
                        ),
                    )

                elif "sparse_embedding" in columns:
                    cur.execute(
                        f"""

                        INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, metadata, text_content, updated_at)

                        VALUES (%s, %s::vector, %s::jsonb, %s, %s, NOW())

                        ON CONFLICT (id) DO UPDATE SET

                            dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),

                            sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),

                            metadata = %s,

                            text_content = COALESCE(%s, {table_ref}.text_content),

                            updated_at = NOW()

                    """,
                        (
                            id,
                            dense_str,
                            sparse_json,
                            json.dumps(metadata),
                            text_content,
                            dense_str,
                            sparse_json,
                            json.dumps(metadata),
                            text_content,
                        ),
                    )

                else:
                    cur.execute(
                        f"""

                        INSERT INTO {table_ref} (id, dense_embedding, metadata, text_content, updated_at)

                        VALUES (%s, %s::vector, %s, %s, NOW())

                        ON CONFLICT (id) DO UPDATE SET

                            dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),

                            metadata = %s,

                            text_content = COALESCE(%s, {table_ref}.text_content),

                            updated_at = NOW()

                    """,
                        (
                            id,
                            dense_str,
                            json.dumps(metadata),
                            text_content,
                            dense_str,
                            json.dumps(metadata),
                            text_content,
                        ),
                    )

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

                cur.execute(
                    f"""

                    SELECT column_name FROM information_schema.columns

                    WHERE table_schema = %s AND table_name = %s

                """,
                    (
                        schema,
                        collection,
                    ),
                )

                columns = {row["column_name"] for row in cur.fetchall()}

                has_sparse = "sparse_embedding" in columns

                has_late_interaction = "late_interaction_embedding" in columns

                count = 0

                for id, data in documents:
                    # Extract special fields

                    dense_embedding = data.pop("_embedding", None) or data.pop(
                        "dense_embedding", None
                    )

                    sparse_embedding = data.pop("sparse_embedding", None)

                    late_interaction_embedding = data.pop("late_interaction_embedding", None)

                    text_content = data.pop("text_content", None)

                    metadata = data  # Remaining fields are metadata

                    # Convert embeddings to proper format

                    dense_str = (
                        f"[{','.join(map(str, dense_embedding))}]" if dense_embedding else None
                    )

                    sparse_json = json.dumps(sparse_embedding) if sparse_embedding else None

                    late_interaction_json = (
                        json.dumps(
                            [
                                e.tolist() if hasattr(e, "tolist") else e
                                for e in late_interaction_embedding
                            ]
                        )
                        if late_interaction_embedding
                        else None
                    )

                    if has_sparse and has_late_interaction:
                        cur.execute(
                            f"""

                            INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, late_interaction_embedding, metadata, text_content, updated_at)

                            VALUES (%s, %s::vector, %s::jsonb, %s::jsonb, %s, %s, NOW())

                            ON CONFLICT (id) DO UPDATE SET

                                dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),

                                sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),

                                late_interaction_embedding = COALESCE(%s::jsonb, {table_ref}.late_interaction_embedding),

                                metadata = %s,

                                text_content = COALESCE(%s, {table_ref}.text_content),

                                updated_at = NOW()

                        """,
                            (
                                id,
                                dense_str,
                                sparse_json,
                                late_interaction_json,
                                json.dumps(metadata),
                                text_content,
                                dense_str,
                                sparse_json,
                                late_interaction_json,
                                json.dumps(metadata),
                                text_content,
                            ),
                        )

                    elif has_sparse:
                        cur.execute(
                            f"""

                            INSERT INTO {table_ref} (id, dense_embedding, sparse_embedding, metadata, text_content, updated_at)

                            VALUES (%s, %s::vector, %s::jsonb, %s, %s, NOW())

                            ON CONFLICT (id) DO UPDATE SET

                                dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),

                                sparse_embedding = COALESCE(%s::jsonb, {table_ref}.sparse_embedding),

                                metadata = %s,

                                text_content = COALESCE(%s, {table_ref}.text_content),

                                updated_at = NOW()

                        """,
                            (
                                id,
                                dense_str,
                                sparse_json,
                                json.dumps(metadata),
                                text_content,
                                dense_str,
                                sparse_json,
                                json.dumps(metadata),
                                text_content,
                            ),
                        )

                    else:
                        cur.execute(
                            f"""

                            INSERT INTO {table_ref} (id, dense_embedding, metadata, text_content, updated_at)

                            VALUES (%s, %s::vector, %s, %s, NOW())

                            ON CONFLICT (id) DO UPDATE SET

                                dense_embedding = COALESCE(%s::vector, {table_ref}.dense_embedding),

                                metadata = %s,

                                text_content = COALESCE(%s, {table_ref}.text_content),

                                updated_at = NOW()

                        """,
                            (
                                id,
                                dense_str,
                                json.dumps(metadata),
                                text_content,
                                dense_str,
                                json.dumps(metadata),
                                text_content,
                            ),
                        )

                    count += 1

                self._conn.commit()

                return count

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:

        table_ref = self._table_ref(collection)

        try:
            with self._conn.cursor() as cur:
                # dense_embedding is selected too. Without it a document
                # written with a vector came back without one, so reading a
                # point back never returned what was stored.
                cur.execute(
                    f"SELECT metadata, text_content, dense_embedding "
                    f"FROM {table_ref} WHERE id = %s",
                    (id,),
                )

                row = cur.fetchone()

                if row:
                    result = dict(row["metadata"] or {})

                    if row["text_content"]:
                        result["text_content"] = row["text_content"]

                    if row["dense_embedding"] is not None:
                        result["dense_embedding"] = _as_float_list(row["dense_embedding"])

                    return result

                return None

        except Exception as exc:
            raise StorageOperationError("get", "Lakebase", str(exc)) from exc

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:

        table_ref = self._table_ref(collection)

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, metadata, text_content FROM {table_ref} WHERE id = ANY(%s)", (ids,)
                )

                results = {}

                for row in cur.fetchall():
                    data = row["metadata"] or {}

                    if row["text_content"]:
                        data["text_content"] = row["text_content"]

                    results[row["id"]] = data

                return [results.get(id) for id in ids]

        except Exception as exc:
            raise StorageOperationError("get_batch", "Lakebase", str(exc)) from exc

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
                    cur.execute(
                        f"""

                        UPDATE {table_ref} SET

                            metadata = %s,

                            dense_embedding = COALESCE(%s::vector, dense_embedding),

                            text_content = COALESCE(%s, text_content),

                            updated_at = NOW()

                        WHERE id = %s

                    """,
                        (json.dumps(metadata), embedding, text_content, id),
                    )

                    self._conn.commit()

                    return cur.rowcount > 0

        return False

    def delete(self, collection: str, id: str) -> bool:

        table_ref = self._table_ref(collection)

        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table_ref} WHERE id = %s", (id,))

                self._conn.commit()

                return cur.rowcount > 0

    def delete_batch(self, collection: str, ids: List[str]) -> int:

        table_ref = self._table_ref(collection)

        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table_ref} WHERE id = ANY(%s)", (ids,))

                self._conn.commit()

                return cur.rowcount

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:

        table_ref = self._table_ref(collection)

        try:
            with self._conn.cursor() as cur:
                if filter_func:
                    # Fetch all and filter in Python

                    cur.execute(
                        f"SELECT id, metadata, text_content FROM {table_ref} ORDER BY created_at"
                    )

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
                        f"SELECT id, metadata, text_content FROM {table_ref} ORDER BY created_at LIMIT %s OFFSET %s",
                        (limit, offset),
                    )

                    for row in cur:
                        data = row["metadata"] or {}

                        if row["text_content"]:
                            data["text_content"] = row["text_content"]

                        yield (row["id"], data)

        except Exception as exc:
            raise StorageOperationError("scan", "Lakebase", str(exc)) from exc

    def count(self, collection: str) -> int:

        table_ref = self._table_ref(collection)

        try:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) as cnt FROM {table_ref}")

                row = cur.fetchone()

                return row["cnt"] if row else 0

        except Exception as exc:
            raise StorageOperationError("count", "Lakebase", str(exc)) from exc

    def flush(self) -> None:

        if self._conn:
            self._conn.commit()

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """

        Perform vector similarity search using pgvector (dense only).



        Args:

            collection: Collection name

            query_vector: Query embedding vector

            limit: Max results to return



        Returns:

            List of (id, metadata, distance) tuples ordered by similarity

        """
        table_ref = self._table_ref(collection)

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""

                    SELECT id, metadata, text_content, dense_embedding <=> %s::vector AS distance

                    FROM {table_ref}

                    WHERE dense_embedding IS NOT NULL

                    ORDER BY distance

                    LIMIT %s

                """,
                    (query_vector, limit),
                )

                results = []

                for row in cur.fetchall():
                    data = row["metadata"] or {}

                    if row["text_content"]:
                        data["text_content"] = row["text_content"]

                    results.append((row["id"], data, row["distance"]))

                return results

        except Exception as e:
            # This printed and returned [], so a connection failure, a
            # permission error and a genuinely empty result were the same
            # answer. A caller cannot tell those apart, and an empty result
            # is the one that looks normal.
            raise SearchError(f"vector search failed on {collection!r}: {e}") from e

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """

        Hybrid search using dense + sparse embeddings with RRF fusion.



        Args:

            collection: Collection name

            dense_query: Dense query vector

            sparse_query: Sparse query (token_id -> weight)

            limit: Max results to return



        Returns:

            List of (id, metadata, score) tuples

        """
        table_ref = self._table_ref(collection)

        try:
            with self._conn.cursor() as cur:
                prefetch = limit * 10

                # Dense search

                cur.execute(
                    f"""

                    SELECT id, metadata, text_content, dense_embedding <=> %s::vector AS distance

                    FROM {table_ref}

                    WHERE dense_embedding IS NOT NULL

                    ORDER BY distance

                    LIMIT %s

                """,
                    (dense_query, prefetch),
                )

                dense_results = [
                    (row["id"], row["metadata"], row["text_content"], row["distance"])
                    for row in cur.fetchall()
                ]

                # Sparse search (if sparse embeddings exist)

                cur.execute(
                    f"""

                    SELECT id, metadata, text_content, sparse_embedding

                    FROM {table_ref}

                    WHERE sparse_embedding IS NOT NULL

                    LIMIT %s

                """,
                    (prefetch,),
                )

                sparse_results = []

                for row in cur.fetchall():
                    doc_sparse = row["sparse_embedding"] or {}

                    # Compute sparse similarity (dot product of matching tokens)

                    score = sum(sparse_query.get(int(k), 0) * v for k, v in doc_sparse.items())

                    if score > 0:
                        sparse_results.append(
                            (row["id"], row["metadata"], row["text_content"], score)
                        )

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
            # This quietly downgraded to a dense-only search, so a caller who
            # asked for hybrid got dense results and was never told. Silently
            # answering a different question is worse than failing.
            raise SearchError(f"hybrid search failed on {collection!r}: {e}") from e

    def ultimate_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        late_interaction_query: List[List[float]],
        limit: int = 10,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """

        Ultimate search using dense + sparse + late interaction embeddings.



        Args:

            collection: Collection name

            dense_query: Dense query vector

            sparse_query: Sparse query (token_id -> weight)

            late_interaction_query: Late interaction query (list of token vectors)

            limit: Max results to return



        Returns:

            List of (id, metadata, score) tuples

        """
        import numpy as np

        try:
            # First get hybrid results

            hybrid_results = self.hybrid_search(collection, dense_query, sparse_query, limit * 3)

            if not hybrid_results:
                return []

            # Get late interaction embeddings for candidates

            candidate_ids = [r[0] for r in hybrid_results]

            table_ref = self._table_ref(collection)

            with self._conn.cursor() as cur:
                cur.execute(
                    f"""

                    SELECT id, late_interaction_embedding

                    FROM {table_ref}

                    WHERE id = ANY(%s) AND late_interaction_embedding IS NOT NULL

                """,
                    (candidate_ids,),
                )

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
            # Same again, one level up: this downgraded to hybrid without a
            # word.
            raise SearchError(f"ultimate search failed on {collection!r}: {e}") from e

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
                cur.execute(
                    f"""

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

                """,
                    (
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
                    ),
                )

                self._conn.commit()

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""

        doc_table = self._doc_table_ref()

        try:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {doc_table} WHERE doc_id = %s", (doc_id,))

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
                        "last_synced": row["last_synced"].isoformat()
                        if row["last_synced"]
                        else None,
                        "metadata": row["metadata"] or {},
                    }

        except Exception as exc:
            raise StorageOperationError("get_document", "Lakebase", str(exc)) from exc

        return None

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents."""

        doc_table = self._doc_table_ref()

        try:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {doc_table} ORDER BY indexed_at DESC")

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
                        "last_synced": row["last_synced"].isoformat()
                        if row["last_synced"]
                        else None,
                        "metadata": row["metadata"] or {},
                    }
                    for row in cur.fetchall()
                ]

        except Exception as exc:
            raise StorageOperationError("list_documents", "Lakebase", str(exc)) from exc

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document."""

        doc_table = self._doc_table_ref()

        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {doc_table} WHERE doc_id = %s", (doc_id,))

                self._conn.commit()

                return cur.rowcount > 0

    def save_node(self, node_data: Dict[str, Any]) -> None:
        """Save a document node."""

        node_table = self._node_table_ref()

        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""

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

                """,
                    (
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
                    ),
                )

                self._conn.commit()

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get node by ID."""

        node_table = self._node_table_ref()

        try:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {node_table} WHERE node_id = %s", (node_id,))

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

        except Exception as exc:
            raise StorageOperationError("get_node", "Lakebase", str(exc)) from exc

        return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:
        """Get all nodes for a document."""

        node_table = self._node_table_ref()

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {node_table} WHERE doc_id = %s ORDER BY position", (doc_id,)
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

        except Exception as exc:
            raise StorageOperationError("get_document_nodes", "Lakebase", str(exc)) from exc

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:
        """Get child nodes of a parent."""

        node_table = self._node_table_ref()

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {node_table} WHERE parent_id = %s ORDER BY position",
                    (parent_id,),
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

        except Exception as exc:
            raise StorageOperationError("get_child_nodes", "Lakebase", str(exc)) from exc

    def delete_document_nodes(self, doc_id: str) -> int:
        """Delete all nodes for a document."""

        node_table = self._node_table_ref()

        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f"DELETE FROM {node_table} WHERE doc_id = %s", (doc_id,))

                self._conn.commit()

                return cur.rowcount


# ============================================================================
# DATABRICKS DELTA LAKE
# ============================================================================
#
# INPUT   a Unity Catalog table
# OUTPUT  storage in Delta Lake, governed by Unity Catalog
#
# The governed store; the sync module rebuilds a fast target from it.


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

        self._conn: Any = None

        self._cursor: Any = None

        self._lock = threading.RLock()

        self._catalog = config.delta_catalog

        self._schema = config.delta_schema

    def _full_table_name(self, table: str) -> str:
        """Fully qualified, backtick-quoted ``catalog``.``schema``.``table``.

        Identifiers cannot be bound as parameters, so this is the one place a
        caller-supplied string still enters statement text. Inside backticks
        the only special character is the backtick itself, which Databricks
        escapes by doubling; control characters are refused outright.
        """

        def quote(identifier: str) -> str:
            if any(ch in identifier for ch in ("\x00", "\r", "\n")):
                raise ConfigurationError(
                    "Identifier contains control characters and cannot be used in a query"
                )
            return "`" + identifier.replace("`", "``") + "`"

        return f"{quote(self._catalog)}.{quote(self._schema)}.{quote(table)}"

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

        server_hostname = (
            self.config.delta_workspace_url.replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )

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

            CREATE TABLE IF NOT EXISTS {self._full_table_name("_vectrix_collections")} (

                name STRING NOT NULL,

                config STRING,

                created_at TIMESTAMP,

                updated_at TIMESTAMP

            ) USING DELTA

        """)

        self._cursor.execute(f"""

            CREATE TABLE IF NOT EXISTS {self._full_table_name("_vectrix_documents")} (

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

            CREATE TABLE IF NOT EXISTS {self._full_table_name("_vectrix_nodes")} (

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

                    TBLPROPERTIES (delta.enableChangeDataFeed = true)

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
                            self._cursor.execute(
                                f"ALTER TABLE {full_name} ADD COLUMN {col} {col_type}"
                            )

                        except Exception:
                            pass  # Column may already exist

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        """A document row with its JSON columns parsed.

        ``save_document`` writes metadata with ``json.dumps``; these readers
        handed the string straight back, so a caller got text where every
        other backend gives a dict.
        """
        decoded = dict(row)
        for column in ("metadata", "data", "attributes"):
            value = decoded.get(column)
            if isinstance(value, str) and value:
                try:
                    decoded[column] = json.loads(value)
                except (ValueError, TypeError):
                    pass  # not JSON, so it is the caller's own string
        return decoded

    def _run(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Execute with native named parameters.



        databricks-sql-connector 3.x binds ``:name`` markers on the server,

        which removes the string escaping this backend used to do by hand and

        the places where it forgot to. Timestamps travel as ISO strings and

        are cast in the statement.

        """

        if params:
            self._cursor.execute(sql, params)

        else:
            self._cursor.execute(sql)

    @staticmethod
    def _array_literal(values: Optional[List[float]]) -> str:
        """An ARRAY<DOUBLE> literal. Arrays cannot be bound as parameters, and

        a float cannot carry an injection, so this is rendered from floats and

        anything that is not one is refused rather than interpolated."""

        if not values:
            return "NULL"

        return "ARRAY(" + ",".join(repr(float(v)) for v in values) + ")"

    def create_collection(self, name: str, config: Dict[str, Any]) -> None:

        mode = config.get("mode", "dense")

        with self._lock:
            now = utcnow_iso()

            self._run(
                f"""

                MERGE INTO {self._full_table_name("_vectrix_collections")} AS target

                USING (SELECT :name AS name) AS source

                ON target.name = source.name

                WHEN MATCHED THEN UPDATE SET

                    config = :config, updated_at = CAST(:updated AS TIMESTAMP)

                WHEN NOT MATCHED THEN INSERT (name, config, created_at, updated_at)

                VALUES (:name, :config, CAST(:created AS TIMESTAMP), CAST(:updated AS TIMESTAMP))

                """,
                {"name": name, "config": json.dumps(config), "created": now, "updated": now},
            )

            self._ensure_collection_table(name, mode=mode)

    def delete_collection(self, name: str) -> None:

        with self._lock:
            self._run(
                f"DELETE FROM {self._full_table_name('_vectrix_collections')} WHERE name = :name",
                {"name": name},
            )

            self._cursor.execute(f"DROP TABLE IF EXISTS {self._full_table_name(name)}")

    def list_collections(self) -> List[str]:

        with self._lock:
            self._cursor.execute(
                f"SELECT name FROM {self._full_table_name('_vectrix_collections')}"
            )

            return [row[0] for row in self._cursor.fetchall()]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:

        with self._lock:
            self._run(
                f"SELECT config FROM {self._full_table_name('_vectrix_collections')} WHERE name = :name",
                {"name": name},
            )

            row = self._cursor.fetchone()

            return json.loads(row[0]) if row else None

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:

        self._ensure_collection_table(collection)

        dense_embedding = data.pop("_embedding", None) or data.pop("dense_embedding", None)

        sparse_embedding = data.pop("sparse_embedding", None)

        late_interaction_embedding = data.pop("late_interaction_embedding", None)

        text_content = data.pop("text_content", None)

        now = utcnow_iso()

        with self._lock:
            dense_str = self._array_literal(dense_embedding)

            late_json = (
                json.dumps(
                    [e.tolist() if hasattr(e, "tolist") else e for e in late_interaction_embedding]
                )
                if late_interaction_embedding
                else None
            )

            self._run(
                f"""

                MERGE INTO {self._full_table_name(collection)} AS target

                USING (SELECT :id AS id) AS source

                ON target.id = source.id

                WHEN MATCHED THEN UPDATE SET

                    data = :data,

                    dense_embedding = {dense_str},

                    sparse_embedding = :sparse,

                    late_interaction_embedding = :late,

                    text_content = :text,

                    updated_at = CAST(:updated AS TIMESTAMP)

                WHEN NOT MATCHED THEN INSERT (id, data, dense_embedding, sparse_embedding, late_interaction_embedding, text_content, created_at, updated_at)

                VALUES (:id, :data, {dense_str}, :sparse, :late, :text, CAST(:created AS TIMESTAMP), CAST(:updated AS TIMESTAMP))

                """,
                {
                    "id": id,
                    "data": json.dumps(data),
                    "sparse": json.dumps(sparse_embedding) if sparse_embedding else None,
                    "late": late_json,
                    "text": text_content,
                    "created": now,
                    "updated": now,
                },
            )

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:

        self._ensure_collection_table(collection)

        count = 0

        for id, data in documents:
            self.insert(collection, id, data)

            count += 1

        return count

    @staticmethod
    def _row_to_data(row: Any) -> Dict[str, Any]:
        """(data, dense, sparse, late, text) -> the dict the rest of the package expects."""

        data = json.loads(row[0]) if row[0] else {}

        if row[1]:
            data["_embedding"] = list(row[1])

        if row[2]:
            data["sparse_embedding"] = json.loads(row[2]) if isinstance(row[2], str) else row[2]

        if row[3]:
            data["late_interaction_embedding"] = (
                json.loads(row[3]) if isinstance(row[3], str) else row[3]
            )

        if row[4]:
            data["text_content"] = row[4]

        return data

    _POINT_COLUMNS = (
        "data, dense_embedding, sparse_embedding, late_interaction_embedding, text_content"
    )

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:

        with self._lock:
            try:
                self._run(
                    f"SELECT {self._POINT_COLUMNS} FROM {self._full_table_name(collection)} WHERE id = :id",
                    {"id": id},
                )

                row = self._cursor.fetchone()

                if row:
                    return self._row_to_data(row)

            except Exception as exc:
                raise StorageOperationError("get", "DeltaLake", str(exc)) from exc

            return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:

        return [self.get(collection, id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:

        embedding = data.pop("_embedding", None) or data.pop("dense_embedding", None)

        now = utcnow_iso()

        with self._lock:
            # Nothing is updated when the id is absent, and the connector's
            # rowcount is not dependable across warehouse versions, so
            # existence is checked rather than assumed. Every other backend
            # returns False for a missing id; this one always said True, and
            # delete_batch counted those as real deletions.
            existed = self.get(collection, id) is not None

            embedding_sql = self._array_literal(embedding) if embedding else "dense_embedding"

            self._run(
                f"""

                UPDATE {self._full_table_name(collection)}

                SET data = :data, dense_embedding = {embedding_sql},

                    updated_at = CAST(:updated AS TIMESTAMP)

                WHERE id = :id

                """,
                {"data": json.dumps(data), "updated": now, "id": id},
            )

            return existed

    def delete(self, collection: str, id: str) -> bool:

        with self._lock:
            existed = self.get(collection, id) is not None

            self._run(
                f"DELETE FROM {self._full_table_name(collection)} WHERE id = :id",
                {"id": id},
            )

            return existed

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

            except Exception as exc:
                raise StorageOperationError("count", "DeltaLake", str(exc)) from exc

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Scan documents with pagination.



        This and ``flush`` are abstract on BaseStorage and were never

        implemented here, so the class could not be instantiated at all: every

        construction raised TypeError before a single query ran.

        """

        with self._lock:
            self._cursor.execute(f"""

                SELECT id, {self._POINT_COLUMNS} FROM {self._full_table_name(collection)}

                LIMIT {int(limit)} OFFSET {int(offset)}

            """)

            rows = self._cursor.fetchall()

        for row in rows:
            data = self._row_to_data(row[1:])

            if filter_func is None or filter_func(data):
                yield row[0], data

    def flush(self) -> None:
        """Delta Lake commits each statement; there is nothing pending to flush."""

        return None

    def iterate(
        self, collection: str, batch_size: int = 1000
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:

        with self._lock:
            offset = 0

            while True:
                self._cursor.execute(f"""

                    SELECT id, {self._POINT_COLUMNS} FROM {self._full_table_name(collection)}

                    LIMIT {int(batch_size)} OFFSET {int(offset)}

                """)

                rows = self._cursor.fetchall()

                if not rows:
                    break

                for row in rows:
                    yield row[0], self._row_to_data(row[1:])

                offset += batch_size

    # Change data feed: what VectrixSync.cdc() reads so deletes reach the
    # target. A table made by this backend has the feed on from its first
    # version; one made before needs enable_change_feed() once, and its feed
    # starts at the version that call commits.

    def enable_change_feed(self, collection: str) -> None:
        """Turn on Delta Lake's change data feed for a collection's table.

        Changes are recorded from the version this commits onwards, not
        before it. A table this backend creates has it on already.
        """

        with self._lock:
            try:
                self._run(
                    f"ALTER TABLE {self._full_table_name(collection)} "
                    "SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
                )

            except Exception as exc:
                raise StorageOperationError("enable_change_feed", "DeltaLake", str(exc)) from exc

    def current_version(self, collection: str) -> int:
        """The latest commit version of a collection's table."""

        with self._lock:
            try:
                self._run(f"DESCRIBE HISTORY {self._full_table_name(collection)} LIMIT 1")

                row = self._cursor.fetchone()

            except Exception as exc:
                raise StorageOperationError("current_version", "DeltaLake", str(exc)) from exc

        if not row:
            raise StorageOperationError("current_version", "DeltaLake", f"{collection} has no history")

        return int(row[0])

    def changes(
        self, collection: str, start_version: int, end_version: int
    ) -> Iterator[Tuple[int, str, str, Optional[Dict[str, Any]]]]:
        """Every change to a collection between two versions, both included.

        Yields ``(version, kind, id, data)`` in commit order, where kind is
        ``"upsert"`` or ``"delete"`` and data is None for a delete. An
        update's pre-image is left out: only the row as it now stands is
        worth copying.

        Raises StorageOperationError when the feed cannot answer: it was not
        on at ``start_version``, or VACUUM has removed the files it needs.
        The caller has to compare the whole table instead.
        """

        with self._lock:
            try:
                self._run(
                    f"""

                    SELECT id, {self._POINT_COLUMNS}, _change_type, _commit_version

                    FROM table_changes(:table, :start, :end)

                    WHERE _change_type != 'update_preimage'

                    ORDER BY _commit_version

                    """,
                    {
                        "table": self._full_table_name(collection),
                        "start": int(start_version),
                        "end": int(end_version),
                    },
                )

                rows = self._cursor.fetchall()

            except Exception as exc:
                raise StorageOperationError("changes", "DeltaLake", str(exc)) from exc

        for row in rows:
            change_type, version = row[6], int(row[7])
            if change_type == "delete":
                yield version, "delete", row[0], None
            else:
                yield version, "upsert", row[0], self._row_to_data(row[1:6])

    def vector_search(
        self, collection: str, query_vector: List[float], limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:
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
        limit: int = 10,
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
        limit: int = 10,
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

            late_interaction_emb = (
                full_data.get("late_interaction_embedding") if full_data else None
            )

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
            now = utcnow_iso()

            params = {
                "doc_id": doc_data.get("doc_id", ""),
                "title": doc_data.get("title", ""),
                "doc_type": doc_data.get("doc_type", ""),
                "page_count": int(doc_data.get("page_count") or 0),
                "section_count": int(doc_data.get("section_count") or 0),
                "node_count": int(doc_data.get("node_count") or 0),
                "indexed_at": now,
                "metadata": json.dumps(doc_data.get("metadata", {})),
            }

            self._run(
                f"""

                MERGE INTO {self._full_table_name("_vectrix_documents")} AS target

                USING (SELECT :doc_id AS doc_id) AS source

                ON target.doc_id = source.doc_id

                WHEN MATCHED THEN UPDATE SET

                    title = :title,

                    doc_type = :doc_type,

                    page_count = :page_count,

                    section_count = :section_count,

                    node_count = :node_count,

                    indexed_at = CAST(:indexed_at AS TIMESTAMP),

                    metadata = :metadata

                WHEN NOT MATCHED THEN INSERT (doc_id, title, doc_type, page_count, section_count, node_count, indexed_at, metadata)

                VALUES (:doc_id, :title, :doc_type, :page_count, :section_count, :node_count,

                        CAST(:indexed_at AS TIMESTAMP), :metadata)

                """,
                params,
            )

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:

        with self._lock:
            try:
                self._run(
                    f"SELECT * FROM {self._full_table_name('_vectrix_documents')} WHERE doc_id = :doc_id",
                    {"doc_id": doc_id},
                )

                row = self._cursor.fetchone()

                if row:
                    cols = [desc[0] for desc in self._cursor.description]

                    return self._decode_row(dict(zip(cols, row)))

            except Exception as exc:
                raise StorageOperationError("get_document", "DeltaLake", str(exc)) from exc

            return None

    def list_documents(self) -> List[Dict[str, Any]]:

        with self._lock:
            try:
                self._cursor.execute(f"SELECT * FROM {self._full_table_name('_vectrix_documents')}")

                cols = [desc[0] for desc in self._cursor.description]

                return [self._decode_row(dict(zip(cols, row))) for row in self._cursor.fetchall()]

            except Exception as exc:
                raise StorageOperationError("list_documents", "DeltaLake", str(exc)) from exc

    def delete_document(self, doc_id: str) -> bool:

        with self._lock:
            self._run(
                f"DELETE FROM {self._full_table_name('_vectrix_documents')} WHERE doc_id = :doc_id",
                {"doc_id": doc_id},
            )

            return True

    def save_node(self, node_data: Dict[str, Any]) -> None:

        with self._lock:
            page_num = node_data.get("page_num")

            params = {
                "node_id": node_data.get("node_id", ""),
                "doc_id": node_data.get("doc_id", ""),
                "parent_id": node_data.get("parent_id") or None,
                "level": int(node_data.get("level") or 0),
                "title": node_data.get("title", ""),
                "text": node_data.get("text", ""),
                "page_num": int(page_num) if page_num is not None else None,
                "position": int(node_data.get("position") or 0),
                "metadata": json.dumps(node_data.get("metadata", {})),
            }

            self._run(
                f"""

                MERGE INTO {self._full_table_name("_vectrix_nodes")} AS target

                USING (SELECT :node_id AS node_id) AS source

                ON target.node_id = source.node_id

                WHEN MATCHED THEN UPDATE SET

                    doc_id = :doc_id,

                    parent_id = :parent_id,

                    level = :level,

                    title = :title,

                    text = :text,

                    page_num = :page_num,

                    position = :position,

                    metadata = :metadata

                WHEN NOT MATCHED THEN INSERT (node_id, doc_id, parent_id, level, title, text, page_num, position, metadata)

                VALUES (:node_id, :doc_id, :parent_id, :level, :title, :text, :page_num, :position, :metadata)

                """,
                params,
            )

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:

        with self._lock:
            try:
                self._run(
                    f"SELECT * FROM {self._full_table_name('_vectrix_nodes')} WHERE node_id = :node_id",
                    {"node_id": node_id},
                )

                row = self._cursor.fetchone()

                if row:
                    cols = [desc[0] for desc in self._cursor.description]

                    return self._decode_row(dict(zip(cols, row)))

            except Exception as exc:
                raise StorageOperationError("get_node", "DeltaLake", str(exc)) from exc

            return None

    def get_document_nodes(self, doc_id: str) -> List[Dict[str, Any]]:

        with self._lock:
            try:
                self._run(
                    f"SELECT * FROM {self._full_table_name('_vectrix_nodes')} WHERE doc_id = :doc_id ORDER BY position",
                    {"doc_id": doc_id},
                )

                cols = [desc[0] for desc in self._cursor.description]

                return [self._decode_row(dict(zip(cols, row))) for row in self._cursor.fetchall()]

            except Exception as exc:
                raise StorageOperationError("get_document_nodes", "DeltaLake", str(exc)) from exc

    def get_child_nodes(self, parent_id: str) -> List[Dict[str, Any]]:

        with self._lock:
            try:
                self._run(
                    f"SELECT * FROM {self._full_table_name('_vectrix_nodes')} WHERE parent_id = :parent_id ORDER BY position",
                    {"parent_id": parent_id},
                )

                cols = [desc[0] for desc in self._cursor.description]

                return [self._decode_row(dict(zip(cols, row))) for row in self._cursor.fetchall()]

            except Exception as exc:
                raise StorageOperationError("get_child_nodes", "DeltaLake", str(exc)) from exc

    def delete_document_nodes(self, doc_id: str) -> int:

        with self._lock:
            self._run(
                f"DELETE FROM {self._full_table_name('_vectrix_nodes')} WHERE doc_id = :doc_id",
                {"doc_id": doc_id},
            )

            return 0  # Delta Lake doesn't return row count easily





# ============================================================================
# PROMOTED FILTER FIELDS
# ============================================================================
#
# INPUT   a metadata path, and a document
# OUTPUT  the index field a promoted path lands in; the value at the path, as
#         its kind, or None; a filter with no honest translation, refused
#
# A store that can only filter on mapped fields has the metadata paths a
# policy filters on promoted into fields of their own.

PROMOTED_KINDS = ("string", "strings", "number", "boolean")


class _FilterUnsupported(Exception):
    """This filter has no honest translation for this store."""


def _promoted_name(path: str) -> str:
    """The index field a promoted metadata path lands in."""
    return "f_" + "".join(ch if ch.isalnum() else "_" for ch in path.replace(".", "__"))


def _promoted_value(data: Dict[str, Any], path: str, kind: str) -> Any:
    """The value at a dotted path, as its kind, or None. A value of the wrong
    type is None too: a filter field that lies is worse than one that is
    missing, because missing fails closed."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if node is None:
        return None
    if kind == "string":
        return str(node) if isinstance(node, (str, int, float)) and not isinstance(node, bool) else None
    if kind == "strings":
        values = node if isinstance(node, (list, tuple, set)) else [node]
        out = [str(v) for v in values if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
        return out or None
    if kind == "number":
        return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None
    if kind == "boolean":
        return node if isinstance(node, bool) else None
    return None


# Which vectors answer the OpenSearch search in progress, and the question's words.
_OS_SELECTION: "contextvars.ContextVar[Any]" = contextvars.ContextVar("vectrixdb_opensearch_vectors", default=None)


# ============================================================================
# AWS OPENSEARCH SERVERLESS
# ============================================================================
#
# INPUT   a collection endpoint
# OUTPUT  vectors and metadata in OpenSearch, with the promoted fields
#         filtered on the engine's side
#
# A filter the engine cannot run is refused rather than approximated.


class OpenSearchStorage(BaseStorage):
    """

    AWS OpenSearch Serverless storage backend.



    Uses OpenSearch k-NN for vector search. Supports dense and hybrid modes.

    NOTE: Does NOT support ultimate/graph modes (no native ColBERT MaxSim).

    """

    EMBEDDINGS = ("vectrixdb", "bedrock", "both")

    def __init__(self, config: StorageConfig, client: Any = None):
        mode = config.opensearch_embeddings
        if mode not in self.EMBEDDINGS:
            raise ConfigurationError(f"embeddings is one of {', '.join(self.EMBEDDINGS)}, got {mode!r}")
        if mode != "vectrixdb":
            if not callable(config.opensearch_embed_fn):
                raise ConfigurationError(
                    f"embeddings={mode!r} needs embed_fn, texts to vectors: "
                    "vectrixdb.models.bedrock.BedrockEmbedder(boto3.client('bedrock-runtime'))"
                )
            dims = config.opensearch_embed_dimensions or getattr(config.opensearch_embed_fn, "dimensions", None)
            if not isinstance(dims, int) or dims <= 0:
                raise ConfigurationError(f"embeddings={mode!r} needs the Bedrock model's dimensions")
        weights = config.opensearch_vector_weights or {}
        if set(weights) - {"vectrixdb", "bedrock"} or any(
            not isinstance(w, (int, float)) or w <= 0 for w in weights.values()
        ):
            raise ConfigurationError("vector_weights maps 'vectrixdb' and 'bedrock' to positive numbers")
        self._staged: Dict[str, Dict[str, Any]] = {}
        self._staged_lock = threading.Lock()
        """``client`` is an already-built OpenSearch client, for tests that
        hand in a stand-in; production leaves it None and connect() builds
        one from the config with AWS credentials."""

        self.config = config

        self._client: Any = client

        self._lock = threading.Lock()

    def connect(self) -> None:
        """Connect to OpenSearch using AWS credentials."""

        if self._client is not None:
            return

        try:
            from opensearchpy import OpenSearch, RequestsHttpConnection

            from requests_aws4auth import AWS4Auth

            import boto3

        except ImportError:
            raise ImportError(
                "Install OpenSearch dependencies: pip install opensearch-py boto3 requests-aws4auth"
            )

        region = self.config.opensearch_region

        service = self.config.opensearch_service

        if self.config.opensearch_aws_access_key_id:
            credentials = boto3.Session(
                aws_access_key_id=self.config.opensearch_aws_access_key_id,
                aws_secret_access_key=self.config.opensearch_aws_secret_access_key,
                aws_session_token=self.config.opensearch_aws_session_token,
            ).get_credentials()

        else:
            credentials = boto3.Session().get_credentials()

        auth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            region,
            service,
            session_token=credentials.token,
        )

        endpoint = self.config.opensearch_endpoint
        if not endpoint:
            raise StorageOperationError("connect", "OpenSearch", "opensearch_endpoint is not set")

        if endpoint.startswith("https://"):
            endpoint = endpoint[8:]

        if endpoint.startswith("http://"):
            endpoint = endpoint[7:]

        self._client = OpenSearch(
            hosts=[{"host": endpoint, "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
        )

    def close(self) -> None:

        self._client = None

    _STANDARD = (
        "_embedding",
        "dense_embedding",
        "sparse_embedding",
        "text_content",
        "late_interaction_embedding",
        "created_at",
        "updated_at",
    )

    def _to_doc(self, id_: str, data: Dict[str, Any], now: str) -> Dict[str, Any]:
        """The OpenSearch document for a caller's record: the caller's own
        fields go under ``metadata`` (a disabled object, so any shape is
        allowed) and the standard fields keep their columns."""
        metadata = {k: v for k, v in data.items() if k not in self._STANDARD}
        # Every promoted field, present or not: an update is partial, so a
        # field that went away has to be sent as null or it would keep the
        # value it had, and a revocation would not reach the filter.
        promoted = {_promoted_name(p): _promoted_value(metadata, p, k) for p, k in self._promoted().items()}
        named = data.get("named_vectors") or {}
        second = {"dense_bedrock": [float(x) for x in named["bedrock"]]} if named.get("bedrock") is not None else {}
        metadata.pop("named_vectors", None)
        return {
            **promoted,
            **second,
            "id": id_,  # our id lives in the body; Serverless refuses custom _id
            "dense_embedding": data.get("dense_embedding") or data.get("_embedding"),
            "sparse_embedding": data.get("sparse_embedding"),
            "text_content": data.get("text_content", ""),
            "metadata": metadata,
            "created_at": data.get("created_at", now),
            "updated_at": now,
        }

    @staticmethod
    def _from_doc(source: Dict[str, Any], include_vector: bool = True) -> Dict[str, Any]:
        """The caller's record from an OpenSearch document: metadata
        flattened back to the top level, the standard fields beside it."""
        out: Dict[str, Any] = dict(source.get("metadata") or {})
        for key in ("text_content", "sparse_embedding", "created_at", "updated_at"):
            if source.get(key) is not None:
                out[key] = source[key]
        if include_vector and source.get("dense_embedding") is not None:
            out["dense_embedding"] = source["dense_embedding"]
        return out

    def _index_name(self, collection: str) -> str:

        return f"{self.config.opensearch_index_prefix}_{collection}".lower()

    def create_collection(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> None:

        index_name = self._index_name(name)

        dimension = metadata.get("dimension", 384) if metadata else 384

        if self._client.indices.exists(index=index_name):
            return

        engine = self.config.opensearch_knn_engine
        kinds = {"string": "keyword", "strings": "keyword", "number": "double", "boolean": "boolean"}
        promoted = {_promoted_name(p): {"type": kinds[k]} for p, k in self._promoted().items()}

        body = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 100,
                }
            },
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "dense_embedding": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": engine,
                            "parameters": {"ef_construction": 128, "m": 24},
                        },
                    },
                    "sparse_embedding": {"type": "object", "enabled": False},
                    "text_content": {"type": "text"},
                    "metadata": {"type": "object", "enabled": False},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    **promoted,
                }
            },
        }
        if self.config.opensearch_embeddings == "both":
            properties: Dict[str, Any] = body["mappings"]["properties"]  # type: ignore[index]
            properties["dense_bedrock"] = {
                "type": "knn_vector",
                "dimension": self._bedrock_dimensions(),
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": engine,
                    "parameters": {"ef_construction": 128, "m": 24},
                },
            }

        self._client.indices.create(index=index_name, body=body)

        now = utcnow().isoformat()

        # Note: Serverless doesn't support custom _id or refresh=True

        self._client.index(
            index=f"{self.config.opensearch_index_prefix}_collections",
            body={"name": name, "created_at": now, **(metadata or {}), "dimension": dimension},
        )

    def delete_collection(self, name: str) -> None:

        index_name = self._index_name(name)

        if self._client.indices.exists(index=index_name):
            self._client.indices.delete(index=index_name)

        # The registry entry goes too, or get_collection_config() keeps
        # answering for a collection that no longer exists.
        registry = f"{self.config.opensearch_index_prefix}_collections"
        try:
            found = self._client.search(
                index=registry, body={"query": {"term": {"name": name}}, "size": 10}
            )
            for hit in found.get("hits", {}).get("hits", []):
                self._client.delete(index=registry, id=hit["_id"])
        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError("delete_collection", "OpenSearch", str(exc)) from exc

    def list_collections(self) -> List[str]:

        prefix = f"{self.config.opensearch_index_prefix}_"

        indices = self._client.indices.get_alias(index=f"{prefix}*")

        return [
            name.replace(prefix, "") for name in indices.keys() if not name.endswith("_collections")
        ]

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        ((id, data),) = self._with_bedrock_vectors([(id, data)])

        index_name = self._index_name(collection)

        now = utcnow().isoformat()

        # An id that already exists is replaced, not duplicated. Serverless
        # refuses custom _id, so the replacement is a delete of the old
        # document followed by an index of the new one.
        existing = self._find_doc_id(index_name, id)
        if existing:
            self._client.delete(index=index_name, id=existing)

        self._client.index(index=index_name, body=self._to_doc(id, data, now))

    def insert_batch(self, collection: str, items: List[Tuple[str, Dict[str, Any]]]) -> int:
        items = self._with_bedrock_vectors(items)

        index_name = self._index_name(collection)

        now = utcnow().isoformat()

        # OpenSearch Serverless doesn't support custom _id at all

        # Use bulk API without _id and store our ID in the doc body

        actions = []

        for id_, data in items:
            existing = self._find_doc_id(index_name, id_)
            if existing:
                actions.append({"delete": {"_index": index_name, "_id": existing}})
            # No _id in the index action: OpenSearch generates one and our id
            # travels in the body, which is what Serverless allows.
            actions.append({"index": {"_index": index_name}})
            actions.append(self._to_doc(id_, data, now))

        if actions:
            result = self._client.bulk(body=actions)  # No refresh for Serverless

            # Check for bulk errors

            if result.get("errors"):
                error_items = [
                    item for item in result.get("items", []) if item.get("index", {}).get("error")
                ]

                if error_items:
                    first_error = error_items[0].get("index", {}).get("error", {})

                    raise RuntimeError(f"Bulk insert failed: {first_error}")

        return len(items)

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        """Get document by our custom id field (not _id which is auto-generated)."""

        index_name = self._index_name(collection)

        try:
            # Search by id field in document body

            result = self._client.search(
                index=index_name, body={"query": {"term": {"id": id}}, "size": 1}
            )

            hits = result.get("hits", {}).get("hits", [])

            return self._from_doc(hits[0]["_source"]) if hits else None

        except Exception as exc:
            if _is_not_found(exc):
                return None

            raise StorageOperationError("get", "OpenSearch", str(exc)) from exc

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:

        return [self.get(collection, id) for id in ids]

    def _find_doc_id(self, index_name: str, id: str) -> Optional[str]:
        """Find the OpenSearch _id for a document with our custom id field."""

        try:
            result = self._client.search(
                index=index_name, body={"query": {"term": {"id": id}}, "size": 1, "_source": False}
            )

            hits = result.get("hits", {}).get("hits", [])

            return hits[0]["_id"] if hits else None

        except Exception as exc:
            if _is_not_found(exc):
                return None

            raise StorageOperationError("find_document_id", "OpenSearch", str(exc)) from exc

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:

        index_name = self._index_name(collection)

        current = self.get(collection, id)
        if current is None:
            return False
        body_doc = self._to_doc(id, {**current, **data}, utcnow().isoformat())

        try:
            # Find the OpenSearch _id first

            doc_id = self._find_doc_id(index_name, id)

            if not doc_id:
                return False

            self._client.update(index=index_name, id=doc_id, body={"doc": body_doc})

            return True

        except Exception as exc:
            if _is_not_found(exc):
                return False

            raise StorageOperationError("update", "OpenSearch", str(exc)) from exc

    def delete(self, collection: str, id: str) -> bool:

        index_name = self._index_name(collection)

        try:
            # Find the OpenSearch _id first

            doc_id = self._find_doc_id(index_name, id)

            if not doc_id:
                return False

            self._client.delete(index=index_name, id=doc_id)

            return True

        except Exception as exc:
            if _is_not_found(exc):
                return False

            raise StorageOperationError("delete", "OpenSearch", str(exc)) from exc

    def delete_batch(self, collection: str, ids: List[str]) -> int:

        count = 0

        for id in ids:
            if self.delete(collection, id):
                count += 1

        return count

    def count(self, collection: str) -> int:

        index_name = self._index_name(collection)

        try:
            result = self._client.count(index=index_name)

            return result["count"]

        except Exception as exc:
            if _is_not_found(exc):
                return 0

            raise StorageOperationError("count", "OpenSearch", str(exc)) from exc

    def iterate(
        self, collection: str, batch_size: int = 1000
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:

        index_name = self._index_name(collection)

        body = {"query": {"match_all": {}}, "size": batch_size}

        result = self._client.search(index=index_name, body=body, scroll="2m")

        scroll_id = result["_scroll_id"]

        hits = result["hits"]["hits"]

        while hits:
            for hit in hits:
                source = hit["_source"]
                yield source.get("id", hit["_id"]), self._from_doc(source)

            result = self._client.scroll(scroll_id=scroll_id, scroll="2m")

            scroll_id = result["_scroll_id"]

            hits = result["hits"]["hits"]

    # ------------------------------------------------------ the second vector

    def _bedrock_dimensions(self) -> int:
        dims = self.config.opensearch_embed_dimensions or getattr(self.config.opensearch_embed_fn, "dimensions", 0)
        return int(dims or 0)

    def vector_names(self) -> tuple:
        return {"vectrixdb": ("vectrixdb",), "bedrock": ("bedrock",), "both": ("vectrixdb", "bedrock")}[
            self.config.opensearch_embeddings
        ]

    def embed_bedrock(self, texts: List[str]) -> List[List[float]]:
        vectors = [[float(x) for x in v] for v in self.config.opensearch_embed_fn(list(texts))]
        want = self._bedrock_dimensions()
        if vectors and len(vectors[0]) != want:
            raise ConfigurationError(
                f"the Bedrock embedding returned {len(vectors[0])} dimensions and the index field was made for {want}"
            )
        return vectors

    @property
    def default_embed_fn(self) -> Any:
        """With ``embeddings="bedrock"`` the collection's own embedder is the
        Bedrock model, so a caller that named no embedder is handed it."""
        if self.config.opensearch_embeddings != "bedrock":
            return None
        import numpy as np

        return lambda texts: np.asarray(self.embed_bedrock(list(texts)), dtype=np.float32)

    def named_embedders(self) -> dict:
        if self.config.opensearch_embeddings != "both":
            return {}
        fn = self.config.opensearch_embed_fn
        return {"bedrock": (str(getattr(fn, "label", "bedrock")), self.embed_bedrock)}

    def stage_named_vectors(self, vectors: Dict[str, Dict[str, Any]]) -> None:
        with self._staged_lock:
            for name, by_id in vectors.items():
                self._staged.setdefault(name, {}).update(by_id)

    def _with_bedrock_vectors(self, items: List[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, Dict[str, Any]]]:
        if self.config.opensearch_embeddings != "both":
            return items
        with self._staged_lock:
            staged = self._staged.get("bedrock", {})
            taken = {doc_id: staged.pop(doc_id) for doc_id, _ in items if doc_id in staged}
        out: List[Tuple[str, Dict[str, Any]]] = []
        missing: List[int] = []
        for i, (doc_id, data) in enumerate(items):
            data = dict(data)
            named = dict(data.get("named_vectors") or {})
            if doc_id in taken:
                named["bedrock"] = taken[doc_id]
            if named.get("bedrock") is None and (data.get("text_content") or "").strip():
                missing.append(i)
            data["named_vectors"] = named
            out.append((doc_id, data))
        if missing:
            for i, vector in zip(missing, self.embed_bedrock([out[i][1]["text_content"] for i in missing])):
                out[i][1]["named_vectors"]["bedrock"] = vector
        return out

    @contextlib.contextmanager
    def using_vectors(self, vectors: Optional[str], query_text: Optional[str] = None) -> Any:
        """Which vectors answer the searches inside this block, and the words
        of the question, which the Bedrock vector has to be embedded from."""
        if vectors is not None:
            self._fields_for(vectors)
        token = _OS_SELECTION.set((vectors, query_text))
        try:
            yield self
        finally:
            _OS_SELECTION.reset(token)

    @staticmethod
    def scoped() -> bool:
        return _OS_SELECTION.get() is not None

    def needs_the_store(self) -> bool:
        """True when the search asks for the Bedrock vector beside the
        collection's own: the local index holds only the second of those."""
        if self.config.opensearch_embeddings != "both":
            return False
        selection = _OS_SELECTION.get()
        return self._fields_for(selection[0] if selection else None) != ["vectrixdb"]

    def _fields_for(self, vectors: Optional[str]) -> List[str]:
        have = self.vector_names()
        if vectors is None:
            return list(have)
        if vectors == "both":
            if len(have) < 2:
                raise ConfigurationError(
                    f"vectors='both' needs an index built with embeddings='both'; this one holds {have[0]!r}"
                )
            return list(have)
        if vectors not in have:
            raise ConfigurationError(
                f"vectors={vectors!r} is not in this index, which holds {' and '.join(repr(h) for h in have)}. "
                "What an index holds is decided when it is built, with embeddings=."
            )
        return [vectors]

    def _vector_runs(
        self, collection: str, query_vector: List[float], limit: int, filter: Optional[Dict[str, Any]], text: Optional[str] = None
    ) -> Dict[str, List[Tuple[str, Dict[str, Any], float]]]:
        """One k-NN search per vector that answers, by name."""
        selection = _OS_SELECTION.get()
        chosen, query_text = selection if selection else (None, None)
        query_text = text or query_text
        runs: Dict[str, List[Tuple[str, Dict[str, Any], float]]] = {}
        for name in self._fields_for(chosen):
            if name == "vectrixdb" or self.config.opensearch_embeddings == "bedrock":
                runs[name] = self._knn_search(collection, "dense_embedding", query_vector, limit, filter)
                continue
            if not query_text:
                raise StorageOperationError(
                    "vector_search", "OpenSearch",
                    "the Bedrock vector is searched with the question's words, and this search gave only a vector; "
                    "search through Vectrix, or pass vectors='vectrixdb'",
                )
            runs[name] = self._knn_search(collection, "dense_bedrock", self.embed_bedrock([query_text])[0], limit, filter)
        return runs

    def _fuse_vector_runs(self, runs: Dict[str, List[Tuple[str, Dict[str, Any], float]]], limit: int) -> List[Tuple[str, Dict[str, Any], float]]:
        if len(runs) == 1:
            return next(iter(runs.values()))[:limit]
        weights = self.config.opensearch_vector_weights or {}
        scores: Dict[str, float] = {}
        data: Dict[str, Dict[str, Any]] = {}
        similar: Dict[str, Dict[str, float]] = {}
        for name, hits in runs.items():
            for rank, (doc_id, doc, _score) in enumerate(hits):
                scores[doc_id] = scores.get(doc_id, 0.0) + float(weights.get(name, 1.0)) / (60 + rank + 1)
                if doc.get("_vx_relevance") is not None:
                    similar.setdefault(doc_id, {})[name] = doc["_vx_relevance"]
                data.setdefault(doc_id, doc)
        for doc_id, by_name in similar.items():
            # Two models measured this chunk. The collection's own is the one
            # a threshold is set on; both are kept for whoever compares them.
            data[doc_id] = {**data[doc_id], "_vx_relevances": by_name, "_vx_relevance": by_name.get("vectrixdb", next(iter(by_name.values())))}
        ordered = sorted(scores, key=lambda i: (-scores[i], i))[:limit]
        # 1 - score, as _knn_search does: the collection turns it back into the fused score.
        return [(i, data[i], 1.0 - scores[i]) for i in ordered]

    def vector_ranks(
        self, collection: str, query_vector: List[float], limit: int = 50, filter: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Dict[str, int]]:
        ranks: Dict[str, Dict[str, int]] = {}
        for name, hits in self._vector_runs(collection, query_vector, limit, filter).items():
            for rank, (doc_id, _doc, _score) in enumerate(hits, start=1):
                ranks.setdefault(doc_id, {})[name] = rank
        return ranks

    def _knn_search(
        self, collection: str, field_name: str, vector: List[float], limit: int, filter: Optional[Dict[str, Any]]
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """One k-NN search. The third element is ``1 - _score``, the distance-like
        value every store hands the collection, which turns it back into the
        engine's own score. Handing ``_score`` itself made every score come out
        as one minus the engine's, and a ``score_threshold`` then kept the
        worst matches and dropped the best."""
        body = {"size": limit, "query": self._knn(vector, limit, filter, field_name)}
        result = self._client.search(index=self._index_name(collection), body=body)
        hits = result["hits"]["hits"]
        formula = self._score_formula(hits, field_name, vector)
        out = []
        for hit in hits:
            data = self._from_doc(hit["_source"], include_vector=False)
            found = _relevance.from_opensearch_cosine_score(hit.get("_score"), formula)
            if found is not None:
                data["_vx_relevance"] = found
            out.append((hit["_source"].get("id", hit["_id"]), data, 1.0 - float(hit["_score"])))
        return out

    #: Which of OpenSearch's two documented cosine formulas this cluster uses,
    #: once a hit has shown it. None until then, and relevance is None with it.
    _cosine_formula: Optional[str] = None

    def _score_formula(self, hits: List[Dict[str, Any]], field_name: str, query: List[float]) -> Optional[str]:
        """Work out how this cluster turns a cosine distance into a score, from a hit.

        The documentation gives ``1 / (1 + d)`` for nmslib and faiss and
        ``(2 - d) / 2`` for Lucene through 2.17, and ``(2 - d) / 2`` for every
        engine from 2.19, so the answer depends on a version and an engine
        that a managed or serverless cluster may not even report. A hit
        settles it: its stored vector gives the true cosine, and only one of
        the two formulas turns its score into that. A perfect match fits
        both and settles nothing, so the next hit is tried.
        """
        configured = getattr(self.config, "opensearch_score_formula", "auto")
        if configured in _relevance.OPENSEARCH_FORMULAS:
            return configured
        if self._cosine_formula is not None:
            return self._cosine_formula
        for hit in hits[:5]:
            stored = (hit.get("_source") or {}).get(field_name)
            if not stored or hit.get("_score") is None:
                continue
            true = _relevance.cosine(query, stored)
            if true is None:
                continue
            picked = _relevance.pick_opensearch_formula(float(hit["_score"]), true)
            if picked is not None:
                self._cosine_formula = picked
                return picked
        return None

    # ------------------------------------------------------------ pushdown

    def _promoted(self) -> Dict[str, str]:
        fields = self.config.opensearch_filter_fields or {}
        for path, kind in fields.items():
            if kind not in PROMOTED_KINDS:
                raise ConfigurationError(
                    f"filter field {path!r} has kind {kind!r}; a kind is one of {', '.join(PROMOTED_KINDS)}"
                )
        return dict(fields)

    def filter_fields(self, collection: str) -> frozenset:
        return frozenset(self._promoted())

    def compile_filter(self, collection: str, filter_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The library's filter grammar as an OpenSearch query over the
        promoted fields. None when a field is not promoted or an operator has
        no honest translation, and the collection then applies that filter
        itself.

        A field that is null is not indexed, so to OpenSearch null and absent
        are the same thing. Every clause therefore requires the field to
        exist, and a null fails the rule, walls included: the closed reading
        is the one to keep when the two cannot be told apart.
        """
        try:
            return self._query(filter_dict, self._promoted())
        except _FilterUnsupported:
            return None

    @classmethod
    def _query(cls, node: Any, kinds: Dict[str, str]) -> Dict[str, Any]:
        if not isinstance(node, dict) or not node:
            raise _FilterUnsupported
        if "$and" in node or "$or" in node:
            key = "$and" if "$and" in node else "$or"
            parts = [cls._query(n, kinds) for n in node[key]]
            if not parts:
                raise _FilterUnsupported
            if key == "$and":
                return {"bool": {"filter": parts}}
            return {"bool": {"should": parts, "minimum_should_match": 1}}
        if "field" in node and "op" in node:
            return cls._clause(node["field"], node["op"], node.get("value"), kinds)
        parts = []
        for field_name, spec in node.items():
            if isinstance(spec, dict) and spec and all(k.startswith("$") for k in spec):
                for op, value in spec.items():
                    parts.append(cls._clause(field_name, op[1:], value, kinds))
            else:
                parts.append(cls._clause(field_name, "eq", spec, kinds))
        return parts[0] if len(parts) == 1 else {"bool": {"filter": parts}}

    @staticmethod
    def _value(value: Any, kind: str) -> Any:
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _FilterUnsupported
            return float(value)
        if kind == "boolean":
            if not isinstance(value, bool):
                raise _FilterUnsupported
            return value
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise _FilterUnsupported
        return str(value)

    @classmethod
    def _clause(cls, field_name: str, op: str, value: Any, kinds: Dict[str, str]) -> Dict[str, Any]:
        kind = kinds.get(field_name)
        if kind is None:
            raise _FilterUnsupported
        name = _promoted_name(field_name)
        exists = {"exists": {"field": name}}
        scalar = "string" if kind == "strings" else kind
        if op == "exists":
            return exists if value else {"bool": {"must_not": [exists]}}
        if op in ("in", "nin"):
            values = value if isinstance(value, (list, tuple, set)) else [value]
            if not values:
                raise _FilterUnsupported
            terms = {"terms": {name: [cls._value(v, scalar) for v in values]}}
            return terms if op == "in" else {"bool": {"filter": [exists], "must_not": [terms]}}
        if kind == "strings":
            raise _FilterUnsupported
        if op == "eq":
            return {"term": {name: cls._value(value, kind)}}
        if op == "ne":
            return {"bool": {"filter": [exists], "must_not": [{"term": {name: cls._value(value, kind)}}]}}
        if op in ("gt", "gte", "lt", "lte"):
            if kind == "boolean":
                raise _FilterUnsupported
            return {"range": {name: {op: cls._value(value, kind)}}}
        raise _FilterUnsupported

    def _knn(
        self, query_vector: List[float], k: int, filter: Optional[Dict[str, Any]], field_name: str = "dense_embedding"
    ) -> Dict[str, Any]:
        """The k-NN clause, with the filter where this engine wants it.

        lucene and faiss take the filter inside the k-NN clause and apply it
        while they search, so ``k`` results come back whatever the filter
        lets through. nmslib cannot: the filter runs over the ``k`` it found,
        so more are asked for, and a selective filter can still come back
        short. That is a recall cost and never a leak; what is outside the
        filter does not leave the service either way.
        """
        if filter is None:
            return {"knn": {field_name: {"vector": query_vector, "k": k}}}
        if self.config.opensearch_knn_engine in ("lucene", "faiss"):
            return {"knn": {field_name: {"vector": query_vector, "k": k, "filter": filter}}}
        wide = min(max(k * 10, 100), 10000)
        return {"bool": {"must": [{"knn": {field_name: {"vector": query_vector, "k": wide}}}], "filter": [filter]}}

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """k-NN search, over every vector this index answers with, fused by rank when there are two.

        Returns ``(id, data, 1 - score)`` tuples, best first, the way every
        store hands results to the collection. ``data`` carries the true
        cosine as ``_vx_relevance`` when the cluster's score formula is known.
        """
        return self._fuse_vector_runs(self._vector_runs(collection, query_vector, limit, filter), limit)

    def text_search(
        self,
        collection: str,
        query_text: str,
        limit: int = 10,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """BM25 over ``text_content``. Returns ``(id, data, score)`` tuples, best first, with OpenSearch's own BM25 score."""
        match = {"match": {"text_content": {"query": query_text}}}
        body = {"size": limit, "query": match if filter is None else {"bool": {"must": [match], "filter": [filter]}}}
        result = self._client.search(index=self._index_name(collection), body=body)
        return [
            (
                hit["_source"].get("id", hit["_id"]),
                self._from_doc(hit["_source"], include_vector=False),
                hit["_score"],
            )
            for hit in result["hits"]["hits"]
        ]

    def hybrid_search(
        self,
        collection: str,
        query_vector: List[float],
        query_text: str,
        limit: int = 10,
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
        rrf_k: int = 60,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Hybrid search: k-NN and BM25 fused by weighted Reciprocal Rank Fusion.

        Each list is fetched five times deeper than ``limit`` (at most 100),
        and a result scores ``weight / (rrf_k + rank)`` in each list it is in,
        so the two scales never have to be compared. Returns
        ``(id, data, rrf_score)`` tuples, best first.
        """
        prefetch = min(limit * 5, 100)
        dense_results = self._fuse_vector_runs(
            self._vector_runs(collection, query_vector, prefetch, filter, text=query_text), prefetch
        )
        sparse_results = self.text_search(collection, query_text, prefetch, filter=filter)

        scores: Dict[str, float] = {}
        doc_data = {}
        for rank, (doc_id, data, _score) in enumerate(dense_results):
            scores[doc_id] = scores.get(doc_id, 0) + dense_weight / (rrf_k + rank + 1)
            doc_data[doc_id] = data
        for rank, (doc_id, data, _score) in enumerate(sparse_results):
            scores[doc_id] = scores.get(doc_id, 0) + sparse_weight / (rrf_k + rank + 1)
            doc_data.setdefault(doc_id, data)

        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        return [(doc_id, doc_data[doc_id], score) for doc_id, score in sorted_results]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        """Get collection configuration from metadata index."""

        try:
            result = self._client.search(
                index=f"{self.config.opensearch_index_prefix}_collections",
                body={"query": {"term": {"name": name}}, "size": 1},
            )
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                return None
            source = dict(hits[0]["_source"])
            source.pop("name", None)
            source.pop("created_at", None)
            return source

        except Exception as exc:
            if _is_not_found(exc):
                return None

            raise StorageOperationError("get_collection_config", "OpenSearch", str(exc)) from exc

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Scan documents with pagination, in index order."""

        index_name = self._index_name(collection)
        try:
            result = self._client.search(
                index=index_name,
                body={"query": {"match_all": {}}, "size": int(limit), "from": int(offset)},
            )
        except Exception as exc:
            if _is_not_found(exc):
                return
            raise StorageOperationError("scan", "OpenSearch", str(exc)) from exc
        for hit in result.get("hits", {}).get("hits", []):
            source = hit["_source"]
            data = self._from_doc(source)
            if filter_func is None or filter_func(data):
                yield source.get("id", hit["_id"]), data

    def flush(self) -> None:
        """Flush pending writes. OpenSearch Serverless handles this automatically."""

        pass  # No-op: Serverless doesn't support explicit refresh


# ============================================================================
# AWS AURORA POSTGRESQL
# ============================================================================
#
# INPUT   an Aurora cluster with pgvector
# OUTPUT  vectors and metadata in Aurora
#
# The same SQL as Lakebase, over Aurora's connection.


class AuroraPostgreSQLStorage(BaseStorage):
    """

    AWS Aurora PostgreSQL storage backend with pgvector.



    Supports all modes including ultimate (ColBERT) and graph.

    """

    SUPPORTS_SESSION_ROLE: ClassVar[bool] = True

    #: What a DBA runs once, so there is something for the role to be refused
    #: by. VectrixDB never issues this: a connection that can create a policy
    #: can drop one.
    RLS_SETUP = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

-- One policy, in SQL, owned by the database. Shape it to your entitlements;
-- what matters is that it is evaluated by PostgreSQL for current_user rather
-- than by the application asking nicely.
CREATE POLICY {table}_read ON {table}
    FOR SELECT
    USING (
        metadata->'entitlements'->>'lob' = current_setting('vectrix.lob', true)
    );

GRANT SELECT ON {table} TO {role};
"""

    @contextmanager
    def session_role(self, role: Optional[str]):
        """Assume ``role`` for one transaction, then give it back.

        ``SET LOCAL`` rather than ``SET``: it lasts until the transaction
        ends and no longer, so the pooled connection handed to the next
        request has already forgotten it. A role that outlived the request
        would be worse than none, because the next caller would run as
        somebody else.
        """
        if role is None:
            yield
            return

        conn = self._conn
        if conn is None:
            raise RuntimeError("not connected")

        quoted = _quoted_role(role)
        # SET LOCAL outside a transaction applies to nothing, and PostgreSQL
        # mentions it only in a notice. This backend may well be in
        # autocommit, where psycopg2 manages no transaction of its own, so
        # one is opened explicitly rather than assumed.
        explicit = bool(getattr(conn, "autocommit", False))
        with conn.cursor() as cur:
            if explicit:
                cur.execute("BEGIN")
            cur.execute(f"SET LOCAL ROLE {quoted}")
        try:
            yield
        finally:
            # Ending the transaction is what gives the role back, which is
            # the reason for SET LOCAL: the next request on this pooled
            # connection must not inherit it. A read has nothing to keep, so
            # roll back rather than commit.
            if explicit:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK")
            else:
                conn.rollback()

    def __init__(self, config: StorageConfig):

        self.config = config

        self._conn: Any = None

        self._lock = threading.Lock()

    def connect(self) -> None:

        try:
            import psycopg2

            from psycopg2.extras import RealDictCursor

        except ImportError:
            raise ImportError("Install psycopg2: pip install psycopg2-binary")

        ssl_mode = "require" if self.config.aurora_ssl else "disable"

        self._conn = psycopg2.connect(
            host=self.config.aurora_host,
            port=self.config.aurora_port,
            database=self.config.aurora_database,
            user=self.config.aurora_user,
            password=self.config.aurora_password,
            sslmode=ssl_mode,
        )

        self._conn.autocommit = True

        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

            cur.execute(f"""

                CREATE TABLE IF NOT EXISTS {self.config.aurora_schema}._vectrix_collections (

                    name VARCHAR(255) PRIMARY KEY,

                    dimension INTEGER,

                    metadata JSONB,

                    created_at TIMESTAMP DEFAULT NOW()

                )

            """)

    def close(self) -> None:

        if self._conn:
            self._conn.close()

            self._conn = None

    def _table_name(self, collection: str) -> str:

        return f"{self.config.aurora_schema}.{collection}"

    def _ensure_collection_table(
        self, collection: str, dimension: int = 384, mode: str = "dense"
    ) -> None:

        table = self._table_name(collection)

        with self._conn.cursor() as cur:
            cur.execute(f"""

                CREATE TABLE IF NOT EXISTS {table} (

                    id VARCHAR(255) PRIMARY KEY,

                    dense_embedding vector({dimension}),

                    sparse_embedding JSONB,

                    late_interaction_embedding JSONB,

                    text_content TEXT,

                    metadata JSONB,

                    created_at TIMESTAMP DEFAULT NOW(),

                    updated_at TIMESTAMP DEFAULT NOW()

                )

            """)

            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {collection}_hnsw_idx ON {table} USING hnsw (dense_embedding vector_cosine_ops)"
            )

            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {collection}_metadata_idx ON {table} USING gin (metadata)"
            )

            if mode in ("hybrid", "ultimate", "graph"):
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {collection}_sparse_idx ON {table} USING gin (sparse_embedding)"
                )

    def create_collection(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> None:

        dimension = metadata.get("dimension", 384) if metadata else 384

        mode = metadata.get("mode", "dense") if metadata else "dense"

        self._ensure_collection_table(name, dimension, mode)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""

                INSERT INTO {self.config.aurora_schema}._vectrix_collections (name, dimension, metadata)

                VALUES (%s, %s, %s)

                ON CONFLICT (name) DO NOTHING

            """,
                (name, dimension, json.dumps(metadata or {})),
            )

    def delete_collection(self, name: str) -> None:

        table = self._table_name(name)

        with self._conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")

            cur.execute(
                f"DELETE FROM {self.config.aurora_schema}._vectrix_collections WHERE name = %s",
                (name,),
            )

    def list_collections(self) -> List[str]:

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT name FROM {self.config.aurora_schema}._vectrix_collections")

            return [row[0] for row in cur.fetchall()]

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        """The metadata passed to create_collection, plus the dimension.

        This, ``scan`` and ``flush`` are abstract on BaseStorage and were
        missing here, so the class could not be instantiated: every
        construction raised TypeError before connect() was reached.
        """

        assert self._conn is not None, "connect() first"
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT dimension, metadata FROM {self.config.aurora_schema}._vectrix_collections "
                "WHERE name = %s",
                (name,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        dimension, metadata = row
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        config = dict(metadata or {})
        config.setdefault("dimension", dimension)
        return config

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Scan documents with pagination, oldest first."""

        table = self._table_name(collection)

        assert self._conn is not None, "connect() first"
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {table} ORDER BY created_at, id LIMIT %s OFFSET %s",
                (int(limit), int(offset)),
            )
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]

        for row in rows:
            raw = dict(zip(cols, row))
            data = self._row_to_document(raw)
            if filter_func is None or filter_func(data):
                yield raw["id"], data

    _STANDARD = (
        "id",
        "_embedding",
        "dense_embedding",
        "sparse_embedding",
        "late_interaction_embedding",
        "text_content",
        "created_at",
        "updated_at",
        "metadata",
    )

    def _row_to_document(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """The caller's document from a table row.

        The row carries the standard columns plus a metadata column holding
        whatever else the caller passed. Returning the row as it stands gave
        back a nested "metadata" dict instead of the fields that went in,
        which is not what any other backend does.
        """
        stored = row.get("metadata")
        if isinstance(stored, str) and stored:
            try:
                stored = json.loads(stored)
            except (ValueError, TypeError):
                stored = {}
        document: Dict[str, Any] = dict(stored or {})

        if row.get("text_content"):
            document["text_content"] = row["text_content"]

        for column in ("created_at", "updated_at"):
            if row.get(column) is not None:
                document[column] = row[column]

        vector = _as_float_list(row.get("dense_embedding"))
        if vector is not None:
            document["dense_embedding"] = vector

        for column in ("sparse_embedding", "late_interaction_embedding"):
            value = row.get(column)
            if isinstance(value, str) and value:
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    value = None
            if value:
                document[column] = value

        return document

    def flush(self) -> None:
        """Commit anything the connection is holding."""

        if self._conn is not None and not getattr(self._conn, "autocommit", False):
            self._conn.commit()

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:

        table = self._table_name(collection)

        dense = data.get("dense_embedding")
        if dense is None:
            dense = data.get("_embedding")

        sparse = json.dumps(data.get("sparse_embedding")) if data.get("sparse_embedding") else None

        late = (
            json.dumps(data.get("late_interaction_embedding"))
            if data.get("late_interaction_embedding")
            else None
        )

        text = data.get("text_content", "")

        # Everything that is not a column of its own travels in metadata.
        # This used to store only data["metadata"], so a flat document, which
        # is what Collection writes, lost its entire payload.
        carried = {k: v for k, v in data.items() if k not in self._STANDARD}
        carried.update(data.get("metadata") or {})

        meta = json.dumps(carried)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""

                INSERT INTO {table} (id, dense_embedding, sparse_embedding, late_interaction_embedding, text_content, metadata)

                VALUES (%s, %s, %s, %s, %s, %s)

                ON CONFLICT (id) DO UPDATE SET

                    dense_embedding = EXCLUDED.dense_embedding,

                    sparse_embedding = EXCLUDED.sparse_embedding,

                    late_interaction_embedding = EXCLUDED.late_interaction_embedding,

                    text_content = EXCLUDED.text_content,

                    metadata = EXCLUDED.metadata,

                    updated_at = NOW()

            """,
                (id, dense, sparse, late, text, meta),
            )

    def insert_batch(self, collection: str, items: List[Tuple[str, Dict[str, Any]]]) -> int:

        for id_, data in items:
            self.insert(collection, id_, data)

        return len(items)

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:

        table = self._table_name(collection)

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (id,))

            row = cur.fetchone()

            if row:
                cols = [desc[0] for desc in cur.description]

                return self._row_to_document(dict(zip(cols, row)))

        return None

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:

        return [self.get(collection, id) for id in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:

        table = self._table_name(collection)

        sets = []

        values = []

        for key, value in data.items():
            if key in (
                "dense_embedding",
                "sparse_embedding",
                "late_interaction_embedding",
                "text_content",
            ):
                if key in ("sparse_embedding", "late_interaction_embedding"):
                    value = json.dumps(value) if value else None

                sets.append(f"{key} = %s")

                values.append(value)

        # Anything that is not a column of its own lives in the metadata
        # blob, so it is merged into what is already stored. This loop used
        # to skip those keys entirely: updating an ordinary field wrote no
        # column at all and still reported success.
        carried = {k: v for k, v in data.items() if k not in self._STANDARD}
        carried.update(data.get("metadata") or {})

        if carried or "metadata" in data:
            current = self.get(collection, id) or {}
            merged = {k: v for k, v in current.items() if k not in self._STANDARD}
            merged.update(carried)

            sets.append("metadata = %s")

            values.append(json.dumps(merged))

        sets.append("updated_at = NOW()")

        values.append(id)

        with self._conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s", values)

            return cur.rowcount > 0

    def delete(self, collection: str, id: str) -> bool:

        table = self._table_name(collection)

        with self._conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE id = %s", (id,))

            return cur.rowcount > 0

    def delete_batch(self, collection: str, ids: List[str]) -> int:

        count = 0

        for id in ids:
            if self.delete(collection, id):
                count += 1

        return count

    def count(self, collection: str) -> int:

        table = self._table_name(collection)

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")

            return cur.fetchone()[0]

    def iterate(
        self, collection: str, batch_size: int = 1000
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:

        table = self._table_name(collection)

        offset = 0

        while True:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} LIMIT %s OFFSET %s", (batch_size, offset))

                rows = cur.fetchall()

                if not rows:
                    break

                cols = [desc[0] for desc in cur.description]

                for row in rows:
                    data = dict(zip(cols, row))

                    yield data["id"], data

                offset += batch_size

    def hybrid_search(
        self,
        collection: str,
        dense_query: List[float],
        sparse_query: Dict[int, float],
        limit: int = 10,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Dense and sparse results, fused with reciprocal rank fusion.

        Every other backend has one; this class ended at vector_search, so
        calling hybrid_search here raised AttributeError. Built from this
        backend's own vector_search and scan, and fused the way the others
        fuse, rather than pushed into SQL.

        It took a ``filter_sql`` WHERE fragment until 2.2, which selected the
        ids both halves were restricted to. That fragment was interpolated,
        so a value reaching it from a request could change the statement, and
        it is gone: these results are filtered by the collection, as they are
        for every other backend.
        """
        prefetch = max(limit * 10, limit)

        dense_results = self.vector_search(collection, dense_query, prefetch)

        sparse_results: List[Tuple[str, Dict[str, Any], float]] = []
        for id_, data in self.scan(collection, limit=max(prefetch * 10, 1000), offset=0):
            stored = data.get("sparse_embedding")
            if not stored:
                continue
            score = sum(sparse_query.get(int(k), 0.0) * float(v) for k, v in stored.items())
            if score > 0:
                payload = {k: v for k, v in data.items() if not k.endswith("_embedding")}
                sparse_results.append((id_, payload, score))

        sparse_results.sort(key=lambda row: row[2], reverse=True)
        sparse_results = sparse_results[:prefetch]

        rrf_k = 60
        scores: Dict[str, float] = {}
        payloads: Dict[str, Dict[str, Any]] = {}
        for ranked in (dense_results, sparse_results):
            for rank, (id_, data, _score) in enumerate(ranked):
                scores[id_] = scores.get(id_, 0.0) + 1.0 / (rrf_k + rank + 1)
                payloads.setdefault(id_, data)

        best = sorted(scores.items(), key=lambda row: row[1], reverse=True)[:limit]
        return [(id_, payloads[id_], score) for id_, score in best]

    def vector_search(
        self, collection: str, query_vector: List[float], limit: int = 10
    ) -> List[Tuple[str, Dict[str, Any], float]]:

        table = self._table_name(collection)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""

                SELECT id, text_content, metadata, dense_embedding <=> %s::vector AS distance

                FROM {table}

                ORDER BY distance

                LIMIT %s

            """,
                (query_vector, limit),
            )

            results = []

            for row in cur.fetchall():
                results.append((row[0], {"text_content": row[1], "metadata": row[2]}, row[3]))

            return results


# ============================================================================
# THE FACTORY
# ============================================================================
#
# INPUT   a StorageConfig
# OUTPUT  the backend it names, opened
#
# The one call the database makes to get a store.


def create_storage(config: StorageConfig) -> BaseStorage:
    """Factory function to create storage backend."""

    # "plugin:<name>" is a storage backend installed as an entry point.
    from ..plugins import load as _load_plugin, parse_ref as _parse_ref

    plugin_name = _parse_ref(config.backend)
    if plugin_name is not None:
        factory = _load_plugin("storage", plugin_name)
        plugged: BaseStorage = factory(config)
        connect = getattr(plugged, "connect", None)
        if callable(connect):
            connect()
        return plugged

    if config.backend == StorageBackend.MEMORY:
        return InMemoryStorage(config)

    elif config.backend == StorageBackend.SQLITE:
        storage: BaseStorage = SQLiteStorage(config)

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

    elif config.backend == StorageBackend.OPENSEARCH:
        storage = OpenSearchStorage(config)

        storage.connect()

        return storage

    elif config.backend == StorageBackend.AURORA_POSTGRESQL:
        storage = AuroraPostgreSQLStorage(config)

        storage.connect()

        return storage

    elif config.backend == StorageBackend.AZURE_SEARCH:
        from .storage_azure import AzureSearchStorage

        azure: BaseStorage = AzureSearchStorage(config)

        azure.connect()

        return azure

    elif config.backend == StorageBackend.POSTGRESQL:
        # Deprecated rather than removed: it is a public name, and the rule in
        # CONTRIBUTING is one minor release of warning first. It goes in 2.3.
        # Until then it does the useful thing instead of raising, because
        # AuroraPostgreSQLStorage is the pgvector backend it always meant.
        warnings.warn(
            "StorageBackend.POSTGRESQL is deprecated since 2.2 and will be removed "
            "in 2.3; use StorageBackend.AURORA_POSTGRESQL, which speaks pgvector "
            "over any PostgreSQL, Aurora or otherwise.",
            DeprecationWarning,
            stacklevel=2,
        )
        storage = AuroraPostgreSQLStorage(config)

        storage.connect()

        return storage

    else:
        supported = ", ".join(
            sorted(b.value for b in StorageBackend if b is not StorageBackend.POSTGRESQL)
        )
        raise ValueError(f"Unknown storage backend: {config.backend}. Supported: {supported}")


# Lives in its own module; imported last so it can subclass BaseStorage above.
from .storage_azure import AzureSearchStorage  # noqa: E402

if "AzureSearchStorage" not in __all__:
    __all__.append("AzureSearchStorage")
