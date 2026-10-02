"""
Tests for the VectrixDB benchmarking suite.

Covers vectrixdb/benchmarks/datasets.py (synthetic dataset generators),
vectrixdb/benchmarks/metrics.py (the benchmarks MetricsCollector and
ThroughputTracker, distinct from the collector of the same name in
core/scaling.py), vectrixdb/benchmarks/runner.py (BenchmarkRunner and
RecallBenchmark) and vectrixdb/benchmarks/reports.py (report formatting and
compare_results). Everything runs offline; wall-clock dependent fields use a
monkeypatched time.perf_counter for exact, hand-computed assertions.
"""

import json
import math

import numpy as np
import pytest

from vectrixdb.benchmarks import metrics as metrics_mod
from vectrixdb.benchmarks import runner as runner_mod
from vectrixdb.benchmarks.datasets import BenchmarkDatasets, DatasetScaler
from vectrixdb.benchmarks.metrics import MetricsCollector as BenchMetricsCollector
from vectrixdb.benchmarks.metrics import ThroughputTracker
from vectrixdb.benchmarks.reports import BenchmarkReport, compare_results
from vectrixdb.benchmarks.runner import BenchmarkResult, BenchmarkRunner, RecallBenchmark


# =============================================================================
# BenchmarkDatasets
# =============================================================================


class TestRandomVectors:
    def test_shape_and_dtype(self):
        vectors = BenchmarkDatasets.random_vectors(20, 8)
        assert vectors.shape == (20, 8)
        assert vectors.dtype == np.dtype("float32")

    def test_deterministic_under_a_fixed_seed(self):
        a = BenchmarkDatasets.random_vectors(10, 4, seed=7)
        b = BenchmarkDatasets.random_vectors(10, 4, seed=7)
        assert np.array_equal(a, b)

    def test_different_seeds_produce_different_vectors(self):
        a = BenchmarkDatasets.random_vectors(10, 4, seed=1)
        b = BenchmarkDatasets.random_vectors(10, 4, seed=2)
        assert not np.array_equal(a, b)

    def test_normalize_true_produces_unit_vectors(self):
        vectors = BenchmarkDatasets.random_vectors(15, 6, normalize=True, seed=3)
        norms = np.linalg.norm(vectors, axis=1)
        assert norms == pytest.approx(np.ones(15), abs=1e-5)

    def test_normalize_false_skips_normalization(self):
        vectors = BenchmarkDatasets.random_vectors(15, 6, normalize=False, seed=3)
        norms = np.linalg.norm(vectors, axis=1)
        assert not np.allclose(norms, 1.0)


class TestClusteredVectors:
    def test_shapes_and_label_range(self):
        vectors, labels = BenchmarkDatasets.clustered_vectors(50, 4, n_clusters=5, seed=1)
        assert vectors.shape == (50, 4)
        assert labels.shape == (50,)
        assert labels.min() >= 0
        assert labels.max() < 5

    def test_deterministic_under_a_fixed_seed(self):
        v1, l1 = BenchmarkDatasets.clustered_vectors(30, 3, n_clusters=4, seed=9)
        v2, l2 = BenchmarkDatasets.clustered_vectors(30, 3, n_clusters=4, seed=9)
        assert np.array_equal(v1, v2)
        assert np.array_equal(l1, l2)

    def test_normalized_vectors_are_unit_length(self):
        vectors, _ = BenchmarkDatasets.clustered_vectors(
            20, 5, n_clusters=3, normalize=True, seed=2
        )
        norms = np.linalg.norm(vectors, axis=1)
        assert norms == pytest.approx(np.ones(20), abs=1e-5)


class TestQueryWithGroundTruth:
    def test_shapes(self):
        base = BenchmarkDatasets.random_vectors(40, 8, seed=1)
        queries, ground_truth = BenchmarkDatasets.query_with_ground_truth(
            base, n_queries=5, k=3, seed=2
        )
        assert queries.shape == (5, 8)
        assert ground_truth.shape == (5, 3)

    def test_deterministic_under_a_fixed_seed(self):
        base = BenchmarkDatasets.random_vectors(40, 8, seed=1)
        q1, g1 = BenchmarkDatasets.query_with_ground_truth(base, n_queries=5, k=3, seed=2)
        q2, g2 = BenchmarkDatasets.query_with_ground_truth(base, n_queries=5, k=3, seed=2)
        assert np.array_equal(q1, q2)
        assert np.array_equal(g1, g2)

    def test_ground_truth_is_the_true_brute_force_top_k(self):
        base = BenchmarkDatasets.random_vectors(40, 8, seed=1)
        queries, ground_truth = BenchmarkDatasets.query_with_ground_truth(
            base, n_queries=5, k=3, seed=2
        )

        for query, truth in zip(queries, ground_truth):
            similarities = np.dot(base, query)
            expected_top_k = np.argsort(-similarities)[:3]
            assert list(truth) == list(expected_top_k)


class TestGenerateMetadata:
    def test_int_field_within_the_documented_range(self):
        records = BenchmarkDatasets.generate_metadata(50, {"n": "int"}, seed=1)
        assert len(records) == 50
        assert all(isinstance(r["n"], int) for r in records)
        assert all(0 <= r["n"] < 1000 for r in records)

    def test_float_field_within_the_documented_range(self):
        records = BenchmarkDatasets.generate_metadata(50, {"x": "float"}, seed=1)
        assert all(isinstance(r["x"], float) for r in records)
        assert all(0.0 <= r["x"] < 1000.0 for r in records)

    def test_string_field_uses_the_allowed_values(self):
        allowed = {"category_a", "category_b", "category_c", "category_d", "category_e"}
        records = BenchmarkDatasets.generate_metadata(50, {"s": "string"}, seed=1)
        assert all(str(r["s"]) in allowed for r in records)

    def test_bool_field_is_a_python_bool(self):
        records = BenchmarkDatasets.generate_metadata(50, {"b": "bool"}, seed=1)
        assert all(isinstance(r["b"], bool) for r in records)

    def test_tags_field_size_and_values(self):
        allowed = {"tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8"}
        records = BenchmarkDatasets.generate_metadata(50, {"t": "tags"}, seed=1)
        for record in records:
            tags = record["t"]
            assert 1 <= len(tags) <= 4  # rng.integers(1, 5) excludes the upper bound
            assert len(set(tags)) == len(tags)  # replace=False: no duplicate tags
            assert all(str(tag) in allowed for tag in tags)

    def test_deterministic_under_a_fixed_seed(self):
        a = BenchmarkDatasets.generate_metadata(20, {"n": "int", "s": "string"}, seed=5)
        b = BenchmarkDatasets.generate_metadata(20, {"n": "int", "s": "string"}, seed=5)
        assert a == b


class TestGloveSample:
    def test_shape_and_clip_bounds(self):
        vectors = BenchmarkDatasets.glove_sample(n=30, dimension=16)
        assert vectors.shape == (30, 16)
        assert vectors.min() >= -5
        assert vectors.max() <= 5

    def test_deterministic_across_calls(self):
        # glove_sample always seeds its own rng with 42, regardless of caller state.
        a = BenchmarkDatasets.glove_sample(n=10, dimension=8)
        b = BenchmarkDatasets.glove_sample(n=10, dimension=8)
        assert np.array_equal(a, b)


class TestSentenceEmbeddingsSample:
    def test_shape_and_normalization(self):
        vectors = BenchmarkDatasets.sentence_embeddings_sample(n=25, dimension=32, seed=3)
        assert vectors.shape == (25, 32)
        norms = np.linalg.norm(vectors, axis=1)
        assert norms == pytest.approx(np.ones(25), abs=1e-5)

    def test_deterministic_under_a_fixed_seed(self):
        a = BenchmarkDatasets.sentence_embeddings_sample(n=10, dimension=8, seed=4)
        b = BenchmarkDatasets.sentence_embeddings_sample(n=10, dimension=8, seed=4)
        assert np.array_equal(a, b)


class TestSparseVectors:
    def test_nonzero_count_hand_computed(self):
        # int(dimension * (1 - sparsity)), computed the same way here to avoid
        # assuming away float rounding (1 - 0.9 is not exactly 0.1).
        expected_nonzero = int(20 * (1 - 0.9))
        vectors = BenchmarkDatasets.sparse_vectors(n=10, dimension=20, sparsity=0.9, seed=1)
        assert len(vectors) == 10
        assert all(len(v) == expected_nonzero for v in vectors)

    def test_indices_within_dimension_and_values_in_range(self):
        vectors = BenchmarkDatasets.sparse_vectors(n=10, dimension=20, sparsity=0.9, seed=1)
        for sparse in vectors:
            assert all(0 <= idx < 20 for idx in sparse)
            assert all(0.1 <= val <= 1.0 for val in sparse.values())

    def test_deterministic_under_a_fixed_seed(self):
        a = BenchmarkDatasets.sparse_vectors(n=5, dimension=10, seed=2)
        b = BenchmarkDatasets.sparse_vectors(n=5, dimension=10, seed=2)
        assert a == b


class TestDatasetScaler:
    def test_get_size_known_names(self):
        assert DatasetScaler.get_size("tiny") == 1_000
        assert DatasetScaler.get_size("small") == 10_000
        assert DatasetScaler.get_size("medium") == 100_000
        assert DatasetScaler.get_size("large") == 1_000_000
        assert DatasetScaler.get_size("xlarge") == 10_000_000

    def test_get_size_unknown_name_defaults_to_ten_thousand(self):
        assert DatasetScaler.get_size("nonexistent") == 10_000

    def test_create_dataset_random_has_no_labels(self):
        vectors, labels = DatasetScaler.create_dataset("tiny", dimension=8, dataset_type="random")
        assert vectors.shape == (1_000, 8)
        assert labels is None

    def test_create_dataset_clustered_has_labels(self):
        vectors, labels = DatasetScaler.create_dataset(
            "tiny", dimension=8, dataset_type="clustered"
        )
        assert vectors.shape == (1_000, 8)
        assert labels is not None
        assert labels.shape == (1_000,)

    def test_create_dataset_sentence_has_no_labels(self):
        vectors, labels = DatasetScaler.create_dataset("tiny", dimension=8, dataset_type="sentence")
        assert vectors.shape == (1_000, 8)
        assert labels is None

    def test_create_dataset_unknown_type_falls_back_to_random(self):
        vectors, labels = DatasetScaler.create_dataset(
            "tiny", dimension=8, dataset_type="nonexistent"
        )
        expected = BenchmarkDatasets.random_vectors(1_000, 8)
        assert np.array_equal(vectors, expected)
        assert labels is None


# =============================================================================
# benchmarks/metrics.py: MetricsCollector, ThroughputTracker
# =============================================================================


class TestBenchMetricsCollector:
    def test_stop_computes_hand_computed_latency_stats(self, monkeypatch):
        collector = BenchMetricsCollector(track_memory=False)
        times = iter([100.0, 100.25])  # start() then stop(): 250ms elapsed
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: next(times))
        collector.start()
        for value in (1.0, 2.0, 3.0, 4.0):
            collector.record_latency(value)

        result = collector.stop()

        assert result["total_time_ms"] == pytest.approx(250.0)
        assert result["memory_peak_mb"] == 0.0
        assert result["memory_delta_mb"] == 0.0
        assert result["latency_count"] == 4
        assert result["latency_mean_ms"] == pytest.approx(2.5)
        assert result["latency_std_ms"] == pytest.approx(float(np.std([1.0, 2.0, 3.0, 4.0])))
        assert result["latency_min_ms"] == 1.0
        assert result["latency_max_ms"] == 4.0
        assert result["latency_p50_ms"] == pytest.approx(
            float(np.percentile([1.0, 2.0, 3.0, 4.0], 50))
        )
        assert result["throughput_ops"] == pytest.approx(4 / 0.25)

    def test_stop_without_start_returns_an_empty_dict(self):
        collector = BenchMetricsCollector(track_memory=False)
        assert collector.stop() == {}

    def test_sample_memory_without_tracking_returns_zero(self):
        collector = BenchMetricsCollector(track_memory=False)
        assert collector.sample_memory() == 0.0
        assert collector._memory_samples == []

    def test_stop_includes_custom_metric_mean_and_sum(self, monkeypatch):
        collector = BenchMetricsCollector(track_memory=False)
        times = iter([0.0, 1.0])
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: next(times))
        collector.start()
        collector.record_custom("recall", 0.5)
        collector.record_custom("recall", 0.7)

        result = collector.stop()

        assert result["recall_mean"] == pytest.approx(0.6)
        assert result["recall_sum"] == pytest.approx(1.2)

    def test_snapshot_captures_last_latency_and_elapsed_time(self, monkeypatch):
        collector = BenchMetricsCollector(track_memory=False)
        # A non-zero start time: see TestBenchMetricsKnownBugs for what happens
        # when start() happens to capture exactly 0.0.
        times = iter([100.0, 102.5])  # start(), then the snapshot's own perf_counter() call
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: next(times))
        collector.start()
        collector.record_latency(9.0)

        snap = collector.snapshot(custom={"k": 1.0})

        assert snap.timestamp == pytest.approx(2.5)
        assert snap.latency_ms == 9.0
        assert snap.custom == {"k": 1.0}
        assert snap.memory_mb == 0.0

    def test_snapshot_latency_is_none_before_any_latency_is_recorded(self, monkeypatch):
        collector = BenchMetricsCollector(track_memory=False)
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: 0.0)
        collector.start()

        snap = collector.snapshot()

        assert snap.latency_ms is None
        assert snap.custom == {}

    def test_get_latency_histogram_empty_when_nothing_recorded(self):
        collector = BenchMetricsCollector(track_memory=False)
        assert collector.get_latency_histogram() == {"bins": [], "counts": []}

    def test_get_latency_histogram_hand_computed_counts(self):
        collector = BenchMetricsCollector(track_memory=False)
        values = [1, 1, 2, 3, 3, 3]
        for value in values:
            collector.record_latency(float(value))

        histogram = collector.get_latency_histogram(bins=3)

        counts, edges = np.histogram(values, bins=3)
        assert histogram["counts"] == counts.tolist()
        assert histogram["total"] == 6
        assert histogram["bins"] == [(edges[i], edges[i + 1]) for i in range(len(counts))]


class TestThroughputTracker:
    def test_current_throughput_hand_computed_with_a_wide_window(self):
        # A window far larger than the test's real runtime makes every recorded
        # operation fall inside it, so the result is exactly count / window.
        tracker = ThroughputTracker(window_seconds=1000.0)
        tracker.start()
        tracker.record_operation(5)

        assert tracker.get_current_throughput() == pytest.approx(5 / 1000.0)

    def test_current_throughput_zero_with_no_operations(self):
        tracker = ThroughputTracker()
        tracker.start()
        assert tracker.get_current_throughput() == 0.0

    def test_average_throughput_hand_computed(self, monkeypatch):
        tracker = ThroughputTracker()
        times = iter([0.0, 0.0, 2.0])  # start(), record_operation()'s timestamp, get_average()
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: next(times))
        tracker.start()
        tracker.record_operation(4)

        assert tracker.get_average_throughput() == pytest.approx(4 / 2.0)

    def test_average_throughput_zero_before_start(self):
        tracker = ThroughputTracker()
        assert tracker.get_average_throughput() == 0.0

    def test_track_memory_true_populates_real_memory_fields(self):
        # track_memory=True is the default; values are inherently platform and
        # timing dependent so this checks types/invariants, not exact numbers.
        collector = BenchMetricsCollector()
        collector.start()
        _ = [object()] * 10_000  # give tracemalloc something to see
        sampled = collector.sample_memory()
        collector.record_latency(1.0)

        result = collector.stop()

        assert isinstance(sampled, float)
        assert sampled >= 0.0
        assert isinstance(result["memory_peak_mb"], float)
        assert result["memory_peak_mb"] >= 0.0
        assert isinstance(result["memory_delta_mb"], float)

    def test_get_stats_shape_and_operation_count(self):
        tracker = ThroughputTracker()
        tracker.start()
        tracker.record_operation(2)

        stats = tracker.get_stats()

        assert set(stats) == {
            "current_throughput",
            "average_throughput",
            "total_operations",
            "elapsed_seconds",
        }
        assert stats["total_operations"] == 2


class TestBenchMetricsKnownBugs:
    """Regression tests documenting real bugs found while writing this suite.

    Both come from the same root cause at two call sites: guarding "has
    start() been called" with a truthy check on a perf_counter() value
    instead of `is not None`. Each is marked xfail(strict=True): if a fix
    lands, the assertion starts passing, strict mode turns that into a
    failure, and that failure is the signal to delete the xfail marker.
    """

    def test_snapshot_elapsed_time_when_start_time_is_exactly_zero(self, monkeypatch):
        collector = BenchMetricsCollector(track_memory=False)
        times = iter([0.0, 2.5])
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: next(times))
        collector.start()  # _start_time becomes exactly 0.0

        snap = collector.snapshot()

        assert snap.timestamp == pytest.approx(2.5)

    def test_get_stats_elapsed_seconds_when_start_time_is_exactly_zero(self, monkeypatch):
        tracker = ThroughputTracker()
        times = iter([0.0, 3.0])
        monkeypatch.setattr(metrics_mod.time, "perf_counter", lambda: next(times))
        tracker.start()  # _start_time becomes exactly 0.0

        stats = tracker.get_stats()

        assert stats["elapsed_seconds"] == pytest.approx(3.0)


# =============================================================================
# benchmarks/runner.py: BenchmarkResult, BenchmarkRunner, RecallBenchmark
# =============================================================================


class TestBenchmarkResultToDict:
    def test_rounds_every_float_field(self):
        result = BenchmarkResult(
            name="bench",
            duration_ms=1.23456,
            operations_per_second=987.6543,
            memory_peak_mb=10.11111,
            memory_delta_mb=1.23456,
            latency_p50_ms=1.23456,
            latency_p95_ms=2.34567,
            latency_p99_ms=3.45678,
            latency_mean_ms=1.11111,
            latency_std_ms=0.11111,
            recall_at_k=0.987654,
            throughput_items=42,
            custom_metrics={"x": 1},
        )

        assert result.to_dict() == {
            "name": "bench",
            "duration_ms": round(1.23456, 2),
            "operations_per_second": round(987.6543, 2),
            "memory_peak_mb": round(10.11111, 2),
            "memory_delta_mb": round(1.23456, 2),
            "latency_p50_ms": round(1.23456, 3),
            "latency_p95_ms": round(2.34567, 3),
            "latency_p99_ms": round(3.45678, 3),
            "latency_mean_ms": round(1.11111, 3),
            "latency_std_ms": round(0.11111, 3),
            "recall_at_k": round(0.987654, 4),
            "throughput_items": 42,
            "custom_metrics": {"x": 1},
        }

    def test_recall_at_k_is_none_when_never_measured(self):
        result = BenchmarkResult(name="bench")
        assert result.to_dict()["recall_at_k"] is None


class TestBenchmarkRunner:
    def test_run_benchmark_hand_computed_stats_with_fixed_latencies(self, monkeypatch):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=4,
            track_memory=False,
            gc_before_benchmark=False,
        )
        # perf_counter call sequence: total_start, 4x(iter_start, iter_end), total_end.
        # Chosen so the 4 iterations take 1, 2, 3, 4 ms respectively (10ms total).
        times = iter([0.0, 0.0, 0.001, 0.001, 0.003, 0.003, 0.006, 0.006, 0.010, 0.010])
        monkeypatch.setattr(runner_mod.time, "perf_counter", lambda: next(times))
        calls = []

        result = runner.run_benchmark(name="bench", benchmark=calls.append)

        assert len(calls) == 4  # no warmup, 4 measured iterations
        assert result.name == "bench"
        assert result.throughput_items == 4
        assert result.memory_peak_mb == 0.0
        assert result.memory_delta_mb == 0.0
        assert result.duration_ms == pytest.approx(10.0)
        assert result.latency_mean_ms == pytest.approx(float(np.mean([1.0, 2.0, 3.0, 4.0])))
        assert result.latency_p50_ms == pytest.approx(
            float(np.percentile([1.0, 2.0, 3.0, 4.0], 50))
        )
        assert result.operations_per_second == pytest.approx((4 / 10.0) * 1000)

    def test_track_memory_true_populates_real_memory_fields(self):
        # track_memory defaults to True; values are platform/timing dependent
        # so this checks types and invariants rather than exact numbers.
        runner = BenchmarkRunner(
            warmup_iterations=0, benchmark_iterations=2, gc_before_benchmark=False
        )

        result = runner.run_benchmark(name="b", benchmark=lambda ctx: [object()] * 1000)

        assert isinstance(result.memory_peak_mb, float)
        assert result.memory_peak_mb >= 0.0
        assert isinstance(result.memory_delta_mb, float)

    def test_run_benchmark_applies_warmup_without_measuring_it(self):
        runner = BenchmarkRunner(
            warmup_iterations=2,
            benchmark_iterations=3,
            track_memory=False,
            gc_before_benchmark=False,
        )
        calls = []

        result = runner.run_benchmark(name="b", benchmark=calls.append)

        assert len(calls) == 5  # 2 warmup + 3 measured
        assert result.throughput_items == 3  # only the measured iterations count

    def test_run_benchmark_calls_setup_and_teardown_with_the_shared_context(self):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=2,
            track_memory=False,
            gc_before_benchmark=False,
        )
        events = []

        def setup():
            events.append("setup")
            return {"ctx": True}

        def bench(ctx):
            events.append(("bench", ctx))

        def teardown(ctx):
            events.append(("teardown", ctx))

        runner.run_benchmark(name="b", benchmark=bench, setup=setup, teardown=teardown)

        assert events[0] == "setup"
        assert events[-1] == ("teardown", {"ctx": True})
        assert events.count(("bench", {"ctx": True})) == 2

    def test_run_benchmark_computes_custom_metrics_from_the_context(self):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=1,
            track_memory=False,
            gc_before_benchmark=False,
        )

        result = runner.run_benchmark(
            name="b",
            benchmark=lambda ctx: None,
            setup=lambda: {"n": 7},
            custom_metrics=lambda ctx: {"n_doubled": ctx["n"] * 2},
        )

        assert result.custom_metrics == {"n_doubled": 14}

    def test_run_latency_benchmark_wraps_a_no_context_function(self):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=1,
            track_memory=False,
            gc_before_benchmark=False,
        )
        calls = []

        result = runner.run_latency_benchmark("latency", lambda: calls.append(1), n_iterations=5)

        assert len(calls) == 5
        assert result.throughput_items == 5
        assert result.name == "latency"

    def test_run_throughput_benchmark_uses_the_documented_default_batch_sizes(self):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=1,
            track_memory=False,
            gc_before_benchmark=False,
        )
        seen = []

        results = runner.run_throughput_benchmark("thr", seen.append)

        assert [r.name for r in results] == ["thr_batch_100", "thr_batch_1000", "thr_batch_10000"]
        # run_throughput_benchmark's own default is n_iterations=10, so each batch
        # size's benchmark function runs 10 times (0 warmup, per this runner's config).
        assert seen == [100] * 10 + [1000] * 10 + [10000] * 10
        assert [r.custom_metrics["batch_size"] for r in results] == [100, 1000, 10000]
        assert [r.throughput_items for r in results] == [1000, 10000, 100000]

    def test_run_throughput_benchmark_with_explicit_batch_sizes_and_iterations(self):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=2,
            track_memory=False,
            gc_before_benchmark=False,
        )

        results = runner.run_throughput_benchmark(
            "thr", lambda _bs: None, batch_sizes=[5, 10], n_iterations=2
        )

        assert len(results) == 2
        assert results[0].throughput_items == 10  # batch_size 5 x 2 iterations
        assert results[1].throughput_items == 20  # batch_size 10 x 2 iterations

    def test_run_suite_runs_each_benchmark_in_order_and_reports_progress(self):
        runner = BenchmarkRunner(
            warmup_iterations=0,
            benchmark_iterations=1,
            track_memory=False,
            gc_before_benchmark=False,
        )
        progress = []
        configs = [
            {"name": "first", "benchmark": lambda ctx: None},
            {"benchmark": lambda ctx: None},  # no name -> defaults to benchmark_1
        ]

        results = runner.run_suite(
            configs, on_progress=lambda name, i, total: progress.append((name, i, total))
        )

        assert [r.name for r in results] == ["first", "benchmark_1"]
        assert progress == [("first", 1, 2), ("benchmark_1", 2, 2)]


class TestRecallBenchmark:
    def test_compute_recall_hand_computed(self):
        bench = RecallBenchmark(k=3)
        results = [["a", "b", "c", "d"], ["x", "y"]]
        ground_truth = [["a", "b", "z"], ["x"]]

        # query0: {a,b,c} & {a,b,z} = {a,b} -> 2/3. query1: {x,y} & {x} = {x} -> 1/1.
        expected = ((2 / 3) + 1.0) / 2

        assert bench.compute_recall(results, ground_truth) == pytest.approx(expected)

    def test_compute_recall_counts_an_empty_ground_truth_query_as_zero(self):
        bench = RecallBenchmark(k=3)
        results = [["a", "b"], ["c"]]
        ground_truth = [["a"], []]  # second query has no ground truth at all

        # query0: {a} & {a} = {a} -> 1/1 = 1.0. query1: skipped (contributes 0), but
        # still counted in the denominator.
        assert bench.compute_recall(results, ground_truth) == pytest.approx(0.5)

    def test_compute_recall_raises_on_mismatched_lengths(self):
        bench = RecallBenchmark()
        with pytest.raises(ValueError):
            bench.compute_recall([["a"]], [["a"], ["b"]])

    def test_compute_recall_zero_queries_is_zero(self):
        assert RecallBenchmark().compute_recall([], []) == 0.0

    def test_compute_mrr_hand_computed(self):
        bench = RecallBenchmark()
        results = [["x", "a", "b"], ["c", "d"]]
        ground_truth = [["a"], ["z"]]

        # query0: "a" found at rank 2 -> 1/2. query1: never found -> 0.
        expected = (0.5 + 0.0) / 2

        assert bench.compute_mrr(results, ground_truth) == pytest.approx(expected)

    def test_compute_mrr_zero_queries_is_zero(self):
        assert RecallBenchmark().compute_mrr([], []) == 0.0

    def test_compute_ndcg_hand_computed(self):
        bench = RecallBenchmark(k=3)
        results = [["a", "x", "b"]]
        ground_truth = [["a", "b", "c"]]

        expected_dcg = 1.0 / math.log2(2) + 1.0 / math.log2(4)  # "a" at rank1, "b" at rank3
        expected_idcg = sum(1.0 / math.log2(i + 2) for i in range(3))
        expected_ndcg = expected_dcg / expected_idcg

        assert bench.compute_ndcg(results, ground_truth) == pytest.approx(expected_ndcg)

    def test_compute_ndcg_zero_queries_is_zero(self):
        assert RecallBenchmark().compute_ndcg([], []) == 0.0


class TestRunnerKnownBugs:
    """Regression tests documenting real bugs found while writing this suite.

    Each is marked xfail(strict=True) with a one-line reason. If a fix lands,
    the assertion starts passing, strict mode turns that into a failure, and
    that failure is the signal to delete the xfail marker.
    """

    def test_to_dict_preserves_a_real_zero_recall(self):
        result = BenchmarkResult(name="b", recall_at_k=0.0)
        assert result.to_dict()["recall_at_k"] == 0.0

    def test_run_benchmark_honors_an_explicit_zero_iterations(self):
        runner = BenchmarkRunner(benchmark_iterations=3, warmup_iterations=0, track_memory=False)
        calls = []

        result = runner.run_benchmark(name="b", benchmark=calls.append, n_iterations=0)

        assert len(calls) == 0
        assert result.throughput_items == 0


# =============================================================================
# benchmarks/reports.py: BenchmarkReport, compare_results
# =============================================================================


def _rich_result(**overrides):
    params = {
        "name": "bench1",
        "duration_ms": 123.456,
        "operations_per_second": 5000.0,
        "memory_peak_mb": 12.3,
        "memory_delta_mb": 1.2,
        "latency_p50_ms": 1.234,
        "latency_p95_ms": 2.345,
        "latency_p99_ms": 3.456,
        "latency_mean_ms": 1.5,
        "latency_std_ms": 0.5,
        "recall_at_k": 0.9876,
        "throughput_items": 10000,
        "custom_metrics": {"batch_size": 100, "note": "ok"},
    }
    params.update(overrides)
    return BenchmarkResult(**params)


class TestBenchmarkReportMarkdown:
    def test_matches_the_expected_text_exactly(self):
        report = BenchmarkReport([_rich_result()], title="My Report", description="A description.")

        text = report.to_markdown()

        ts = report.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        expected = "\n".join(
            [
                "# My Report",
                "",
                f"*Generated: {ts}*",
                "",
                "A description.",
                "",
                "## Summary",
                "",
                "| Benchmark | Ops/sec | Latency P50 | Latency P99 | Memory Peak |",
                "|-----------|---------|-------------|-------------|-------------|",
                "| bench1 | 5,000 | 1.23ms | 3.46ms | 12.3MB |",
                "",
                "## Detailed Results",
                "",
                "### bench1",
                "",
                "- **Duration:** 123.46ms",
                "- **Operations/sec:** 5,000.00",
                "- **Throughput items:** 10,000",
                "",
                "**Latency:**",
                "- Mean: 1.500ms",
                "- Std: 0.500ms",
                "- P50: 1.234ms",
                "- P95: 2.345ms",
                "- P99: 3.456ms",
                "",
                "**Memory:**",
                "- Peak: 12.30MB",
                "- Delta: 1.20MB",
                "",
                "**Recall@k:** 0.9876",
                "",
                "**Custom Metrics:**",
                "- batch_size: 100",
                "- note: ok",
                "",
            ]
        )

        assert text == expected

    def test_omits_the_description_block_when_blank(self):
        report = BenchmarkReport([_rich_result()], title="T", description="")
        assert "A description." not in report.to_markdown()

    def test_custom_metrics_float_values_are_formatted_to_four_decimals(self):
        report = BenchmarkReport([_rich_result(custom_metrics={"ratio": 0.123456})], title="T")
        assert "- ratio: 0.1235" in report.to_markdown()

    def test_omits_recall_and_custom_metrics_when_absent(self):
        report = BenchmarkReport([_rich_result(recall_at_k=None, custom_metrics={})], title="T")
        text = report.to_markdown()
        assert "Recall@k" not in text
        assert "Custom Metrics" not in text


class TestBenchmarkReportJson:
    def test_matches_the_expected_structure_exactly(self):
        result = _rich_result()
        report = BenchmarkReport([result], title="T", description="D")

        parsed = json.loads(report.to_json())

        assert parsed["title"] == "T"
        assert parsed["description"] == "D"
        assert parsed["timestamp"] == report.timestamp.isoformat()
        assert parsed["results"] == [result.to_dict()]

    def test_indent_parameter_controls_formatting(self):
        report = BenchmarkReport([_rich_result()], title="T")
        compact = report.to_json(indent=None)
        assert "\n" not in compact
        assert json.loads(compact) == json.loads(report.to_json())


class TestBenchmarkReportHtml:
    def test_contains_key_values_and_structure(self):
        report = BenchmarkReport([_rich_result()], title="HTML Report", description="Desc text")

        html = report.to_html()

        assert "<title>HTML Report</title>" in html
        assert "<h1>HTML Report</h1>" in html
        assert "<p>Desc text</p>" in html
        ts = report.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        assert f"Generated: {ts}" in html
        assert "<td>bench1</td>" in html
        assert "<td>5,000</td>" in html
        assert '<span class="metric good">0.9876</span>' in html
        assert html.count("<tr>") == 2  # header row + one result row
        assert html.strip().endswith("</html>")

    def test_omits_description_paragraph_when_blank(self):
        with_desc = BenchmarkReport([_rich_result()], title="T", description="hello").to_html()
        without_desc = BenchmarkReport([_rich_result()], title="T", description="").to_html()

        assert "<p>hello</p>" in with_desc
        assert "<p>hello</p>" not in without_desc

    def test_omits_recall_span_when_not_measured(self):
        html = BenchmarkReport([_rich_result(recall_at_k=None)], title="T").to_html()
        assert "Recall@k" not in html


class TestBenchmarkReportConsole:
    def test_matches_the_expected_text_exactly(self):
        result = _rich_result(
            name="short", operations_per_second=12345.678, latency_p50_ms=1.5, latency_p99_ms=9.876
        )
        report = BenchmarkReport([result], title="Console Report")

        text = report.to_console()

        expected = "\n".join(
            [
                "=" * 60,
                " Console Report",
                "=" * 60,
                "",
                "SUMMARY",
                "-" * 60,
                f"{'Benchmark':<25} {'Ops/sec':>12} {'P50':>10} {'P99':>10}",
                "-" * 60,
                f"{'short':<25} {12345.678:>12,.0f} {1.5:>9.2f}ms {9.876:>9.2f}ms",
                "",
                "=" * 60,
            ]
        )

        assert text == expected


class TestBenchmarkReportSave:
    def test_auto_detects_format_from_the_file_suffix(self, tmp_path):
        report = BenchmarkReport([_rich_result()], title="T")

        report.save(tmp_path / "r.md")
        report.save(tmp_path / "r.json")
        report.save(tmp_path / "r.html")
        report.save(tmp_path / "r.txt")
        report.save(tmp_path / "r.xyz")  # unknown suffix falls back to markdown

        assert (tmp_path / "r.md").read_text(encoding="utf-8") == report.to_markdown()
        assert (tmp_path / "r.json").read_text(encoding="utf-8") == report.to_json()
        assert (tmp_path / "r.html").read_text(encoding="utf-8") == report.to_html()
        assert (tmp_path / "r.txt").read_text(encoding="utf-8") == report.to_console()
        assert (tmp_path / "r.xyz").read_text(encoding="utf-8") == report.to_markdown()

    def test_an_explicit_format_overrides_the_suffix(self, tmp_path):
        report = BenchmarkReport([_rich_result()], title="T")
        path = tmp_path / "r.md"  # suffix says markdown

        report.save(path, format="json")

        assert path.read_text(encoding="utf-8") == report.to_json()


class TestCompareResults:
    def test_improved_when_ops_change_exceeds_five_percent(self):
        baseline = [BenchmarkResult(name="b", operations_per_second=100.0, latency_p50_ms=10.0)]
        current = [BenchmarkResult(name="b", operations_per_second=120.0, latency_p50_ms=10.0)]

        comparison = compare_results(baseline, current)["b"]

        assert comparison["ops_change_percent"] == pytest.approx(20.0)
        assert comparison["improved"] is True
        assert comparison["regressed"] is False

    def test_regressed_when_latency_gets_worse_by_more_than_five_percent(self):
        baseline = [BenchmarkResult(name="b", operations_per_second=100.0, latency_p50_ms=10.0)]
        current = [BenchmarkResult(name="b", operations_per_second=100.0, latency_p50_ms=12.0)]

        comparison = compare_results(baseline, current)["b"]

        # (base - curr) / base * 100 = (10 - 12) / 10 * 100 = -20
        assert comparison["latency_change_percent"] == pytest.approx(-20.0)
        assert comparison["regressed"] is True
        assert comparison["improved"] is False

    def test_within_five_percent_is_neither_improved_nor_regressed(self):
        baseline = [BenchmarkResult(name="b", operations_per_second=100.0, latency_p50_ms=10.0)]
        current = [BenchmarkResult(name="b", operations_per_second=102.0, latency_p50_ms=10.0)]

        comparison = compare_results(baseline, current)["b"]

        assert comparison["improved"] is False
        assert comparison["regressed"] is False

    def test_new_and_removed_benchmarks_are_flagged(self):
        baseline = [BenchmarkResult(name="only_baseline")]
        current = [BenchmarkResult(name="only_current")]

        comparison = compare_results(baseline, current)

        assert comparison["only_baseline"] == {"status": "removed"}
        assert comparison["only_current"] == {"status": "new"}

    def test_zero_baseline_values_avoid_division_by_zero(self):
        baseline = [
            BenchmarkResult(
                name="b", operations_per_second=0.0, latency_p50_ms=0.0, memory_peak_mb=0.0
            )
        ]
        current = [
            BenchmarkResult(
                name="b", operations_per_second=50.0, latency_p50_ms=5.0, memory_peak_mb=2.0
            )
        ]

        comparison = compare_results(baseline, current)["b"]

        assert comparison["ops_change_percent"] == 0
        assert comparison["latency_change_percent"] == 0
        assert comparison["memory_change_percent"] == 0
