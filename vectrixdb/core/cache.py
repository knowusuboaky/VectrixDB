"""
VectrixDB Cache Layer - High-performance caching for low latency.

Supports multiple cache backends:
- InMemory LRU: Ultra-fast, limited by RAM
- Redis: Distributed cache (Azure Redis, Docker Redis, local)
- Hybrid: Memory + Redis tiered caching

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

import hashlib
import json

import numpy as np
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
import pickle


__all__ = [
    "CacheBackend",
    "CacheConfig",
    "CacheEntry",
    "CacheStats",
    "BaseCache",
    "NoCache",
    "MemoryCache",
    "RedisCache",
    "HybridCache",
    "VectorCache",
    "create_cache",
]


# ============================================================================
# THE BACKENDS, THE CONFIG, AN ENTRY, AND THE STATS
# ============================================================================
#
# INPUT   a configuration
# OUTPUT  which backend, its settings, one cached entry with its metadata, and
#         the statistics monitoring reads
#
# In-memory LRU, Redis, or both.


class CacheBackend(str, Enum):
    """Available cache backends."""

    NONE = "none"
    MEMORY = "memory"
    REDIS = "redis"
    HYBRID = "hybrid"  # Memory L1 + Redis L2


@dataclass
class CacheConfig:
    """Configuration for cache layer."""

    backend: CacheBackend = CacheBackend.MEMORY

    # Memory cache config
    memory_max_size: int = 10000  # Max items in memory
    memory_ttl_seconds: int = 3600  # Default TTL

    # Redis config
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_db: int = 0
    redis_ssl: bool = False  # True for Azure Redis
    redis_prefix: str = "vectrix:"
    redis_ttl_seconds: int = 86400  # 24 hours

    # Azure Redis specific
    azure_redis_connection_string: Optional[str] = None

    # Hybrid config (L1 memory, L2 Redis)
    hybrid_l1_size: int = 1000
    hybrid_l1_ttl: int = 300  # 5 minutes

    # Performance
    compression: bool = True  # Compress cached values
    serializer: str = "json"  # "json" or "pickle"

    @classmethod
    def from_env(cls) -> "CacheConfig":
        """Create config from environment variables."""
        backend = os.getenv("VECTRIX_CACHE_BACKEND", "memory")
        return cls(
            backend=CacheBackend(backend),
            memory_max_size=int(os.getenv("VECTRIX_CACHE_SIZE", "10000")),
            redis_host=os.getenv("VECTRIX_REDIS_HOST", "localhost"),
            redis_port=int(os.getenv("VECTRIX_REDIS_PORT", "6379")),
            redis_password=os.getenv("VECTRIX_REDIS_PASSWORD"),
            redis_ssl=os.getenv("VECTRIX_REDIS_SSL", "").lower() == "true",
            azure_redis_connection_string=os.getenv("VECTRIX_AZURE_REDIS_CONNECTION"),
        )


@dataclass
class CacheEntry:
    """A cached entry with metadata."""

    value: Any
    created_at: float
    ttl: int
    hits: int = 0

    @property
    def is_expired(self) -> bool:
        return time.time() > self.created_at + self.ttl


class CacheStats:
    """Cache statistics for monitoring."""

    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.deletes = 0
        self.evictions = 0
        self.size = 0  # Number of cached items
        self.memory_bytes = 0  # Estimated memory usage
        self._lock = threading.Lock()

    def record_hit(self):
        with self._lock:
            self.hits += 1

    def record_miss(self):
        with self._lock:
            self.misses += 1

    def record_set(self):
        with self._lock:
            self.sets += 1

    def record_delete(self):
        with self._lock:
            self.deletes += 1

    def record_eviction(self):
        with self._lock:
            self.evictions += 1

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "deletes": self.deletes,
            "evictions": self.evictions,
            "hit_rate": f"{self.hit_rate:.2%}",
        }


# ============================================================================
# THE CACHES: none, memory, Redis, and hybrid
# ============================================================================
#
# INPUT   a key and a value, with a TTL
# OUTPUT  the same get, set and invalidate on each: a no-op, an in-memory LRU,
#         Redis, or memory in front of Redis
#
# One base class, so a collection does not know which it has.


class BaseCache(ABC):
    """Abstract base class for cache backends."""

    def __init__(self, config: CacheConfig):
        self.config = config
        self.stats = CacheStats()

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Get a value from cache."""
        pass

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Set a value in cache."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a key from cache."""
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all cache entries."""
        pass

    def delete_prefix(self, prefix: str) -> int:
        """Forget every key that starts with ``prefix``; how many went.

        A cache that cannot look its keys up by prefix forgets everything,
        because forgetting too much costs a recomputation and forgetting
        too little returns an answer that is no longer true.
        """
        self.clear()
        return 0

    @abstractmethod
    def size(self) -> int:
        """Get number of cached items."""
        pass

    def get_many(self, keys: List[str]) -> Dict[str, Any]:
        """Get multiple values."""
        return {k: self.get(k) for k in keys if self.get(k) is not None}

    def set_many(self, items: Dict[str, Any], ttl: Optional[int] = None) -> None:
        """Set multiple values."""
        for key, value in items.items():
            self.set(key, value, ttl)

    def delete_many(self, keys: List[str]) -> int:
        """Delete multiple keys."""
        count = 0
        for key in keys:
            if self.delete(key):
                count += 1
        return count

    def _serialize(self, value: Any) -> bytes:
        """Serialize value for storage."""
        if self.config.serializer == "pickle":
            return pickle.dumps(value)
        else:
            return json.dumps(value).encode("utf-8")

    def _deserialize(self, data: bytes) -> Any:
        """Deserialize value from storage."""
        if self.config.serializer == "pickle":
            return pickle.loads(data)
        else:
            return json.loads(data.decode("utf-8"))

    def _make_key(self, key: str) -> str:
        """Create a prefixed cache key."""
        return f"{self.config.redis_prefix}{key}"


class NoCache(BaseCache):
    """No-op cache (disabled)."""

    def get(self, key: str) -> Optional[Any]:
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        pass

    def delete(self, key: str) -> bool:
        return False

    def delete_prefix(self, prefix: str) -> int:
        return 0

    def exists(self, key: str) -> bool:
        return False

    def clear(self) -> None:
        pass

    def size(self) -> int:
        return 0


class MemoryCache(BaseCache):
    """
    In-memory LRU cache with TTL support.

    Ultra-low latency (<1ms), but limited by available RAM.
    Best for:
    - Hot data caching
    - Session data
    - Frequently accessed vectors
    """

    def __init__(self, config: CacheConfig):
        super().__init__(config)
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self.stats.record_miss()
                return None

            if entry.is_expired:
                del self._cache[key]
                self.stats.record_miss()
                return None

            # Move to end (LRU)
            self._cache.move_to_end(key)
            entry.hits += 1
            self.stats.record_hit()
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        # ``is None``: a caller passing ttl=0 is asking for this not to be
        # kept, and reading that as "no ttl given" cached it for the
        # default hour and served it back.
        ttl = self.config.memory_ttl_seconds if ttl is None else ttl

        with self._lock:
            # Evict if at capacity
            while len(self._cache) >= self.config.memory_max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                self.stats.record_eviction()

            self._cache[key] = CacheEntry(
                value=value,
                created_at=time.time(),
                ttl=ttl,
            )
            self._cache.move_to_end(key)
            self.stats.record_set()

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self.stats.record_delete()
                return True
            return False

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            doomed = [k for k in self._cache if isinstance(k, str) and k.startswith(prefix)]
            for key in doomed:
                del self._cache[key]
                self.stats.record_delete()
            return len(doomed)

    def exists(self, key: str) -> bool:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False
            if entry.is_expired:
                del self._cache[key]
                return False
            return True

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        return len(self._cache)

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        with self._lock:
            expired_keys = [k for k, v in self._cache.items() if v.is_expired]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)


class RedisCache(BaseCache):
    """
    Redis-based distributed cache.

    Supports:
    - Local Redis (Docker/native)
    - Azure Cache for Redis
    - Redis Cluster

    Requires: pip install redis
    """

    def __init__(self, config: CacheConfig):
        super().__init__(config)
        self._client: Any = None
        self._connect()

    def _connect(self):
        try:
            import redis
        except ImportError:
            raise ImportError("redis is required. Install with: pip install redis")

        # Azure Redis connection string
        if self.config.azure_redis_connection_string:
            self._client = redis.from_url(
                self.config.azure_redis_connection_string, decode_responses=False
            )
        else:
            self._client = redis.Redis(
                host=self.config.redis_host,
                port=self.config.redis_port,
                password=self.config.redis_password,
                db=self.config.redis_db,
                ssl=self.config.redis_ssl,
                decode_responses=False,
                socket_timeout=5,
                socket_connect_timeout=5,
            )

        # Test connection
        self._client.ping()

    def get(self, key: str) -> Optional[Any]:
        full_key = self._make_key(key)
        data = self._client.get(full_key)

        if data is None:
            self.stats.record_miss()
            return None

        self.stats.record_hit()
        return self._deserialize(data)

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        ttl = self.config.redis_ttl_seconds if ttl is None else ttl
        full_key = self._make_key(key)
        data = self._serialize(value)

        self._client.setex(full_key, ttl, data)
        self.stats.record_set()

    def delete(self, key: str) -> bool:
        full_key = self._make_key(key)
        result = self._client.delete(full_key) > 0
        if result:
            self.stats.record_delete()
        return result

    def delete_prefix(self, prefix: str) -> int:
        # SCAN, never KEYS: this runs on every write, against a server that
        # other things are using.
        pattern = self._make_key(prefix).replace("*", "[*]").replace("?", "[?]") + "*"
        removed = 0
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match=pattern, count=1000)
            if keys:
                removed += int(self._client.delete(*keys) or 0)
            if cursor == 0:
                break
        return removed

    def exists(self, key: str) -> bool:
        full_key = self._make_key(key)
        return self._client.exists(full_key) > 0

    def clear(self) -> None:
        # Only clear keys with our prefix
        pattern = f"{self.config.redis_prefix}*"
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match=pattern, count=1000)
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break

    def size(self) -> int:
        pattern = f"{self.config.redis_prefix}*"
        count = 0
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match=pattern, count=1000)
            count += len(keys)
            if cursor == 0:
                break
        return count

    def get_many(self, keys: List[str]) -> Dict[str, Any]:
        """Optimized batch get using MGET."""
        full_keys = [self._make_key(k) for k in keys]
        values = self._client.mget(full_keys)

        result = {}
        for key, value in zip(keys, values):
            if value is not None:
                result[key] = self._deserialize(value)
                self.stats.record_hit()
            else:
                self.stats.record_miss()

        return result

    def set_many(self, items: Dict[str, Any], ttl: Optional[int] = None) -> None:
        """Optimized batch set using pipeline."""
        ttl = self.config.redis_ttl_seconds if ttl is None else ttl

        pipe = self._client.pipeline()
        for key, value in items.items():
            full_key = self._make_key(key)
            data = self._serialize(value)
            pipe.setex(full_key, ttl, data)
            self.stats.record_set()

        pipe.execute()


class HybridCache(BaseCache):
    """
    Two-tier cache: Memory (L1) + Redis (L2).

    Provides:
    - Ultra-low latency for hot data (L1)
    - Large capacity with persistence (L2)
    - Automatic promotion/demotion between tiers

    Best for high-throughput production systems.
    """

    def __init__(self, config: CacheConfig):
        super().__init__(config)

        # L1: Small, fast memory cache
        l1_config = CacheConfig(
            backend=CacheBackend.MEMORY,
            memory_max_size=config.hybrid_l1_size,
            memory_ttl_seconds=config.hybrid_l1_ttl,
        )
        self._l1 = MemoryCache(l1_config)

        # L2: Large, persistent Redis cache
        self._l2 = RedisCache(config)

    def get(self, key: str) -> Optional[Any]:
        # Try L1 first
        value = self._l1.get(key)
        if value is not None:
            self.stats.record_hit()
            return value

        # Try L2
        value = self._l2.get(key)
        if value is not None:
            # Promote to L1
            self._l1.set(key, value)
            self.stats.record_hit()
            return value

        self.stats.record_miss()
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        # Write to both tiers
        self._l1.set(key, value, self.config.hybrid_l1_ttl)
        self._l2.set(key, value, ttl)
        self.stats.record_set()

    def delete(self, key: str) -> bool:
        l1_deleted = self._l1.delete(key)
        l2_deleted = self._l2.delete(key)
        if l1_deleted or l2_deleted:
            self.stats.record_delete()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._l1.exists(key) or self._l2.exists(key)

    def delete_prefix(self, prefix: str) -> int:
        return self._l1.delete_prefix(prefix) + self._l2.delete_prefix(prefix)

    def clear(self) -> None:
        self._l1.clear()
        self._l2.clear()

    def size(self) -> int:
        # L2 is the source of truth
        return self._l2.size()

    def get_stats(self) -> Dict[str, Any]:
        return {
            "l1": self._l1.stats.to_dict(),
            "l2": self._l2.stats.to_dict(),
            "combined": self.stats.to_dict(),
        }


# ============================================================================
# SPECIALISED CACHES, AND THE FACTORY
# ============================================================================
#
# INPUT   a search's vector and options; a configuration
# OUTPUT  its results cached under a key made from them; the backend the
#         configuration names
#
# A search cache keys on everything that changes the answer.


def _json_key_default(value: Any) -> Any:
    """JSON for a cache key's non-JSON values, the same on every call.

    A set operand (``{"$in": {"a", "b"}}``) made json.dumps raise, and its
    iteration order is not stable across processes, so it is sorted.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    return repr(value)


class VectorCache:
    """
    Specialized cache for vector search results.

    Features:
    - Query result caching
    - Vector embedding caching
    - Automatic cache invalidation
    """

    def __init__(self, cache: BaseCache, prefix: str = "vec:"):
        self._cache = cache
        self._prefix = prefix

    def _query_key(self, collection: str, query_hash: str) -> str:
        return f"{self._prefix}q:{collection}:{query_hash}"

    def _vector_key(self, collection: str, vector_id: str) -> str:
        return f"{self._prefix}v:{collection}:{vector_id}"

    def _hash_query(
        self,
        query: List[float],
        filter: Optional[Dict] = None,
        limit: int = 10,
        options: Optional[Dict] = None,
    ) -> str:
        """Create a hash for a query.

        The vector is hashed as rounded float32 bytes rather than through
        JSON: a 384-float dump with a Python round() per element cost more
        than the index search it was caching. Rounding keeps near-identical
        queries on the same key. ``options`` holds anything else that
        changes the answer, such as a score threshold or ef.
        """
        vector = np.round(np.asarray(query, dtype=np.float32), 5).tobytes()
        rest = json.dumps(
            {"f": filter, "l": limit, "o": options}, sort_keys=True, default=_json_key_default
        ).encode()
        return hashlib.md5(vector + rest).hexdigest()[:16]  # noqa: S324 - not security

    def get_search_results(
        self,
        collection: str,
        query: List[float],
        filter: Optional[Dict] = None,
        limit: int = 10,
        options: Optional[Dict] = None,
    ) -> Optional[List[Dict]]:
        """Get cached search results."""
        query_hash = self._hash_query(query, filter, limit, options)
        key = self._query_key(collection, query_hash)
        return self._cache.get(key)

    def set_search_results(
        self,
        collection: str,
        query: List[float],
        results: List[Dict],
        filter: Optional[Dict] = None,
        limit: int = 10,
        ttl: int = 300,
        options: Optional[Dict] = None,
    ) -> None:
        """Cache search results."""
        query_hash = self._hash_query(query, filter, limit, options)
        key = self._query_key(collection, query_hash)
        self._cache.set(key, results, ttl)

    def get_vector(self, collection: str, vector_id: str) -> Optional[Dict]:
        """Get cached vector data."""
        key = self._vector_key(collection, vector_id)
        return self._cache.get(key)

    def set_vector(self, collection: str, vector_id: str, data: Dict, ttl: int = 3600) -> None:
        """Cache vector data."""
        key = self._vector_key(collection, vector_id)
        self._cache.set(key, data, ttl)

    def invalidate_collection(self, collection: str) -> None:
        """Forget every cached search and cached vector of a collection.

        Called after every write. It was an empty function, so a search
        repeated after an add or a delete returned the answer from before
        it, for as long as the entry lived: an hour in memory, a day in
        Redis.
        """
        for kind in ("q", "v"):
            self._cache.delete_prefix(f"{self._prefix}{kind}:{collection}:")

    def invalidate_vector(self, collection: str, vector_id: str) -> None:
        """Invalidate cached vector data."""
        key = self._vector_key(collection, vector_id)
        self._cache.delete(key)

    @property
    def stats(self) -> CacheStats:
        return self._cache.stats


def create_cache(config: CacheConfig) -> BaseCache:
    """Factory function to create cache backend."""
    if config.backend == CacheBackend.NONE:
        return NoCache(config)
    elif config.backend == CacheBackend.MEMORY:
        return MemoryCache(config)
    elif config.backend == CacheBackend.REDIS:
        return RedisCache(config)
    elif config.backend == CacheBackend.HYBRID:
        return HybridCache(config)
    else:
        raise ValueError(f"Unknown cache backend: {config.backend}")
