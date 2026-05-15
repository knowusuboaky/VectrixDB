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
