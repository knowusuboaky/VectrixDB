"""
Metrics Collector

Collects and computes benchmark metrics during execution.
"""

import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np


@dataclass
class MetricsSnapshot:
    """Snapshot of metrics at a point in time."""
    timestamp: float
    memory_mb: float
    latency_ms: Optional[float] = None
    custom: Dict[str, float] = field(default_factory=dict)


class MetricsCollector:
    """
    Collects and computes benchmark metrics.

    Tracks latencies, memory usage, and custom metrics during execution.

    Example:
        >>> collector = MetricsCollector()
        >>> collector.start()
        >>> for item in items:
        ...     start = time.perf_counter()
        ...     process(item)
        ...     collector.record_latency((time.perf_counter() - start) * 1000)
        >>> metrics = collector.stop()
    """

    def __init__(self, track_memory: bool = True):
        """
        Initialize metrics collector.

        Args:
            track_memory: Enable memory tracking
        """
        self.track_memory = track_memory

        self._latencies: List[float] = []
        self._memory_samples: List[float] = []
        self._snapshots: List[MetricsSnapshot] = []
        self._custom_metrics: Dict[str, List[float]] = {}

        self._start_time: Optional[float] = None
        self._start_memory: int = 0

    def start(self) -> None:
        """Start collecting metrics."""
        self._latencies = []
        self._memory_samples = []
        self._snapshots = []
        self._custom_metrics = {}

        self._start_time = time.perf_counter()

        if self.track_memory:
            tracemalloc.start()
            self._start_memory = tracemalloc.get_traced_memory()[0]

    def record_latency(self, latency_ms: float) -> None:
        """
        Record a single operation latency.

        Args:
            latency_ms: Latency in milliseconds
        """
        self._latencies.append(latency_ms)

    def sample_memory(self) -> float:
        """
        Sample current memory usage.

        Returns:
            Memory usage in MB
        """
        if not self.track_memory:
            return 0.0

        current, _ = tracemalloc.get_traced_memory()
        memory_mb = current / (1024 * 1024)
        self._memory_samples.append(memory_mb)
        return memory_mb

    def record_custom(self, name: str, value: float) -> None:
        """
        Record a custom metric.

        Args:
            name: Metric name
            value: Metric value
        """
        if name not in self._custom_metrics:
            self._custom_metrics[name] = []
        self._custom_metrics[name].append(value)

    def snapshot(self, custom: Optional[Dict[str, float]] = None) -> MetricsSnapshot:
        """
        Take a snapshot of current metrics.

        Args:
            custom: Optional custom metrics to include

        Returns:
            MetricsSnapshot
        """
        elapsed = time.perf_counter() - self._start_time if self._start_time else 0
        memory = self.sample_memory()

        snapshot = MetricsSnapshot(
            timestamp=elapsed,
            memory_mb=memory,
            latency_ms=self._latencies[-1] if self._latencies else None,
            custom=custom or {},
        )
        self._snapshots.append(snapshot)
        return snapshot

    def stop(self) -> Dict[str, float]:
        """
        Stop collecting and return computed metrics.

        Returns:
            Dictionary of computed metrics
        """
        if self._start_time is None:
            return {}

        total_time = time.perf_counter() - self._start_time

        # Memory stats
        memory_peak_mb = 0.0
        memory_delta_mb = 0.0
        if self.track_memory:
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            memory_peak_mb = peak / (1024 * 1024)
            memory_delta_mb = (current - self._start_memory) / (1024 * 1024)

        metrics = {
            "total_time_ms": total_time * 1000,
            "memory_peak_mb": memory_peak_mb,
            "memory_delta_mb": memory_delta_mb,
        }

        # Latency stats
        if self._latencies:
            latencies = np.array(self._latencies)
            metrics.update({
                "latency_count": len(latencies),
                "latency_mean_ms": float(np.mean(latencies)),
                "latency_std_ms": float(np.std(latencies)),
                "latency_min_ms": float(np.min(latencies)),
                "latency_max_ms": float(np.max(latencies)),
                "latency_p50_ms": float(np.percentile(latencies, 50)),
                "latency_p95_ms": float(np.percentile(latencies, 95)),
                "latency_p99_ms": float(np.percentile(latencies, 99)),
                "throughput_ops": len(latencies) / total_time if total_time > 0 else 0,
            })

        # Custom metrics
        for name, values in self._custom_metrics.items():
            arr = np.array(values)
            metrics[f"{name}_mean"] = float(np.mean(arr))
            metrics[f"{name}_sum"] = float(np.sum(arr))

        return metrics

    def get_latency_histogram(self, bins: int = 50) -> Dict[str, any]:
        """
        Get latency histogram data.

        Args:
            bins: Number of histogram bins

        Returns:
            Dict with histogram data
        """
        if not self._latencies:
            return {"bins": [], "counts": []}

        counts, bin_edges = np.histogram(self._latencies, bins=bins)

        return {
            "bins": [(bin_edges[i], bin_edges[i+1]) for i in range(len(counts))],
            "counts": counts.tolist(),
            "total": len(self._latencies),
        }


class ThroughputTracker:
    """
    Track throughput over time.

    Useful for monitoring performance during long-running operations.
    """

    def __init__(self, window_seconds: float = 1.0):
        """
        Initialize throughput tracker.

        Args:
            window_seconds: Time window for throughput calculation
        """
        self.window_seconds = window_seconds
        self._operations: List[float] = []  # Timestamps
        self._start_time: Optional[float] = None

    def start(self) -> None:
        """Start tracking."""
        self._operations = []
        self._start_time = time.perf_counter()

    def record_operation(self, count: int = 1) -> None:
        """
        Record completed operations.

        Args:
            count: Number of operations completed
        """
        timestamp = time.perf_counter()
        for _ in range(count):
            self._operations.append(timestamp)

    def get_current_throughput(self) -> float:
        """
        Get current throughput in operations per second.

        Returns:
            Operations per second in the last window
        """
        if not self._operations:
            return 0.0

        now = time.perf_counter()
        window_start = now - self.window_seconds

        # Count operations in window
        count = sum(1 for t in self._operations if t >= window_start)

        return count / self.window_seconds

    def get_average_throughput(self) -> float:
        """
        Get average throughput since start.

        Returns:
            Average operations per second
        """
        if not self._operations or self._start_time is None:
            return 0.0

        elapsed = time.perf_counter() - self._start_time
        return len(self._operations) / elapsed if elapsed > 0 else 0.0

    def get_stats(self) -> Dict[str, float]:
        """Get throughput statistics."""
        return {
            "current_throughput": self.get_current_throughput(),
            "average_throughput": self.get_average_throughput(),
            "total_operations": len(self._operations),
            "elapsed_seconds": time.perf_counter() - self._start_time if self._start_time else 0,
        }
