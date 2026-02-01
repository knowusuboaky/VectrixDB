"""
Benchmark Runner

Core benchmark execution with timing and memory tracking.
"""

import gc
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import numpy as np


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    name: str
    duration_ms: float = 0.0
    operations_per_second: float = 0.0
    memory_peak_mb: float = 0.0
    memory_delta_mb: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    latency_mean_ms: float = 0.0
    latency_std_ms: float = 0.0
    recall_at_k: Optional[float] = None
    throughput_items: int = 0
    custom_metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "duration_ms": round(self.duration_ms, 2),
            "operations_per_second": round(self.operations_per_second, 2),
            "memory_peak_mb": round(self.memory_peak_mb, 2),
            "memory_delta_mb": round(self.memory_delta_mb, 2),
            "latency_p50_ms": round(self.latency_p50_ms, 3),
            "latency_p95_ms": round(self.latency_p95_ms, 3),
            "latency_p99_ms": round(self.latency_p99_ms, 3),
            "latency_mean_ms": round(self.latency_mean_ms, 3),
            "latency_std_ms": round(self.latency_std_ms, 3),
            "recall_at_k": round(self.recall_at_k, 4) if self.recall_at_k else None,
            "throughput_items": self.throughput_items,
            "custom_metrics": self.custom_metrics,
        }


class BenchmarkRunner:
    """
    Runs benchmarks and collects metrics.

    Handles warmup iterations, memory tracking, and latency collection.

    Example:
        >>> runner = BenchmarkRunner(warmup_iterations=3)
        >>> result = runner.run_benchmark(
        ...     name="search_latency",
        ...     setup=lambda: create_index(),
        ...     benchmark=lambda ctx: search(ctx),
        ...     n_iterations=100
        ... )
    """

    def __init__(
        self,
        warmup_iterations: int = 3,
        benchmark_iterations: int = 10,
        track_memory: bool = True,
        gc_before_benchmark: bool = True,
    ):
        """
        Initialize benchmark runner.

        Args:
            warmup_iterations: Iterations before measurement
            benchmark_iterations: Iterations for measurement
            track_memory: Enable memory tracking
            gc_before_benchmark: Run garbage collection before benchmarks
        """
        self.warmup_iterations = warmup_iterations
        self.benchmark_iterations = benchmark_iterations
        self.track_memory = track_memory
        self.gc_before_benchmark = gc_before_benchmark

    def run_benchmark(
        self,
        name: str,
        benchmark: Callable[[Any], None],
        setup: Optional[Callable[[], Any]] = None,
        teardown: Optional[Callable[[Any], None]] = None,
        n_iterations: Optional[int] = None,
        n_operations: int = 1,
        custom_metrics: Optional[Callable[[Any], Dict]] = None,
    ) -> BenchmarkResult:
        """
        Run a single benchmark with setup/teardown.

        Args:
            name: Benchmark name
            benchmark: Function to benchmark (receives context from setup)
            setup: Optional setup function, returns context
            teardown: Optional cleanup function
            n_iterations: Override default iterations
            n_operations: Operations per iteration (for throughput)
            custom_metrics: Function to compute custom metrics

        Returns:
            BenchmarkResult with all metrics
        """
        iterations = n_iterations or self.benchmark_iterations
        latencies = []

        # Setup
        context = setup() if setup else None

        # Garbage collection
        if self.gc_before_benchmark:
            gc.collect()

        # Memory tracking
        if self.track_memory:
            tracemalloc.start()
            memory_before = tracemalloc.get_traced_memory()[0]

        # Warmup
        for _ in range(self.warmup_iterations):
            benchmark(context)

        # Benchmark iterations
        total_start = time.perf_counter()

        for _ in range(iterations):
            iter_start = time.perf_counter()
            benchmark(context)
            iter_end = time.perf_counter()
            latencies.append((iter_end - iter_start) * 1000)  # ms

        total_end = time.perf_counter()

        # Memory tracking
        memory_peak_mb = 0.0
        memory_delta_mb = 0.0
        if self.track_memory:
            memory_current, memory_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            memory_peak_mb = memory_peak / (1024 * 1024)
            memory_delta_mb = (memory_current - memory_before) / (1024 * 1024)

        # Compute metrics
        total_duration_ms = (total_end - total_start) * 1000
        total_operations = iterations * n_operations
        ops_per_second = (total_operations / total_duration_ms) * 1000

        latencies_array = np.array(latencies)

        result = BenchmarkResult(
            name=name,
            duration_ms=total_duration_ms,
            operations_per_second=ops_per_second,
            memory_peak_mb=memory_peak_mb,
            memory_delta_mb=memory_delta_mb,
            latency_p50_ms=float(np.percentile(latencies_array, 50)),
            latency_p95_ms=float(np.percentile(latencies_array, 95)),
            latency_p99_ms=float(np.percentile(latencies_array, 99)),
            latency_mean_ms=float(np.mean(latencies_array)),
            latency_std_ms=float(np.std(latencies_array)),
            throughput_items=total_operations,
        )

        # Custom metrics
        if custom_metrics:
            result.custom_metrics = custom_metrics(context)

        # Teardown
        if teardown:
            teardown(context)

        return result

    def run_latency_benchmark(
        self,
        name: str,
        benchmark: Callable[[], None],
        n_iterations: int = 1000,
    ) -> BenchmarkResult:
        """
        Run a simple latency benchmark.

        Args:
            name: Benchmark name
            benchmark: Function to benchmark (no context)
            n_iterations: Number of iterations

        Returns:
            BenchmarkResult focused on latency
        """
        return self.run_benchmark(
            name=name,
            benchmark=lambda _: benchmark(),
            n_iterations=n_iterations,
        )

    def run_throughput_benchmark(
        self,
        name: str,
        benchmark: Callable[[int], None],
        batch_sizes: List[int] = [100, 1000, 10000],
        n_iterations: int = 10,
    ) -> List[BenchmarkResult]:
        """
        Run throughput benchmark with different batch sizes.

        Args:
            name: Benchmark name
            benchmark: Function taking batch_size argument
            batch_sizes: Batch sizes to test
            n_iterations: Iterations per batch size

        Returns:
            List of BenchmarkResults for each batch size
        """
        results = []

        for batch_size in batch_sizes:
            result = self.run_benchmark(
                name=f"{name}_batch_{batch_size}",
                benchmark=lambda _, bs=batch_size: benchmark(bs),
                n_iterations=n_iterations,
                n_operations=batch_size,
            )
            result.custom_metrics["batch_size"] = batch_size
            results.append(result)

        return results

    def run_suite(
        self,
        benchmarks: List[Dict[str, Any]],
        on_progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> List[BenchmarkResult]:
        """
        Run a suite of benchmarks.

        Args:
            benchmarks: List of benchmark configs with keys:
                - name: Benchmark name
                - benchmark: Function to benchmark
                - setup: Optional setup function
                - teardown: Optional teardown
                - n_iterations: Optional iterations
            on_progress: Progress callback (name, current, total)

        Returns:
            List of BenchmarkResults
        """
        results = []
        total = len(benchmarks)

        for i, config in enumerate(benchmarks):
            name = config.get("name", f"benchmark_{i}")

            if on_progress:
                on_progress(name, i + 1, total)

            result = self.run_benchmark(
                name=name,
                benchmark=config["benchmark"],
                setup=config.get("setup"),
                teardown=config.get("teardown"),
                n_iterations=config.get("n_iterations"),
                n_operations=config.get("n_operations", 1),
                custom_metrics=config.get("custom_metrics"),
            )
            results.append(result)

        return results


class RecallBenchmark:
    """
    Benchmark for measuring search recall.

    Compares approximate search results against ground truth.
    """

    def __init__(self, k: int = 10):
        """
        Initialize recall benchmark.

        Args:
            k: Number of results to consider
        """
        self.k = k

    def compute_recall(
        self,
        results: List[List[str]],
        ground_truth: List[List[str]],
    ) -> float:
        """
        Compute recall@k.

        Args:
            results: Search results per query
            ground_truth: Ground truth results per query

        Returns:
            Recall score (0-1)
        """
        if len(results) != len(ground_truth):
            raise ValueError("Results and ground truth must have same length")

        total_recall = 0.0
        n_queries = len(results)

        for result, truth in zip(results, ground_truth):
            result_set = set(result[:self.k])
            truth_set = set(truth[:self.k])

            if truth_set:
                recall = len(result_set & truth_set) / len(truth_set)
                total_recall += recall

        return total_recall / n_queries if n_queries > 0 else 0.0

    def compute_mrr(
        self,
        results: List[List[str]],
        ground_truth: List[List[str]],
    ) -> float:
        """
        Compute Mean Reciprocal Rank.

        Args:
            results: Search results per query
            ground_truth: Ground truth results per query

        Returns:
            MRR score (0-1)
        """
        total_rr = 0.0
        n_queries = len(results)

        for result, truth in zip(results, ground_truth):
            truth_set = set(truth)

            for rank, item in enumerate(result, 1):
                if item in truth_set:
                    total_rr += 1.0 / rank
                    break

        return total_rr / n_queries if n_queries > 0 else 0.0

    def compute_ndcg(
        self,
        results: List[List[str]],
        ground_truth: List[List[str]],
    ) -> float:
        """
        Compute Normalized Discounted Cumulative Gain.

        Args:
            results: Search results per query
            ground_truth: Ground truth results per query

        Returns:
            NDCG score (0-1)
        """
        total_ndcg = 0.0
        n_queries = len(results)

        for result, truth in zip(results, ground_truth):
            truth_set = set(truth[:self.k])

            # DCG
            dcg = 0.0
            for i, item in enumerate(result[:self.k]):
                if item in truth_set:
                    dcg += 1.0 / np.log2(i + 2)  # i+2 because rank starts at 1

            # IDCG (ideal DCG)
            idcg = sum(1.0 / np.log2(i + 2) for i in range(min(len(truth_set), self.k)))

            if idcg > 0:
                total_ndcg += dcg / idcg

        return total_ndcg / n_queries if n_queries > 0 else 0.0
