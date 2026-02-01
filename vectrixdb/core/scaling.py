"""
VectrixDB Auto-Scaling - Dynamic scaling and resource management.

Features:
- Automatic index resizing based on load
- Memory pressure detection
- Query performance monitoring
- Shard management for horizontal scaling
- Connection pooling

Author: Daddy Nyame Owusu - Boakye
"""

import os
import psutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import deque
import logging

logger = logging.getLogger(__name__)


class ScalingStrategy(str, Enum):
    """Scaling strategies."""
    NONE = "none"  # No auto-scaling
    CONSERVATIVE = "conservative"  # Scale slowly, prioritize stability
    BALANCED = "balanced"  # Balance between performance and resources
    AGGRESSIVE = "aggressive"  # Scale quickly, prioritize performance


@dataclass
class ScalingConfig:
    """Configuration for auto-scaling."""
    strategy: ScalingStrategy = ScalingStrategy.BALANCED

    # Memory thresholds
    memory_target_percent: float = 70.0  # Target memory usage
    memory_high_watermark: float = 85.0  # Trigger scale-down
    memory_low_watermark: float = 50.0  # Can accept more load

    # Query performance thresholds (ms)
    latency_target_p95: float = 50.0  # Target P95 latency
    latency_max_p99: float = 200.0  # Max acceptable P99

    # Index sizing
    min_index_capacity: int = 1000
    max_index_capacity: int = 100_000_000
    index_growth_factor: float = 2.0  # Grow by 2x when needed

    # Connection pool
    min_connections: int = 5
    max_connections: int = 100
    connection_timeout: int = 30

    # Monitoring
    metrics_window_seconds: int = 300  # 5 minute window
    check_interval_seconds: int = 30

    # Sharding (for horizontal scaling)
    enable_sharding: bool = False
    shard_count: int = 1
    max_shards: int = 16

    @classmethod
    def from_env(cls) -> "ScalingConfig":
        """Create config from environment variables."""
        strategy = os.getenv("VECTRIX_SCALING_STRATEGY", "balanced")
        return cls(
            strategy=ScalingStrategy(strategy),
            memory_target_percent=float(os.getenv("VECTRIX_MEMORY_TARGET", "70")),
            max_index_capacity=int(os.getenv("VECTRIX_MAX_INDEX_SIZE", "100000000")),
        )


@dataclass
class PerformanceMetrics:
    """Performance metrics for a time window."""
    timestamp: datetime
    query_count: int = 0
    total_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    memory_used_bytes: int = 0
    memory_percent: float = 0.0
    cpu_percent: float = 0.0
    index_size: int = 0
    cache_hit_rate: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.query_count if self.query_count > 0 else 0


class MetricsCollector:
    """Collects and aggregates performance metrics."""

    def __init__(self, window_seconds: int = 300):
        self.window_seconds = window_seconds
        self._latencies: deque = deque(maxlen=10000)
        self._lock = threading.Lock()
        self._query_count = 0
        self._total_latency = 0.0
        self._last_snapshot = datetime.utcnow()

    def record_query(self, latency_ms: float) -> None:
        """Record a query's latency."""
        with self._lock:
            self._latencies.append(latency_ms)
            self._query_count += 1
            self._total_latency += latency_ms

    def get_metrics(self) -> PerformanceMetrics:
        """Get current metrics snapshot."""
        with self._lock:
            latencies = sorted(self._latencies)
            n = len(latencies)

            # Calculate percentiles
            p50 = latencies[int(n * 0.5)] if n > 0 else 0
            p95 = latencies[int(n * 0.95)] if n > 0 else 0
            p99 = latencies[int(n * 0.99)] if n > 0 else 0

            # System metrics
            memory = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.1)

            return PerformanceMetrics(
                timestamp=datetime.utcnow(),
                query_count=self._query_count,
                total_latency_ms=self._total_latency,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                p99_latency_ms=p99,
                memory_used_bytes=memory.used,
                memory_percent=memory.percent,
                cpu_percent=cpu,
            )

    def reset(self) -> None:
        """Reset metrics for new window."""
        with self._lock:
            self._latencies.clear()
            self._query_count = 0
            self._total_latency = 0.0
            self._last_snapshot = datetime.utcnow()


class ResourceMonitor:
    """Monitors system resources and triggers scaling actions."""

    def __init__(self, config: ScalingConfig):
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[PerformanceMetrics], None]] = []
        self._metrics = MetricsCollector(config.metrics_window_seconds)

    def start(self) -> None:
        """Start the resource monitor."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("Resource monitor started")

    def stop(self) -> None:
        """Stop the resource monitor."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Resource monitor stopped")

    def on_metrics(self, callback: Callable[[PerformanceMetrics], None]) -> None:
        """Register a callback for metrics updates."""
        self._callbacks.append(callback)

    def record_query(self, latency_ms: float) -> None:
        """Record a query execution."""
        self._metrics.record_query(latency_ms)

    def get_current_metrics(self) -> PerformanceMetrics:
        """Get current metrics."""
        return self._metrics.get_metrics()

    def get_current_stats(self) -> dict:
        """Get current metrics as a dictionary for API responses."""
        metrics = self._metrics.get_metrics()
        return {
            "timestamp": metrics.timestamp.isoformat(),
            "query_count": metrics.query_count,
            "avg_latency_ms": metrics.avg_latency_ms,
            "p50_latency_ms": metrics.p50_latency_ms,
            "p95_latency_ms": metrics.p95_latency_ms,
            "p99_latency_ms": metrics.p99_latency_ms,
            "memory_used_bytes": metrics.memory_used_bytes,
            "memory_percent": metrics.memory_percent,
            "cpu_percent": metrics.cpu_percent,
            "index_size": metrics.index_size,
            "cache_hit_rate": metrics.cache_hit_rate,
        }

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                metrics = self._metrics.get_metrics()

                # Notify callbacks
                for callback in self._callbacks:
                    try:
                        callback(metrics)
                    except Exception as e:
                        logger.error(f"Metrics callback error: {e}")

                # Reset for next window
                self._metrics.reset()

            except Exception as e:
                logger.error(f"Monitor error: {e}")

            time.sleep(self.config.check_interval_seconds)


class IndexScaler:
    """Manages index capacity scaling."""

    def __init__(self, config: ScalingConfig):
        self.config = config
        self._current_capacity: Dict[str, int] = {}
        self._lock = threading.Lock()

    def get_capacity(self, collection: str) -> int:
        """Get current index capacity for collection."""
        with self._lock:
            return self._current_capacity.get(collection, self.config.min_index_capacity)

    def set_capacity(self, collection: str, capacity: int) -> None:
        """Set current capacity."""
        with self._lock:
            self._current_capacity[collection] = capacity

    def should_grow(self, collection: str, current_count: int, current_capacity: int) -> bool:
        """Check if index should grow."""
        # Grow when 80% full
        threshold = current_capacity * 0.8
        return current_count >= threshold

    def calculate_new_capacity(self, current_capacity: int) -> int:
        """Calculate new capacity when growing."""
        new_capacity = int(current_capacity * self.config.index_growth_factor)
        return min(new_capacity, self.config.max_index_capacity)

    def should_shrink(self, collection: str, current_count: int, current_capacity: int) -> bool:
        """Check if index should shrink (to save memory)."""
        # Shrink when less than 25% full and above minimum
        if current_capacity <= self.config.min_index_capacity:
            return False
        threshold = current_capacity * 0.25
        return current_count < threshold

    def calculate_shrink_capacity(self, current_count: int) -> int:
        """Calculate new capacity when shrinking."""
        # Shrink to 2x current count, but not below minimum
        new_capacity = max(current_count * 2, self.config.min_index_capacity)
        return new_capacity


class MemoryManager:
    """Manages memory usage and triggers cleanup."""

    def __init__(self, config: ScalingConfig):
        self.config = config
        self._pressure_callbacks: List[Callable[[], None]] = []

    def on_memory_pressure(self, callback: Callable[[], None]) -> None:
        """Register callback for memory pressure events."""
        self._pressure_callbacks.append(callback)

    def check_memory(self) -> Tuple[float, bool]:
        """
        Check memory usage.

        Returns:
            Tuple of (memory_percent, is_under_pressure)
        """
        memory = psutil.virtual_memory()
        is_pressure = memory.percent >= self.config.memory_high_watermark

        if is_pressure:
            logger.warning(f"Memory pressure detected: {memory.percent:.1f}%")
            for callback in self._pressure_callbacks:
                try:
                    callback()
                except Exception as e:
                    logger.error(f"Memory pressure callback error: {e}")

        return memory.percent, is_pressure

    def get_available_memory_mb(self) -> float:
        """Get available memory in MB."""
        memory = psutil.virtual_memory()
        return memory.available / (1024 * 1024)

    def estimate_vector_memory(self, dimension: int, count: int, dtype_bytes: int = 4) -> float:
        """Estimate memory usage for vectors in MB."""
        # Vector data + overhead (~20% for index structures)
        vector_bytes = dimension * count * dtype_bytes
        overhead = vector_bytes * 0.2
        return (vector_bytes + overhead) / (1024 * 1024)


class ShardManager:
    """Manages collection sharding for horizontal scaling."""

    def __init__(self, config: ScalingConfig):
        self.config = config
        self._shards: Dict[str, List[str]] = {}  # collection -> list of shard names
        self._lock = threading.Lock()

    def get_shard_count(self, collection: str) -> int:
        """Get number of shards for collection."""
        with self._lock:
            return len(self._shards.get(collection, [collection]))

    def get_shard_for_id(self, collection: str, id: str) -> str:
        """Get the shard name for a given ID."""
        with self._lock:
            shards = self._shards.get(collection, [collection])
            if len(shards) == 1:
                return shards[0]

            # Simple hash-based sharding
            shard_idx = hash(id) % len(shards)
            return shards[shard_idx]

    def get_all_shards(self, collection: str) -> List[str]:
        """Get all shard names for a collection."""
        with self._lock:
            return self._shards.get(collection, [collection]).copy()

    def add_shard(self, collection: str) -> Optional[str]:
        """Add a new shard. Returns new shard name or None if at max."""
        with self._lock:
            shards = self._shards.setdefault(collection, [collection])

            if len(shards) >= self.config.max_shards:
                logger.warning(f"Cannot add shard: max shards ({self.config.max_shards}) reached")
                return None

            new_shard = f"{collection}_shard_{len(shards)}"
            shards.append(new_shard)
            logger.info(f"Added shard {new_shard} to {collection}")
            return new_shard

    def should_add_shard(
        self,
        collection: str,
        current_count: int,
        vectors_per_shard: int = 1_000_000
    ) -> bool:
        """Check if collection should be sharded."""
        if not self.config.enable_sharding:
            return False

        shard_count = self.get_shard_count(collection)
        avg_per_shard = current_count / shard_count

        return avg_per_shard > vectors_per_shard and shard_count < self.config.max_shards


class AutoScaler:
    """
    Main auto-scaling coordinator.

    Combines all scaling components:
    - Resource monitoring
    - Index capacity management
    - Memory management
    - Shard management
    """

    def __init__(
        self,
        config: ScalingConfig,
        resource_monitor: Optional[ResourceMonitor] = None
    ):
        self.config = config
        self.monitor = resource_monitor or ResourceMonitor(config)
        self.index_scaler = IndexScaler(config)
        self.memory_manager = MemoryManager(config)
        self.shard_manager = ShardManager(config)

        # Register metric handler
        self.monitor.on_metrics(self._on_metrics)

        # Scaling decision history
        self._decisions: deque = deque(maxlen=100)

    def start(self) -> None:
        """Start auto-scaling."""
        if self.config.strategy == ScalingStrategy.NONE:
            logger.info("Auto-scaling disabled")
            return

        self.monitor.start()
        logger.info(f"Auto-scaler started with strategy: {self.config.strategy}")

    def stop(self) -> None:
        """Stop auto-scaling."""
        self.monitor.stop()

    def register_index(self, name: str, index: Any) -> None:
        """Register an index for scaling management."""
        # Track initial capacity
        self.index_scaler.set_capacity(name, self.config.min_index_capacity)
        logger.debug(f"Registered index: {name}")

    def unregister_index(self, name: str) -> None:
        """Unregister an index from scaling management."""
        # Remove from tracking
        with self.index_scaler._lock:
            self.index_scaler._current_capacity.pop(name, None)
        logger.debug(f"Unregistered index: {name}")

    def record_query(self, latency_ms: float) -> None:
        """Record a query for metrics."""
        self.monitor.record_query(latency_ms)

    def should_scale_index(self, collection: str, current_count: int) -> Tuple[bool, int]:
        """
        Check if index should be scaled.

        Returns:
            Tuple of (should_scale, new_capacity)
        """
        current_capacity = self.index_scaler.get_capacity(collection)

        if self.index_scaler.should_grow(collection, current_count, current_capacity):
            new_capacity = self.index_scaler.calculate_new_capacity(current_capacity)
            self._record_decision(collection, "grow_index", current_capacity, new_capacity)
            return True, new_capacity

        if self.index_scaler.should_shrink(collection, current_count, current_capacity):
            new_capacity = self.index_scaler.calculate_shrink_capacity(current_count)
            self._record_decision(collection, "shrink_index", current_capacity, new_capacity)
            return True, new_capacity

        return False, current_capacity

    def check_memory_pressure(self) -> bool:
        """Check for memory pressure."""
        _, is_pressure = self.memory_manager.check_memory()
        return is_pressure

    def get_status(self) -> Dict[str, Any]:
        """Get current auto-scaler status."""
        metrics = self.monitor.get_current_metrics()
        return {
            "strategy": self.config.strategy.value,
            "metrics": {
                "query_count": metrics.query_count,
                "avg_latency_ms": metrics.avg_latency_ms,
                "p95_latency_ms": metrics.p95_latency_ms,
                "memory_percent": metrics.memory_percent,
                "cpu_percent": metrics.cpu_percent,
            },
            "recent_decisions": list(self._decisions)[-10:],
        }

    def _on_metrics(self, metrics: PerformanceMetrics) -> None:
        """Handle metrics update."""
        # Check latency thresholds
        if metrics.p95_latency_ms > self.config.latency_target_p95:
            logger.warning(
                f"P95 latency ({metrics.p95_latency_ms:.2f}ms) exceeds target "
                f"({self.config.latency_target_p95}ms)"
            )

        # Check memory
        if metrics.memory_percent > self.config.memory_high_watermark:
            logger.warning(f"Memory usage high: {metrics.memory_percent:.1f}%")

    def _record_decision(
        self,
        collection: str,
        action: str,
        old_value: Any,
        new_value: Any
    ) -> None:
        """Record a scaling decision."""
        decision = {
            "timestamp": datetime.utcnow().isoformat(),
            "collection": collection,
            "action": action,
            "old_value": old_value,
            "new_value": new_value,
        }
        self._decisions.append(decision)
        logger.info(f"Scaling decision: {action} for {collection}: {old_value} -> {new_value}")


# =============================================================================
# Connection Pool
# =============================================================================

class ConnectionPool:
    """Generic connection pool for database connections."""

    def __init__(self, config: ScalingConfig, factory: Callable[[], Any]):
        self.config = config
        self._factory = factory
        self._pool: deque = deque()
        self._in_use: int = 0
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(config.max_connections)

        # Pre-create minimum connections
        for _ in range(config.min_connections):
            self._pool.append(factory())

    def acquire(self, timeout: Optional[float] = None) -> Any:
        """Acquire a connection from the pool."""
        timeout = timeout or self.config.connection_timeout

        if not self._semaphore.acquire(timeout=timeout):
            raise TimeoutError("Could not acquire connection from pool")

        with self._lock:
            if self._pool:
                conn = self._pool.popleft()
            else:
                conn = self._factory()

            self._in_use += 1
            return conn

    def release(self, conn: Any) -> None:
        """Release a connection back to the pool."""
        with self._lock:
            self._pool.append(conn)
            self._in_use -= 1

        self._semaphore.release()

    def close_all(self) -> None:
        """Close all connections."""
        with self._lock:
            while self._pool:
                conn = self._pool.popleft()
                try:
                    if hasattr(conn, 'close'):
                        conn.close()
                except:
                    pass

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "available": len(self._pool),
            "in_use": self._in_use,
            "total": len(self._pool) + self._in_use,
        }
