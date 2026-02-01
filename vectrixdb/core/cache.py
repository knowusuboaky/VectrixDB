"""
VectrixDB Cache Layer - High-performance caching for low latency.

Supports multiple cache backends:
- InMemory LRU: Ultra-fast, limited by RAM
- Redis: Distributed cache (Azure Redis, Docker Redis, local)
- Hybrid: Memory + Redis tiered caching

Author: Daddy Nyame Owusu - Boakye
"""

import hashlib
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
import pickle


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
        ttl = ttl or self.config.memory_ttl_seconds

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
        self._client = None
        self._connect()

    def _connect(self):
        try:
            import redis
        except ImportError:
            raise ImportError("redis is required. Install with: pip install redis")

        # Azure Redis connection string
        if self.config.azure_redis_connection_string:
            self._client = redis.from_url(
                self.config.azure_redis_connection_string,
                decode_responses=False
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
        ttl = ttl or self.config.redis_ttl_seconds
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
        ttl = ttl or self.config.redis_ttl_seconds

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


# =============================================================================
# Specialized Caches for VectrixDB
# =============================================================================

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

    def _hash_query(self, query: List[float], filter: Optional[Dict] = None, limit: int = 10) -> str:
        """Create a hash for a query."""
        data = json.dumps({
            "q": [round(v, 6) for v in query],  # Round for cache hits
            "f": filter,
            "l": limit,
        }, sort_keys=True)
        return hashlib.md5(data.encode()).hexdigest()[:16]

    def get_search_results(
        self,
        collection: str,
        query: List[float],
        filter: Optional[Dict] = None,
        limit: int = 10
    ) -> Optional[List[Dict]]:
        """Get cached search results."""
        query_hash = self._hash_query(query, filter, limit)
        key = self._query_key(collection, query_hash)
        return self._cache.get(key)

    def set_search_results(
        self,
        collection: str,
        query: List[float],
        results: List[Dict],
        filter: Optional[Dict] = None,
        limit: int = 10,
        ttl: int = 300
    ) -> None:
        """Cache search results."""
        query_hash = self._hash_query(query, filter, limit)
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
        """Invalidate all cache entries for a collection."""
        # This requires pattern-based deletion
        # For memory cache, we'd need to scan
        # For Redis, we can use SCAN with pattern
        pass

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
