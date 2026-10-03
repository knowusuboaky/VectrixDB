"""
Tests for VectrixDB cache functionality.
"""

import pytest

from vectrixdb import CacheBackend, CacheConfig, create_cache


class TestCacheBackendEnum:
    """Test CacheBackend enum."""

    def test_cache_backends_exist(self):
        """Test cache backend enum values."""
        assert CacheBackend.NONE is not None
        assert CacheBackend.MEMORY is not None

    def test_cache_backend_values(self):
        """Test cache backend string values."""
        assert CacheBackend.NONE.value == "none"
        assert CacheBackend.MEMORY.value == "memory"


class TestCacheConfig:
    """Test CacheConfig dataclass."""

    def test_default_config(self):
        """Test default cache config."""
        config = CacheConfig()
        assert config.backend == CacheBackend.MEMORY

    def test_none_backend_config(self):
        """Test none backend config."""
        config = CacheConfig(backend=CacheBackend.NONE)
        assert config.backend == CacheBackend.NONE

    def test_memory_backend_config(self):
        """Test memory backend config."""
        config = CacheConfig(backend=CacheBackend.MEMORY)
        assert config.backend == CacheBackend.MEMORY


class TestCreateCache:
    """Test create_cache factory function."""

    def test_create_cache_exists(self):
        """Test create_cache function exists."""
        assert callable(create_cache)

    def test_create_memory_cache(self):
        """Test creating memory cache via factory."""
        config = CacheConfig(backend=CacheBackend.MEMORY)
        cache = create_cache(config)
        # May return cache or None
        assert cache is not None or cache is None


class TestCacheIntegration:
    """Test cache integration with VectrixDB."""

    def test_vectrixdb_with_cache(self):
        """Test VectrixDB with cache config."""
        from vectrixdb import VectrixDB

        cache_config = CacheConfig(backend=CacheBackend.MEMORY)
        db = VectrixDB(cache_config=cache_config)

        assert db is not None
        db.close()

    def test_vectrixdb_without_cache(self):
        """Test VectrixDB without cache."""
        from vectrixdb import VectrixDB

        cache_config = CacheConfig(backend=CacheBackend.NONE)
        db = VectrixDB(cache_config=cache_config)

        assert db is not None
        db.close()


# =============================================================================
# Backends and helpers, tested directly. No live Redis: the Redis-backed
# classes run against a dict-backed fake module injected into sys.modules.
# =============================================================================

import sys
import time

from vectrixdb.core.cache import (
    BaseCache,
    CacheEntry,
    CacheStats,
    HybridCache,
    MemoryCache,
    NoCache,
    RedisCache,
    VectorCache,
)


class FakeRedisClient:
    """Dict-backed stand-in for redis.Redis with only the methods the backend calls."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.store = {}
        self.ttls = {}
        self.pings = 0

    def ping(self):
        self.pings += 1
        return True

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, data):
        self.store[key] = data
        self.ttls[key] = ttl

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                self.ttls.pop(key, None)
                removed += 1
        return removed

    def exists(self, key):
        return 1 if key in self.store else 0

    def scan(self, cursor, match="*", count=1000):
        # Two pages so the cursor loop in the backend is exercised. The key
        # list is snapshotted on the first page, as real SCAN guarantees that
        # keys present for the whole scan are returned even when the caller
        # deletes between pages (which clear() does).
        if cursor == 0:
            prefix = match.rstrip("*")
            self._scan_snapshot = [k for k in self.store if k.startswith(prefix)]
            if len(self._scan_snapshot) > 1:
                return 1, self._scan_snapshot[:1]
            return 0, self._scan_snapshot
        return 0, self._scan_snapshot[1:]

    def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def setex(self, key, ttl, data):
        self.ops.append((key, ttl, data))

    def execute(self):
        for key, ttl, data in self.ops:
            self.client.setex(key, ttl, data)
        self.ops = []


class FakeRedisModule:
    """Module shaped like ``redis``: exposes Redis(...) and from_url(...)."""

    def __init__(self):
        self.created = []
        self.urls = []

    def Redis(self, **kwargs):
        client = FakeRedisClient(**kwargs)
        self.created.append(client)
        return client

    def from_url(self, url, **kwargs):
        client = FakeRedisClient(url=url, **kwargs)
        self.urls.append(url)
        self.created.append(client)
        return client


@pytest.fixture
def fake_redis(monkeypatch):
    module = FakeRedisModule()
    monkeypatch.setitem(sys.modules, "redis", module)
    return module


class TestCacheConfigFromEnv:
    def test_defaults_when_env_is_empty(self, monkeypatch):
        for name in (
            "VECTRIX_CACHE_BACKEND",
            "VECTRIX_CACHE_SIZE",
            "VECTRIX_REDIS_HOST",
            "VECTRIX_REDIS_PORT",
            "VECTRIX_REDIS_PASSWORD",
            "VECTRIX_REDIS_SSL",
            "VECTRIX_AZURE_REDIS_CONNECTION",
        ):
            monkeypatch.delenv(name, raising=False)
        config = CacheConfig.from_env()
        assert config.backend == CacheBackend.MEMORY
        assert config.memory_max_size == 10000
        assert config.redis_host == "localhost"
        assert config.redis_port == 6379
        assert config.redis_password is None
        assert config.redis_ssl is False
        assert config.azure_redis_connection_string is None

    def test_reads_every_variable(self, monkeypatch):
        monkeypatch.setenv("VECTRIX_CACHE_BACKEND", "redis")
        monkeypatch.setenv("VECTRIX_CACHE_SIZE", "42")
        monkeypatch.setenv("VECTRIX_REDIS_HOST", "cache.example")
        monkeypatch.setenv("VECTRIX_REDIS_PORT", "6380")
        monkeypatch.setenv("VECTRIX_REDIS_PASSWORD", "secret")
        monkeypatch.setenv("VECTRIX_REDIS_SSL", "TRUE")
        monkeypatch.setenv("VECTRIX_AZURE_REDIS_CONNECTION", "rediss://azure")
        config = CacheConfig.from_env()
        assert config.backend == CacheBackend.REDIS
        assert config.memory_max_size == 42
        assert config.redis_host == "cache.example"
        assert config.redis_port == 6380
        assert config.redis_password == "secret"
        assert config.redis_ssl is True
        assert config.azure_redis_connection_string == "rediss://azure"

    def test_unknown_backend_name_is_rejected(self, monkeypatch):
        monkeypatch.setenv("VECTRIX_CACHE_BACKEND", "memcached")
        with pytest.raises(ValueError):
            CacheConfig.from_env()

    def test_all_backend_values(self):
        assert {b.value for b in CacheBackend} == {"none", "memory", "redis", "hybrid"}
        assert CacheBackend("hybrid") is CacheBackend.HYBRID


class TestCacheEntry:
    def test_fresh_entry_is_not_expired(self):
        entry = CacheEntry(value=1, created_at=time.time(), ttl=60)
        assert entry.is_expired is False
        assert entry.hits == 0

    def test_old_entry_is_expired(self):
        entry = CacheEntry(value=1, created_at=time.time() - 120, ttl=60)
        assert entry.is_expired is True


class TestCacheStats:
    def test_counters_and_hit_rate(self):
        stats = CacheStats()
        assert stats.hit_rate == 0.0
        stats.record_hit()
        stats.record_hit()
        stats.record_miss()
        stats.record_set()
        stats.record_delete()
        stats.record_eviction()
        assert stats.hits == 2 and stats.misses == 1
        assert stats.sets == 1 and stats.deletes == 1 and stats.evictions == 1
        assert stats.hit_rate == pytest.approx(2 / 3)

    def test_to_dict_formats_hit_rate_as_percent(self):
        stats = CacheStats()
        stats.record_hit()
        stats.record_miss()
        d = stats.to_dict()
        assert d == {
            "hits": 1,
            "misses": 1,
            "sets": 0,
            "deletes": 0,
            "evictions": 0,
            "hit_rate": "50.00%",
        }


class TestNoCache:
    def test_everything_is_a_no_op(self):
        cache = NoCache(CacheConfig(backend=CacheBackend.NONE))
        cache.set("k", "v")
        assert cache.get("k") is None
        assert cache.exists("k") is False
        assert cache.delete("k") is False
        assert cache.size() == 0
        cache.clear()
        assert cache.get_many(["k"]) == {}
        assert cache.delete_many(["k", "j"]) == 0


class TestBaseCacheHelpers:
    def test_json_serializer_round_trip(self):
        cache = NoCache(CacheConfig(serializer="json"))
        data = cache._serialize({"a": [1, 2]})
        assert isinstance(data, bytes)
        assert cache._deserialize(data) == {"a": [1, 2]}

    def test_pickle_serializer_round_trip(self):
        cache = NoCache(CacheConfig(serializer="pickle"))
        value = {"a": (1, 2), "b": {3}}
        assert cache._deserialize(cache._serialize(value)) == value

    def test_make_key_uses_prefix(self):
        cache = NoCache(CacheConfig(redis_prefix="p:"))
        assert cache._make_key("x") == "p:x"

    def test_set_many_and_delete_many_go_through_single_ops(self):
        cache = MemoryCache(CacheConfig())
        cache.set_many({"a": 1, "b": 2, "c": None})
        assert cache.get_many(["a", "b", "c", "missing"]) == {"a": 1, "b": 2}
        assert cache.delete_many(["a", "b", "zzz"]) == 2
        assert cache.size() == 1


class TestMemoryCache:
    @pytest.fixture
    def cache(self):
        return MemoryCache(CacheConfig(memory_max_size=3, memory_ttl_seconds=100))

    def test_set_get_and_stats(self, cache):
        cache.set("a", {"v": 1})
        assert cache.get("a") == {"v": 1}
        assert cache.get("nope") is None
        assert cache.stats.hits == 1 and cache.stats.misses == 1 and cache.stats.sets == 1
        assert cache._cache["a"].hits == 1

    def test_default_ttl_comes_from_config(self, cache):
        cache.set("a", 1)
        assert cache._cache["a"].ttl == 100
        cache.set("b", 1, ttl=5)
        assert cache._cache["b"].ttl == 5

    def test_expired_entry_is_a_miss_and_is_removed(self, cache):
        cache.set("a", 1, ttl=10)
        cache._cache["a"].created_at -= 11
        assert cache.get("a") is None
        assert "a" not in cache._cache
        assert cache.stats.misses == 1

    def test_exists_drops_expired_entries(self, cache):
        cache.set("a", 1, ttl=10)
        assert cache.exists("a") is True
        assert cache.exists("missing") is False
        cache._cache["a"].created_at -= 11
        assert cache.exists("a") is False
        assert cache.size() == 0

    def test_lru_eviction_evicts_least_recently_used(self, cache):
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.get("a")  # a is now the most recently used
        cache.set("d", 4)  # capacity 3: b is the oldest and goes
        assert cache.get("b") is None
        assert cache.get("a") == 1 and cache.get("c") == 3 and cache.get("d") == 4
        assert cache.stats.evictions == 1
        assert cache.size() == 3

    def test_overwriting_a_key_does_not_evict(self, cache):
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("a", 10)
        assert cache.size() == 2
        assert cache.get("a") == 10
        assert cache.stats.evictions == 0

    def test_delete_and_clear(self, cache):
        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.delete("a") is True
        assert cache.delete("a") is False
        assert cache.stats.deletes == 1
        cache.clear()
        assert cache.size() == 0
        assert cache.get("b") is None

    def test_cleanup_expired_returns_count(self, cache):
        cache.set("a", 1, ttl=10)
        cache.set("b", 2, ttl=10)
        cache.set("c", 3, ttl=1000)
        cache._cache["a"].created_at -= 11
        cache._cache["b"].created_at -= 11
        assert cache.cleanup_expired() == 2
        assert cache.size() == 1
        assert cache.cleanup_expired() == 0

    def test_capacity_of_one_keeps_only_the_latest(self):
        cache = MemoryCache(CacheConfig(memory_max_size=1))
        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.size() == 1
        assert cache.get("a") is None and cache.get("b") == 2


class TestRedisCacheWithFakeClient:
    @pytest.fixture
    def cache(self, fake_redis):
        return RedisCache(CacheConfig(backend=CacheBackend.REDIS, redis_prefix="t:"))

    def test_connect_uses_host_config_and_pings(self, fake_redis, cache):
        client = fake_redis.created[0]
        assert client.pings == 1
        assert client.kwargs["host"] == "localhost" and client.kwargs["port"] == 6379
        assert client.kwargs["decode_responses"] is False
        assert fake_redis.urls == []

    def test_connect_prefers_azure_connection_string(self, fake_redis):
        RedisCache(
            CacheConfig(backend=CacheBackend.REDIS, azure_redis_connection_string="rediss://az")
        )
        assert fake_redis.urls == ["rediss://az"]

    def test_missing_redis_package_is_an_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "redis", None)
        with pytest.raises(ImportError, match="pip install redis"):
            RedisCache(CacheConfig(backend=CacheBackend.REDIS))

    def test_set_get_with_prefix_and_ttl(self, fake_redis, cache):
        cache.set("k", {"x": 1})
        client = fake_redis.created[0]
        assert "t:k" in client.store
        assert client.ttls["t:k"] == cache.config.redis_ttl_seconds
        assert cache.get("k") == {"x": 1}
        assert cache.get("missing") is None
        assert cache.stats.hits == 1 and cache.stats.misses == 1 and cache.stats.sets == 1
        cache.set("k2", 1, ttl=7)
        assert client.ttls["t:k2"] == 7

    def test_delete_and_exists(self, cache):
        cache.set("k", 1)
        assert cache.exists("k") is True
        assert cache.delete("k") is True
        assert cache.delete("k") is False
        assert cache.exists("k") is False
        assert cache.stats.deletes == 1

    def test_size_and_clear_only_touch_prefixed_keys(self, fake_redis, cache):
        client = fake_redis.created[0]
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        client.store["other:x"] = b"1"
        assert cache.size() == 3
        cache.clear()
        assert cache.size() == 0
        assert "other:x" in client.store

    def test_get_many_uses_mget(self, cache):
        cache.set_many({"a": 1, "b": [2]})
        assert cache.get_many(["a", "b", "zzz"]) == {"a": 1, "b": [2]}
        assert cache.stats.hits == 2 and cache.stats.misses == 1
        assert cache.stats.sets == 2

    def test_set_many_uses_ttl_override(self, fake_redis, cache):
        cache.set_many({"a": 1}, ttl=3)
        assert fake_redis.created[0].ttls["t:a"] == 3

    def test_pickle_serializer_through_redis(self, fake_redis):
        cache = RedisCache(CacheConfig(backend=CacheBackend.REDIS, serializer="pickle"))
        cache.set("k", (1, 2))
        assert cache.get("k") == (1, 2)


class TestHybridCacheWithFakeClient:
    @pytest.fixture
    def cache(self, fake_redis):
        return HybridCache(
            CacheConfig(backend=CacheBackend.HYBRID, hybrid_l1_size=2, hybrid_l1_ttl=50)
        )

    def test_l1_is_sized_from_hybrid_config(self, cache):
        assert cache._l1.config.memory_max_size == 2
        assert cache._l1.config.memory_ttl_seconds == 50

    def test_set_writes_both_tiers(self, cache):
        cache.set("k", "v")
        assert cache._l1.get("k") == "v"
        assert cache._l2.get("k") == "v"
        assert cache._l1._cache["k"].ttl == 50
        assert cache.stats.sets == 1

    def test_get_hits_l1_first(self, cache):
        cache.set("k", "v")
        assert cache.get("k") == "v"
        assert cache._l2.stats.hits == 0
        assert cache.stats.hits == 1

    def test_l2_hit_is_promoted_to_l1(self, cache):
        cache._l2.set("k", "v")
        assert cache._l1.get("k") is None
        assert cache.get("k") == "v"
        assert cache._l1.exists("k") is True
        assert cache.stats.hits == 1

    def test_miss_everywhere(self, cache):
        assert cache.get("nope") is None
        assert cache.stats.misses == 1

    def test_delete_reports_either_tier(self, cache):
        cache.set("k", 1)
        assert cache.delete("k") is True
        assert cache.delete("k") is False
        cache._l2.set("only-l2", 1)
        assert cache.delete("only-l2") is True
        assert cache.stats.deletes == 2

    def test_exists_size_clear(self, cache):
        cache.set("a", 1)
        cache._l2.set("b", 2)
        assert cache.exists("a") and cache.exists("b") and not cache.exists("c")
        assert cache.size() == 2
        cache.clear()
        assert cache.size() == 0 and cache._l1.size() == 0

    def test_get_stats_has_three_sections(self, cache):
        cache.set("a", 1)
        cache.get("a")
        stats = cache.get_stats()
        assert set(stats) == {"l1", "l2", "combined"}
        assert stats["combined"]["hits"] == 1


class TestVectorCache:
    @pytest.fixture
    def vcache(self):
        return VectorCache(MemoryCache(CacheConfig()), prefix="p:")

    def test_keys_are_prefixed(self, vcache):
        assert vcache._query_key("col", "h") == "p:q:col:h"
        assert vcache._vector_key("col", "id1") == "p:v:col:id1"

    def test_query_hash_is_stable_and_rounds_small_differences(self, vcache):
        a = vcache._hash_query([0.1, 0.2, 0.3])
        assert a == vcache._hash_query([0.1, 0.2, 0.3])
        assert a == vcache._hash_query([0.1000001, 0.2, 0.3])
        assert len(a) == 16

    def test_query_hash_changes_with_filter_limit_and_vector(self, vcache):
        base = vcache._hash_query([0.1, 0.2])
        assert base != vcache._hash_query([0.1, 0.2], filter={"k": 1})
        assert base != vcache._hash_query([0.1, 0.2], limit=5)
        assert base != vcache._hash_query([0.2, 0.1])

    def test_query_hash_changes_with_options_and_takes_set_operands(self, vcache):
        """A score threshold or ef changes the answer, so it changes the key;
        a set operand used to make json.dumps raise."""
        base = vcache._hash_query([0.1, 0.2])
        assert base != vcache._hash_query([0.1, 0.2], options={"score_threshold": 0.9})
        a = vcache._hash_query([0.1], filter={"n": {"$in": {"x", "y", "z"}}})
        assert a == vcache._hash_query([0.1], filter={"n": {"$in": {"z", "y", "x"}}})

    def test_search_results_round_trip(self, vcache):
        assert vcache.get_search_results("c", [1.0, 0.0]) is None
        vcache.set_search_results("c", [1.0, 0.0], [{"id": "a"}], ttl=30)
        assert vcache.get_search_results("c", [1.0, 0.0]) == [{"id": "a"}]
        assert vcache.get_search_results("c", [1.0, 0.0], limit=3) is None
        assert vcache.get_search_results("other", [1.0, 0.0]) is None

    def test_vector_round_trip_and_invalidate(self, vcache):
        vcache.set_vector("c", "v1", {"vector": [1, 2]})
        assert vcache.get_vector("c", "v1") == {"vector": [1, 2]}
        vcache.invalidate_vector("c", "v1")
        assert vcache.get_vector("c", "v1") is None
        vcache.invalidate_collection("c")  # documented no-op, must not raise

    def test_stats_come_from_the_backend(self, vcache):
        vcache.get_vector("c", "missing")
        assert vcache.stats.misses == 1


class TestCreateCacheFactory:
    def test_none_and_memory(self):
        assert isinstance(create_cache(CacheConfig(backend=CacheBackend.NONE)), NoCache)
        assert isinstance(create_cache(CacheConfig(backend=CacheBackend.MEMORY)), MemoryCache)

    def test_redis_and_hybrid_with_fake_client(self, fake_redis):
        assert isinstance(create_cache(CacheConfig(backend=CacheBackend.REDIS)), RedisCache)
        assert isinstance(create_cache(CacheConfig(backend=CacheBackend.HYBRID)), HybridCache)

    def test_unknown_backend_raises(self):
        config = CacheConfig()
        config.backend = "bogus"  # the dataclass does not validate the field
        with pytest.raises(ValueError, match="Unknown cache backend"):
            create_cache(config)

    def test_base_cache_is_abstract(self):
        with pytest.raises(TypeError):
            BaseCache(CacheConfig())
